#!/usr/bin/env python
import argparse
import json
import os
import subprocess
import sys
import time
from datetime import datetime, timedelta
from pathlib import Path


PROJECT_ROOT = Path(__file__).resolve().parents[1]
TRACKER_DIR = PROJECT_ROOT / "tracker"
TASKS_FILE = TRACKER_DIR / "tasks.json"
TASKS_LOCK_FILE = TRACKER_DIR / ".tasks.lock"
LOG_DIR = TRACKER_DIR / ".estimation-logs"
ORACLE_PROMPT_FILE = TRACKER_DIR / "oracle-prompt.txt"
MAX_CONTEXT_CHARS = 15_000

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8")
if hasattr(sys.stderr, "reconfigure"):
    sys.stderr.reconfigure(encoding="utf-8")


def parse_args(argv: list[str]) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Estimate human-time complexity for one Claude Code session.")
    parser.add_argument("session_id")
    parser.add_argument("transcript_path")
    return parser.parse_args(argv)


def read_tasks() -> dict:
    if not TASKS_FILE.exists():
        return {}
    try:
        with TASKS_FILE.open("r", encoding="utf-8") as source:
            data = json.load(source)
    except (OSError, json.JSONDecodeError):
        return {}
    return data if isinstance(data, dict) else {}


def acquire_tasks_lock(timeout_seconds: int = 10) -> int | None:
    deadline = datetime.now() + timedelta(seconds=timeout_seconds)
    while True:
        try:
            fd = os.open(str(TASKS_LOCK_FILE), os.O_CREAT | os.O_EXCL | os.O_WRONLY)
            os.write(fd, str(os.getpid()).encode("ascii"))
            return fd
        except FileExistsError:
            if datetime.now() >= deadline:
                return None
            time.sleep(0.05)
        except OSError:
            return None


def release_tasks_lock(fd: int | None) -> None:
    if fd is None:
        return
    os.close(fd)
    try:
        TASKS_LOCK_FILE.unlink()
    except OSError:
        pass


def atomic_write_json(path: Path, data: dict) -> None:
    TRACKER_DIR.mkdir(parents=True, exist_ok=True)
    temp_path = path.with_name(f".{path.name}.tmp.{os.getpid()}")
    with temp_path.open("w", encoding="utf-8", newline="\n") as target:
        json.dump(data, target, ensure_ascii=False, indent=2, sort_keys=True)
        target.write("\n")
        target.flush()
        os.fsync(target.fileno())
    os.replace(temp_path, path)


def update_task_entry(session_id: str, entry: dict) -> None:
    fd = acquire_tasks_lock()
    try:
        tasks = read_tasks()
        previous = tasks.get(session_id)
        if isinstance(previous, dict) and "human_corrected_hours" in previous:
            entry["human_corrected_hours"] = previous.get("human_corrected_hours")
        tasks[session_id] = entry
        atomic_write_json(TASKS_FILE, tasks)
    finally:
        release_tasks_lock(fd)


def remove_inflight_lock(session_id: str) -> None:
    try:
        (LOG_DIR / f"{session_id}.lock").unlink()
    except OSError:
        pass


def extract_text(content: object) -> str:
    if isinstance(content, str):
        return content
    if not isinstance(content, list):
        return ""

    parts = []
    for block in content:
        if isinstance(block, str):
            parts.append(block)
            continue
        if not isinstance(block, dict) or block.get("type") != "text":
            continue
        text = block.get("text")
        if isinstance(text, str):
            parts.append(text)

    return "\n".join(part.strip() for part in parts if part and part.strip())


def transcript_role(event: dict) -> str | None:
    event_type = event.get("type")
    if event_type in {"user", "assistant"}:
        return str(event_type)

    message = event.get("message")
    if isinstance(message, dict):
        role = message.get("role")
        if role in {"user", "assistant"}:
            return str(role)

    return None


def transcript_text(event: dict) -> str:
    message = event.get("message")
    if isinstance(message, dict):
        return extract_text(message.get("content"))
    return extract_text(event.get("content"))


def read_transcript(path: Path) -> tuple[list[str], list[str]]:
    user_messages = []
    assistant_messages = []

    with path.open("r", encoding="utf-8") as source:
        for line in source:
            if not line.strip():
                continue
            try:
                event = json.loads(line)
            except json.JSONDecodeError:
                continue
            if not isinstance(event, dict):
                continue

            role = transcript_role(event)
            if role is None:
                continue

            text = transcript_text(event).strip()
            if not text:
                continue

            if role == "user":
                user_messages.append(text)
            elif role == "assistant":
                assistant_messages.append(text)

    return user_messages[:3], assistant_messages[-5:]


def clip_text(text: str, limit: int) -> str:
    if len(text) <= limit:
        return text
    return text[:limit].rstrip() + "\n...[truncated]"


def clip_middle(text: str, limit: int) -> str:
    if len(text) <= limit:
        return text
    head = limit // 3
    tail = limit - head - len("\n...[middle truncated]...\n")
    return text[:head].rstrip() + "\n...[middle truncated]...\n" + text[-tail:].lstrip()


def render_context(user_messages: list[str], assistant_messages: list[str], per_message_limit: int) -> str:
    sections = []

    if user_messages:
        sections.append("### First user messages")
        for index, message in enumerate(user_messages, start=1):
            sections.append(f"[user {index}]\n{clip_text(message, per_message_limit)}")

    if assistant_messages:
        sections.append("### Last assistant messages")
        for index, message in enumerate(assistant_messages, start=1):
            sections.append(f"[assistant {index}]\n{clip_text(message, per_message_limit)}")

    if not sections:
        return "No readable transcript text found."

    return "\n\n".join(sections)


def build_truncated_context(transcript_path: str) -> str:
    user_messages, assistant_messages = read_transcript(Path(transcript_path))
    context = render_context(user_messages, assistant_messages, 2_500)
    if len(context) > MAX_CONTEXT_CHARS:
        context = render_context(user_messages, assistant_messages, 1_200)
    return clip_middle(context, MAX_CONTEXT_CHARS)


def parse_json_text(text: str) -> object:
    stripped = text.strip()
    if stripped.startswith("```"):
        lines = stripped.splitlines()
        if lines and lines[0].startswith("```"):
            lines = lines[1:]
        if lines and lines[-1].startswith("```"):
            lines = lines[:-1]
        stripped = "\n".join(lines).strip()

    try:
        return json.loads(stripped)
    except json.JSONDecodeError:
        start = stripped.find("{")
        end = stripped.rfind("}")
        if start == -1 or end <= start:
            raise
        return json.loads(stripped[start : end + 1])


def unwrap_oracle_payload(data: object) -> dict:
    if isinstance(data, dict) and "brief_description" in data and "ai_baseline_hours" in data:
        return data

    if isinstance(data, dict) and "result" in data:
        result = data.get("result")
        if isinstance(result, dict):
            return unwrap_oracle_payload(result)
        if isinstance(result, str):
            return unwrap_oracle_payload(parse_json_text(result))

    raise ValueError("oracle output does not contain a task estimate")


def normalize_oracle_payload(payload: dict) -> dict:
    description = payload.get("brief_description")
    if not isinstance(description, str) or not description.strip():
        raise ValueError("brief_description is missing")
    description = " ".join(description.strip().split())

    try:
        hours = float(payload.get("ai_baseline_hours"))
    except (TypeError, ValueError) as exc:
        raise ValueError("ai_baseline_hours is not a number") from exc
    if hours < 0:
        raise ValueError("ai_baseline_hours must be non-negative")

    confidence = payload.get("estimation_confidence")
    if confidence not in {"high", "medium", "low"}:
        confidence = "low"

    review_flag = payload.get("needs_manual_review", False)
    if isinstance(review_flag, bool):
        needs_manual_review = review_flag
    elif isinstance(review_flag, str):
        needs_manual_review = review_flag.strip().lower() == "true"
    else:
        needs_manual_review = bool(review_flag)

    return {
        "ai_baseline_hours": hours,
        "human_corrected_hours": None,
        "brief_description": description,
        "estimated_at": datetime.now().astimezone().isoformat(timespec="seconds"),
        "estimation_confidence": confidence,
        "needs_manual_review": needs_manual_review,
    }


def run_oracle(prompt: str) -> dict:
    completed = subprocess.run(
        ["claude", "-p", "--bare", "--output-format", "json"],
        input=prompt,
        text=True,
        encoding="utf-8",
        errors="replace",
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        timeout=60,
    )
    if completed.returncode != 0:
        raise RuntimeError(f"claude exited with {completed.returncode}: {completed.stderr.strip()}")

    payload = unwrap_oracle_payload(parse_json_text(completed.stdout))
    return normalize_oracle_payload(payload)


def failure_entry(transcript_path: str, reason: str) -> dict:
    return {
        "ai_baseline_hours": None,
        "human_corrected_hours": None,
        "brief_description": f"estimation failed: {reason}",
        "estimated_at": datetime.now().astimezone().isoformat(timespec="seconds"),
        "estimation_confidence": "low",
        "needs_manual_review": True,
        "transcript_path": transcript_path,
    }


def estimate_session(session_id: str, transcript_path: str) -> None:
    context = build_truncated_context(transcript_path)
    oracle_prompt = ORACLE_PROMPT_FILE.read_text(encoding="utf-8")
    prompt = oracle_prompt + "\n\n=== TRANSCRIPT (truncated) ===\n" + context
    entry = run_oracle(prompt)
    entry["transcript_path"] = transcript_path
    update_task_entry(session_id, entry)


def main(argv: list[str]) -> int:
    args = parse_args(argv)
    try:
        estimate_session(args.session_id, args.transcript_path)
    except subprocess.TimeoutExpired:
        print("estimation failed: claude timed out", file=sys.stderr)
        update_task_entry(args.session_id, failure_entry(args.transcript_path, "claude timed out"))
    except Exception as exc:
        print(f"estimation failed: {exc}", file=sys.stderr)
        update_task_entry(args.session_id, failure_entry(args.transcript_path, str(exc)))
    finally:
        remove_inflight_lock(args.session_id)

    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
