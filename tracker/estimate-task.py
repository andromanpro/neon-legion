#!/usr/bin/env python
import argparse
import json
import os
import re
import subprocess
import sys
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path


PROJECT_ROOT = Path(__file__).resolve().parents[1]
TRACKER_DIR = PROJECT_ROOT / "tracker"
TASKS_FILE = TRACKER_DIR / "tasks.json"
TASKS_LOCK_FILE = TRACKER_DIR / ".tasks.lock"
LOG_DIR = TRACKER_DIR / ".estimation-logs"
ORACLE_PROMPT_FILE = TRACKER_DIR / "oracle-prompt.txt"
MAX_CONTEXT_CHARS = 15_000
SESSION_SIZE_GAP_MINUTES = 2

def chunk_date(ts: datetime) -> str:
    """Calendar-day key for per-day chunk task ids. MUST stay byte-identical
    to tracker/summary.chunk_date — producer (this hook) and the consumer
    (summarize_productivity) key on the same string. Replicated as a local
    one-liner instead of importing summary (which transitively pulls
    tools.config) to keep this frequently-spawned hook subprocess light.

    Codex-audit MED: the replica had drifted — summary buckets by the LOCAL
    day (`ts.astimezone().date()`), this one used the timestamp's own zone
    (UTC in transcripts), so a 22:30Z prompt landed in yesterday's chunk key
    and the estimate was never found by the aggregator (baseline silently
    lost for late-evening MSK work)."""
    return ts.astimezone().date().isoformat()

PROFANITY_RU_PATTERNS = [
    re.compile(r"\bбля[а-яё]*", re.IGNORECASE),
    re.compile(r"\bёб[а-яё]*", re.IGNORECASE),
    re.compile(r"\bеб[а-яё]+", re.IGNORECASE),
    # Prefixed ёб-family — `\bеб[а-яё]+` doesn't catch «заебал/выебал»
    # because there's no word boundary inside the compound word.
    re.compile(r"\b(?:за|вы|на|по|подъ|пере)еб[а-яё]+", re.IGNORECASE),
    re.compile(r"\bхуй[а-яё]*|\bхрен[а-яё]*|\bхер[а-яё]*", re.IGNORECASE),
    re.compile(r"\bпизд[а-яё]*", re.IGNORECASE),
    re.compile(r"\bсук[а-яё]*", re.IGNORECASE),
    re.compile(r"\bговн[а-яё]*", re.IGNORECASE),
    re.compile(r"\bсран[а-яё]*|\bсрать", re.IGNORECASE),
    re.compile(r"\bжоп[а-яё]*", re.IGNORECASE),
    re.compile(r"\bнах(уй|ер|рен)[а-яё]*", re.IGNORECASE),
    re.compile(r"\bпошёл\s+на\b|\bпошел\s+на\b", re.IGNORECASE),
]

PROFANITY_EN_PATTERNS = [
    re.compile(r"\bfuck[a-z]*", re.IGNORECASE),
    re.compile(r"\bshit[a-z]*", re.IGNORECASE),
    re.compile(r"\bdamn[a-z]*", re.IGNORECASE),
    re.compile(r"\bbitch[a-z]*", re.IGNORECASE),
    re.compile(r"\bcrap[a-z]*", re.IGNORECASE),
]

ALL_PROFANITY = PROFANITY_RU_PATTERNS + PROFANITY_EN_PATTERNS

# Appreciation markers — symmetric to PROFANITY_* but for positive feedback.
# GENUINE gratitude only — direct verbal thanks + strong unambiguous praise of
# the work. Deliberately TIGHT (2026-05-30 recalibration): the old lexicon
# counted the user's «)» / «))» smiley habit (85% of all hits!), filler/approval
# markers («хорошо», «норм», «отлично» — per profile these mean "proceed", not
# "thanks"), continue-commands («дальше», «продолжай», «keep going»), laughter
# and celebratory emoji. That inflated the count ~250× over real thanks
# (10164 → ~26-40). «Благодарностей» must mean gratitude, not good mood —
# the smiley/momentum/emoji signals belong to a separate "vibe" metric if wanted.
APPRECIATION_RU_PATTERNS = [
    # direct thanks
    re.compile(r"\bспасибо[а-яё]*", re.IGNORECASE),
    re.compile(r"\bблагодар[а-яё]+", re.IGNORECASE),
    # strong, unambiguous praise of the result (not "ok/proceed" markers)
    re.compile(r"\bкрасав[а-яё]*", re.IGNORECASE),
    re.compile(r"\bты\s+(лучший|молодец|гений)\b", re.IGNORECASE),
    re.compile(r"\bобожаю\b", re.IGNORECASE),
]

APPRECIATION_EN_PATTERNS = [
    re.compile(r"\bthanks?\b|\bthank\s+you\b", re.IGNORECASE),
    re.compile(r"\bawesome\b|\bexcellent\b|\bbrilliant\b", re.IGNORECASE),
    re.compile(r"\blove\s+it\b", re.IGNORECASE),
]

# Emoji intentionally excluded — celebratory/momentum emoji (🚀🔥💯🎉) read as
# energy, not gratitude. Kept as a no-match sentinel so the call site stays
# unchanged. 🙏 (prayer hands = thanks) could be re-added if desired.
APPRECIATION_EMOJI_PATTERN = re.compile(
    r"(?!x)x"  # never matches
)

ALL_APPRECIATION = APPRECIATION_RU_PATTERNS + APPRECIATION_EN_PATTERNS

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


# Fields that must NEVER be overwritten by None from a failed estimation:
# they are local-deterministic (profanity) or user-provided (corrections).
_PROTECTED_NONE_FIELDS = frozenset({
    "human_corrected_hours",
    "profanity_count",
})


def update_task_entry(session_id: str, entry: dict) -> None:
    """Merge `entry` into the existing record (if any).

    B1 (Codex review): protected fields are not overwritten by None — when
    a fresh `failure_entry` lacks `human_corrected_hours`, we keep the prior
    value. profanity_count is handled the same way: a fresh oracle failure
    that doesn't carry profanity should not zero out a previously-counted
    value.

    B3: if lock acquisition fails (timeout / OS error), do NOT proceed with
    an unlocked write — refuse and let the caller log/retry.
    """
    fd = acquire_tasks_lock()
    if fd is None:
        raise RuntimeError(
            f"failed to acquire {TASKS_LOCK_FILE} within timeout; refusing unlocked write"
        )
    try:
        tasks = read_tasks()
        previous = tasks.get(session_id)
        if isinstance(previous, dict):
            merged = dict(previous)
            for key, value in entry.items():
                if value is None and key in _PROTECTED_NONE_FIELDS and key in merged:
                    # keep previously-captured value
                    continue
                merged[key] = value
            tasks[session_id] = merged
        else:
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


def parse_transcript_ts(value: object) -> datetime | None:
    if not isinstance(value, str) or not value:
        return None
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        return parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def active_hours_for_timestamps(timestamps: list[datetime], gap_minutes: int = SESSION_SIZE_GAP_MINUTES) -> float:
    if len(timestamps) < 2:
        return 0.0

    max_gap = timedelta(minutes=gap_minutes)
    timestamps = sorted(timestamps)
    total_seconds = 0.0
    previous = timestamps[0]
    for current in timestamps[1:]:
        gap = current - previous
        if gap <= max_gap:
            total_seconds += gap.total_seconds()
        previous = current

    return total_seconds / 3600


def _event_content_blocks(event: dict) -> list:
    message = event.get("message")
    if isinstance(message, dict):
        content = message.get("content")
    else:
        content = event.get("content")
    return content if isinstance(content, list) else []


def _tool_call_count_for_event(event: dict, role: str | None) -> int:
    tool_blocks = 0
    if role == "assistant":
        for block in _event_content_blocks(event):
            if isinstance(block, dict) and block.get("type") == "tool_use":
                tool_blocks += 1
    if tool_blocks:
        return tool_blocks
    if event.get("toolUseID") is not None:
        return 1
    return 0


def compute_session_metrics(transcript_path) -> dict:
    metrics = {
        "event_count": 0,
        "user_message_count": 0,
        "assistant_message_count": 0,
        "tool_call_count": 0,
        "span_hours": 0.0,
        "active_hours": 0.0,
    }
    timestamps: list[datetime] = []
    first_ts = None
    last_ts = None

    try:
        source = Path(transcript_path).open("r", encoding="utf-8")
    except Exception:
        return metrics

    try:
        with source:
            for line in source:
                if not line.strip():
                    continue
                try:
                    event = json.loads(line)
                except json.JSONDecodeError:
                    continue

                metrics["event_count"] += 1
                if not isinstance(event, dict):
                    continue

                try:
                    role = transcript_role(event)
                    if role == "user":
                        metrics["user_message_count"] += 1
                    elif role == "assistant":
                        metrics["assistant_message_count"] += 1

                    metrics["tool_call_count"] += _tool_call_count_for_event(event, role)

                    ts = parse_transcript_ts(event.get("timestamp") or event.get("ts"))
                    if ts is not None:
                        if first_ts is None:
                            first_ts = ts
                        last_ts = ts
                        timestamps.append(ts)
                except Exception:
                    continue
    except (OSError, UnicodeDecodeError):
        pass

    if first_ts is not None and last_ts is not None:
        span_seconds = max((last_ts - first_ts).total_seconds(), 0.0)
        metrics["span_hours"] = span_seconds / 3600

    try:
        metrics["active_hours"] = active_hours_for_timestamps(timestamps, SESSION_SIZE_GAP_MINUTES)
    except Exception:
        metrics["active_hours"] = 0.0

    return metrics


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

    return user_messages, assistant_messages


def count_profanity(user_messages: list[str]) -> int:
    """Count profanity matches across user messages without storing raw examples."""
    total = 0
    for message in user_messages:
        for pattern in ALL_PROFANITY:
            total += len(pattern.findall(message))
    return total


def count_appreciation(user_messages: list[str]) -> int:
    """Count appreciation markers in user messages — symmetric to count_profanity.

    Tightened lexicon (fixes the 10164→30 over-count): ONLY genuine gratitude
    and unambiguous praise count — «спасибо/благодарю/красав/обожаю/ты лучший»,
    thanks/awesome/excellent/brilliant/love it. Bare acks («отлично», «круто»),
    momentum markers, laughter/«))» smileys, emoji, and the old
    profanity-as-positive carve-out are deliberately NOT counted — they signal
    "proceed"/energy, not thanks, and used to swamp the real signal.
    """
    total = 0
    for message in user_messages:
        for pattern in ALL_APPRECIATION:
            total += len(pattern.findall(message))
        total += len(APPRECIATION_EMOJI_PATTERN.findall(message))
    return total


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


def build_truncated_context_from_messages(user_messages: list[str], assistant_messages: list[str]) -> str:
    context = render_context(user_messages[:3], assistant_messages[-5:], 2_500)
    if len(context) > MAX_CONTEXT_CHARS:
        context = render_context(user_messages[:3], assistant_messages[-5:], 1_200)
    return clip_middle(context, MAX_CONTEXT_CHARS)


def build_truncated_context(transcript_path: str) -> str:
    user_messages, assistant_messages = read_transcript(Path(transcript_path))
    return build_truncated_context_from_messages(user_messages, assistant_messages)


def format_session_size_block(metrics: dict) -> str:
    return (
        "=== SESSION SIZE (ground truth — the transcript below is TRUNCATED to a few messages; "
        "trust THESE numbers for scale, not the transcript length) ===\n"
        f"events={int(metrics.get('event_count') or 0)}  "
        f"user_msgs={int(metrics.get('user_message_count') or 0)}  "
        f"assistant_msgs={int(metrics.get('assistant_message_count') or 0)}  "
        f"tool_calls={int(metrics.get('tool_call_count') or 0)}  "
        f"span_hours={float(metrics.get('span_hours') or 0.0):.3f}  "
        f"active_hours={float(metrics.get('active_hours') or 0.0):.3f}"
    )


def build_estimation_prompt(context: str, metrics: dict) -> str:
    oracle_prompt = ORACLE_PROMPT_FILE.read_text(encoding="utf-8")
    return (
        oracle_prompt
        + "\n\n"
        + format_session_size_block(metrics)
        + "\n\n=== TRANSCRIPT (truncated) ===\n"
        + context
    )


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

    try:
        frustration = float(payload.get("frustration_score", 0))
    except (TypeError, ValueError):
        frustration = 0.0
    frustration = max(0.0, min(1.0, frustration))

    try:
        appreciation = float(payload.get("appreciation_score", 0))
    except (TypeError, ValueError):
        appreciation = 0.0
    appreciation = max(0.0, min(1.0, appreciation))

    mood_arc = payload.get("mood_arc", "")
    if not isinstance(mood_arc, str):
        mood_arc = ""
    mood_arc = mood_arc[:30]

    intensity = payload.get("sentiment_intensity", "low")
    if intensity not in {"low", "medium", "high"}:
        intensity = "low"

    return {
        "ai_baseline_hours": hours,
        "human_corrected_hours": None,
        "brief_description": description,
        "estimated_at": datetime.now().astimezone().isoformat(timespec="seconds"),
        "estimation_confidence": confidence,
        "needs_manual_review": needs_manual_review,
        "frustration_score": frustration,
        "appreciation_score": appreciation,
        "mood_arc": mood_arc,
        "sentiment_intensity": intensity,
    }


def _resolve_codex_cli() -> str:
    """Find the Codex CLI binary. On Windows the npm shim is `codex.cmd` —
    shutil.which handles PATHEXT lookup correctly."""
    import shutil
    found = shutil.which("codex")
    if found is None:
        raise RuntimeError(
            "codex CLI not found in PATH (looked for codex/.cmd/.exe). "
            "Install: npm i -g @openai/codex-cli"
        )
    return found


def run_oracle(prompt: str) -> dict:
    """Run the oracle prompt through `codex exec` (ChatGPT-auth headless).

    Why Codex and not Claude: `claude -p` headless requires an Anthropic API
    key, which Max-subscription users don't have by default. Codex CLI under
    ChatGPT-auth works headless from any subprocess without extra setup.
    """
    codex = _resolve_codex_cli()
    import tempfile
    tmp_out = tempfile.NamedTemporaryFile("w", encoding="utf-8", delete=False, suffix=".txt")
    tmp_out.close()
    try:
        # Pass prompt via stdin — multi-line argv through codex.CMD shim on Windows
        # gets eaten at the first newline (cmd.exe quirk). stdin is reliable.
        completed = subprocess.run(
            [
                codex, "exec",
                "--sandbox", "read-only",
                "--skip-git-repo-check",
                "--output-last-message", tmp_out.name,
            ],
            input=prompt,
            text=True,
            encoding="utf-8",
            errors="replace",
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            timeout=180,
        )
        if completed.returncode != 0:
            raise RuntimeError(f"codex exited with {completed.returncode}: {completed.stderr.strip()[:300]}")

        last_message = Path(tmp_out.name).read_text(encoding="utf-8").strip()
        if not last_message:
            raise RuntimeError("codex returned empty last message")

        payload = unwrap_oracle_payload(parse_json_text(last_message))
        return normalize_oracle_payload(payload)
    finally:
        try:
            Path(tmp_out.name).unlink(missing_ok=True)
        except Exception:
            pass


def failure_entry(transcript_path: str, reason: str, profanity: int | None = None) -> dict:
    """Failure record. Preserves profanity counter when known — it's a local
    deterministic count that doesn't depend on the oracle call succeeding."""
    entry = {
        "ai_baseline_hours": None,
        "human_corrected_hours": None,
        "brief_description": f"estimation failed: {reason}",
        "estimated_at": datetime.now().astimezone().isoformat(timespec="seconds"),
        "estimation_confidence": "low",
        "needs_manual_review": True,
        "transcript_path": transcript_path,
    }
    if profanity is not None:
        entry["profanity_count"] = profanity
    return entry


def estimate_session(session_id: str, transcript_path: str) -> None:
    user_messages, assistant_messages = read_transcript(Path(transcript_path))
    # Count profanity first — local, never fails. Persist it even if oracle dies later.
    profanity = count_profanity(user_messages)
    update_task_entry(session_id, {
        "transcript_path": transcript_path,
        "profanity_count": profanity,
    })

    metrics = compute_session_metrics(transcript_path)
    context = build_truncated_context_from_messages(user_messages, assistant_messages)
    prompt = build_estimation_prompt(context, metrics)
    entry = run_oracle(prompt)
    entry["transcript_path"] = transcript_path
    entry["profanity_count"] = profanity
    update_task_entry(session_id, entry)

    chunks: dict[str, list[tuple[dict, datetime]]] = {}
    try:
        with Path(transcript_path).open("r", encoding="utf-8") as source:
            for line in source:
                if not line.strip():
                    continue
                try:
                    event = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if not isinstance(event, dict):
                    continue

                ts = parse_transcript_ts(event.get("timestamp") or event.get("ts"))
                if ts is None:
                    continue
                chunks.setdefault(chunk_date(ts), []).append((event, ts))
    except (OSError, UnicodeDecodeError) as exc:
        print(f"chunk-estimate-failed\t{session_id}\t<read>\t{exc}", file=sys.stderr)
        return

    if not chunks:
        return

    latest_date = max(chunks)
    tasks = read_tasks()

    def has_numeric_baseline(task_key: str) -> bool:
        previous = tasks.get(task_key)
        if not isinstance(previous, dict):
            return False
        baseline = previous.get("ai_baseline_hours")
        if baseline is None or isinstance(baseline, bool):
            return False
        try:
            float(baseline)
        except (TypeError, ValueError):
            return False
        return True

    for date_key in sorted(chunks):
        task_key = f"{session_id}:{date_key}"
        if date_key < latest_date and has_numeric_baseline(task_key):
            continue

        try:
            event_pairs = chunks[date_key]
            events = [item[0] for item in event_pairs]
            timestamps = [item[1] for item in event_pairs]
            chunk_user_messages: list[str] = []
            chunk_assistant_messages: list[str] = []
            chunk_metrics = {
                "event_count": len(events),
                "user_message_count": 0,
                "assistant_message_count": 0,
                "tool_call_count": 0,
                "span_hours": 0.0,
                "active_hours": 0.0,
            }

            for event in events:
                role = transcript_role(event)
                if role == "user":
                    chunk_metrics["user_message_count"] += 1
                elif role == "assistant":
                    chunk_metrics["assistant_message_count"] += 1
                chunk_metrics["tool_call_count"] += _tool_call_count_for_event(event, role)

                if role is None:
                    continue
                text = transcript_text(event).strip()
                if not text:
                    continue
                if role == "user":
                    chunk_user_messages.append(text)
                elif role == "assistant":
                    chunk_assistant_messages.append(text)

            if timestamps:
                ordered = sorted(timestamps)
                chunk_metrics["span_hours"] = max(
                    (ordered[-1] - ordered[0]).total_seconds(),
                    0.0,
                ) / 3600
                chunk_metrics["active_hours"] = active_hours_for_timestamps(
                    timestamps,
                    SESSION_SIZE_GAP_MINUTES,
                )

            chunk_context = build_truncated_context_from_messages(
                chunk_user_messages,
                chunk_assistant_messages,
            )
            chunk_prompt = build_estimation_prompt(chunk_context, chunk_metrics)
            chunk_entry = run_oracle(chunk_prompt)
            for sentiment_key in (
                "frustration_score",
                "appreciation_score",
                "mood_arc",
                "sentiment_intensity",
            ):
                chunk_entry.pop(sentiment_key, None)
            chunk_entry["transcript_path"] = transcript_path
            chunk_entry["source_session_id"] = session_id
            chunk_entry["chunk_date"] = date_key
            chunk_entry["chunk_event_count"] = len(events)
            chunk_entry["estimation_mode"] = "calendar-day-chunk-live"
            update_task_entry(task_key, chunk_entry)
            tasks[task_key] = chunk_entry
        except Exception as exc:
            print(f"chunk-estimate-failed\t{session_id}\t{date_key}\t{exc}", file=sys.stderr)
            continue


def _safe_count_profanity(transcript_path: str) -> int | None:
    """Best-effort profanity count for the failure path. Returns None on read errors."""
    try:
        user_messages, _ = read_transcript(Path(transcript_path))
        return count_profanity(user_messages)
    except Exception:
        return None


def main(argv: list[str]) -> int:
    args = parse_args(argv)
    try:
        estimate_session(args.session_id, args.transcript_path)
    except subprocess.TimeoutExpired:
        print("estimation failed: codex timed out", file=sys.stderr)
        prof = _safe_count_profanity(args.transcript_path)
        update_task_entry(args.session_id, failure_entry(args.transcript_path, "codex timed out", prof))
    except Exception as exc:
        print(f"estimation failed: {exc}", file=sys.stderr)
        prof = _safe_count_profanity(args.transcript_path)
        update_task_entry(args.session_id, failure_entry(args.transcript_path, str(exc), prof))
    finally:
        remove_inflight_lock(args.session_id)

    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
