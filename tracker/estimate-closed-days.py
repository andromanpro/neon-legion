#!/usr/bin/env python
"""Estimate closed calendar days one at a time, independent of whole sessions.

Since 676ff0d the productivity window counts only closed local days that carry
a day estimate (`<session_id>:<YYYY-MM-DD>` in tasks.json); the session is just
the container. Nothing kept those day estimates coming:

* only estimate-task.py::estimate_session cuts days, and the SessionStart hook
  runs it only for sessions without a session estimate, so a session that lives
  for weeks through context compaction never gets its new days estimated;
* that day loop freezes older dates and re-estimates only the latest one, so a
  day estimated while it was still open stays partial after it closes;
* day estimates made before the input fix (941db5b, e6ecac7) saw an empty Codex
  conversation with zero counters, or a compaction summary in place of the
  owner's first prompts.

The queue takes exactly the accounting's unit, a (session, closed local day)
pair with task-metric events in the ledger, so its unestimated days are the
window's `units_unknown`. It hands one day at a time to
`estimate-task.py --day`; a day without a transcript on disk is listed and never
run. Nothing here writes tasks.json: the estimator does, under its lock.

Usage:
    py -3.14 tracker/estimate-closed-days.py --list
    py -3.14 tracker/estimate-closed-days.py --limit 3 --timeout 600
    py -3.14 tracker/estimate-closed-days.py --category missing --limit 10
"""

import argparse
from dataclasses import dataclass
from datetime import date, datetime, timedelta
import importlib.util
import json
import os
from pathlib import Path
import re
import subprocess
import sys
import time

PROJECT_ROOT = Path(__file__).resolve().parents[1]
TRACKER_DIR = PROJECT_ROOT / "tracker"
ESTIMATOR = TRACKER_DIR / "estimate-task.py"
ATTEMPTS_FILE = TRACKER_DIR / ".estimation-logs" / "closed-day-attempts.json"
CLAUDE_PROJECTS_ROOT = Path.home() / ".claude" / "projects"
CODEX_SESSION_ROOTS = (Path.home() / ".codex" / "sessions", Path.home() / ".codex" / "archived_sessions")

for _directory in (PROJECT_ROOT, TRACKER_DIR):
    if str(_directory) not in sys.path:
        sys.path.insert(0, str(_directory))

import summary  # noqa: E402
from transcript_events import is_compact_summary  # noqa: E402

_spec = importlib.util.spec_from_file_location("nl_estimate_task", ESTIMATOR)
estimator = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(estimator)

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8")

# Day estimates written before this moment used the old oracle input: 941db5b
# (2026-09-11 08:45) fixed the text of Codex days and dropped compaction
# summaries, e6ecac7 (08:58) fixed their SESSION SIZE counters. The later commit
# is the boundary, because both halves of the prompt were wrong until then.
DAY_INPUT_FIXED_AT = datetime.fromisoformat("2026-09-11T08:58:20+03:00")

# tools/rechunk_estimate.py read days with its own Claude-only copy of the parser
# until the 1c audit: an unversioned entry of this mode is old input whenever it
# was written.
RECHUNK_ESTIMATION_MODE = "calendar-day-chunk"

# The attempt journal's read-modify-write takes milliseconds; a lock older than
# this was left by a process that died inside it.
ATTEMPTS_LOCK_STALE_SECONDS = 60

# The old input gave the oracle the first three user messages of the day.
OLD_INPUT_FIRST_USER_MESSAGES = 3

# A day whose estimation failed this many times waits for --retry-failed, so a
# day the oracle can never handle does not block the queue behind it.
MAX_DAY_ATTEMPTS = 3

# Within one day: wrong values already inside the numerator first, then absent
# ones. Across days the newest goes first (owner's call, 2026-09-15): the 7- and
# 30-day windows the dashboard shows become complete and honest before old
# history does.
CATEGORIES = ("codex_blind", "compaction_input", "partial_day", "missing")

_UUID_TAIL = re.compile(r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$")


@dataclass
class Candidate:
    session_id: str
    day: str
    provider: str  # "claude" or "codex"
    category: str
    transcript: Path | None
    hours: float | None  # the current effective estimate; None for a missing day
    failures: int = 0

    @property
    def task_key(self) -> str:
        return f"{self.session_id}:{self.day}"


def closed_units(events: list[dict], today: str) -> dict[tuple[str, str], str]:
    """(session_id, local day) -> "claude"/"codex" for every closed day.

    Keyed exactly like summary.summarize_productivity keys its chunks, so the
    unestimated part of this set is the accounting's `units_unknown`.
    """
    units: dict[tuple[str, str], str] = {}
    for event in summary.events_for_task_metrics(events):
        session_id = event.get("session_id")
        if not isinstance(session_id, str) or not session_id:
            continue
        ts = summary.parse_event_ts(event.get("ts"))
        if ts is None:
            continue
        day = summary.chunk_date(ts)
        if day >= today:
            continue
        provider = "codex" if summary.event_provider(event) == "openai" else "claude"
        units.setdefault((session_id, day), provider)
    return units


def parse_time(value: object) -> datetime | None:
    if not isinstance(value, str) or not value:
        return None
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError:
        return None
    return parsed if parsed.tzinfo is not None else parsed.astimezone()


def estimated_at(entry: dict) -> datetime | None:
    return parse_time(entry.get("estimated_at"))


def input_taken_at(entry: dict) -> datetime | None:
    """When the estimate's transcript was read.

    estimated_at is stamped when the oracle answers, which can be after
    midnight. day_input_read_at exists since the 1c audit; older entries have
    only estimated_at.
    """
    return parse_time(entry.get("day_input_read_at")) or estimated_at(entry)


def has_old_input(entry: dict) -> bool:
    version = entry.get("day_input_version")
    if isinstance(version, int) and not isinstance(version, bool) and version >= estimator.DAY_INPUT_VERSION:
        return False
    if entry.get("estimation_mode") == RECHUNK_ESTIMATION_MODE:
        return True
    at = input_taken_at(entry)
    return at is not None and at < DAY_INPUT_FIXED_AT


def estimated_while_open(entry: dict, day: str) -> bool:
    at = input_taken_at(entry)
    return at is not None and at.astimezone().date().isoformat() <= day


def classify(provider: str, day: str, entry: object, compaction_in_input: bool) -> tuple[str | None, float | None]:
    """(category or None, current effective hours) for one closed day."""
    hours = summary.effective_task_hours(entry) if isinstance(entry, dict) else None
    if hours is None:
        return "missing", None
    if has_old_input(entry):
        if provider == "codex":
            return "codex_blind", hours
        if compaction_in_input:
            return "compaction_input", hours
    if estimated_while_open(entry, day):
        return "partial_day", hours
    return None, hours


def old_input_compaction_days(transcript: Path, days: set[str]) -> set[str]:
    """Days whose old oracle input had a compaction summary among its first
    three user messages.

    Rebuilds the reading from before 941db5b on purpose: that day loop and
    tools/rechunk_estimate.py took every user-role line with text as a prompt,
    so a summary counted like one. Tool results carry no text and never did.
    An unreadable transcript flags nothing.
    """
    counted: dict[str, int] = {}
    flagged: set[str] = set()
    try:
        with Path(transcript).open("r", encoding="utf-8", errors="replace") as source:
            for line in source:
                if '"user"' not in line:
                    continue
                try:
                    event = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if not isinstance(event, dict) or estimator.transcript_role(event) != "user":
                    continue
                ts = estimator.parse_transcript_ts(event.get("timestamp") or event.get("ts"))
                if ts is None:
                    continue
                day = estimator.chunk_date(ts)
                if day not in days or counted.get(day, 0) >= OLD_INPUT_FIRST_USER_MESSAGES:
                    continue
                if not estimator.transcript_text(event).strip():
                    continue
                counted[day] = counted.get(day, 0) + 1
                if is_compact_summary(event):
                    flagged.add(day)
    except OSError:
        return set()
    return flagged


def build_queue(events, tasks, today, find_transcript, attempts=None) -> list[Candidate]:
    """Every closed day that needs an estimate: newest day first, then category.

    `find_transcript(session_id, provider)` returns an existing transcript path
    or None; `attempts` is the failure log read by read_attempts.
    """
    attempts = attempts or {}
    units = closed_units(events, today)

    transcripts: dict[str, Path | None] = {}

    def transcript_for(session_id: str, provider: str) -> Path | None:
        if session_id not in transcripts:
            transcripts[session_id] = find_transcript(session_id, provider)
        return transcripts[session_id]

    # Only a Claude day estimated with the old input can hold a summary in that
    # input; each such transcript is read once for all of its suspect days.
    suspects: dict[str, set[str]] = {}
    for (session_id, day), provider in units.items():
        entry = tasks.get(f"{session_id}:{day}")
        if (
            provider == "claude"
            and isinstance(entry, dict)
            and summary.effective_task_hours(entry) is not None
            and has_old_input(entry)
        ):
            suspects.setdefault(session_id, set()).add(day)
    compacted: set[tuple[str, str]] = set()
    for session_id, days in suspects.items():
        transcript = transcript_for(session_id, "claude")
        if transcript is not None:
            compacted.update((session_id, day) for day in old_input_compaction_days(transcript, days))

    candidates = []
    for (session_id, day), provider in units.items():
        key = f"{session_id}:{day}"
        category, hours = classify(provider, day, tasks.get(key), (session_id, day) in compacted)
        if category is None:
            continue
        attempt = attempts.get(key)
        failures = attempt.get("failures") if isinstance(attempt, dict) else 0
        candidates.append(Candidate(
            session_id=session_id,
            day=day,
            provider=provider,
            category=category,
            transcript=transcript_for(session_id, provider),
            hours=hours,
            failures=int(failures or 0),
        ))

    rank = {name: index for index, name in enumerate(CATEGORIES)}
    candidates.sort(key=lambda c: (-date.fromisoformat(c.day).toordinal(), rank[c.category], c.session_id))
    return candidates


def runnable(candidates: list[Candidate], categories=None, retry_failed: bool = False) -> list[Candidate]:
    return [
        c for c in candidates
        if c.transcript is not None
        and (categories is None or c.category in categories)
        and (retry_failed or c.failures < MAX_DAY_ATTEMPTS)
    ]


def read_attempts(path: Path) -> dict:
    try:
        data = json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    return data if isinstance(data, dict) else {}


def write_attempts(path: Path, data: dict) -> None:
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    temp_path = path.with_name(f".{path.name}.tmp.{os.getpid()}")
    temp_path.write_text(json.dumps(data, ensure_ascii=False, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    os.replace(temp_path, path)


def _acquire_lock(lock_path: Path, timeout_seconds: float = 10.0) -> int | None:
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    deadline = time.monotonic() + timeout_seconds
    while True:
        try:
            fd = os.open(str(lock_path), os.O_CREAT | os.O_EXCL | os.O_WRONLY)
            os.write(fd, str(os.getpid()).encode("ascii"))
            return fd
        except FileExistsError:
            try:
                if time.time() - lock_path.stat().st_mtime > ATTEMPTS_LOCK_STALE_SECONDS:
                    lock_path.unlink()
                    continue
            except OSError:
                pass
            if time.monotonic() >= deadline:
                return None
            time.sleep(0.05)
        except OSError:
            return None


def record_attempt(path: Path, task_key: str, error: str | None) -> None:
    """Count a failed attempt (error given) or clear the day after a success.

    One key per read-modify-write, under a lock file. The journal used to be
    read once per run and written back whole, dropping what an overlapping run
    had recorded (1c audit, P2).
    """
    path = Path(path)
    lock_path = path.with_name(path.name + ".lock")
    fd = _acquire_lock(lock_path)
    if fd is None:
        print(f"[closed-days] журнал попыток занят, отметка {task_key} пропущена", file=sys.stderr)
        return
    try:
        attempts = read_attempts(path)
        if error is None:
            if attempts.pop(task_key, None) is None:
                return
        else:
            previous = attempts.get(task_key)
            failures = previous.get("failures") if isinstance(previous, dict) else 0
            attempts[task_key] = {
                "failures": int(failures or 0) + 1,
                "last_failed_at": datetime.now().astimezone().isoformat(timespec="seconds"),
                "last_error": error[-160:],
            }
        write_attempts(path, attempts)
    finally:
        os.close(fd)
        try:
            lock_path.unlink()
        except OSError:
            pass


def landed_now(entry: object, started: datetime) -> bool:
    """True when the estimator wrote a day estimate during this attempt."""
    if not isinstance(entry, dict) or entry.get("ai_baseline_hours") is None:
        return False
    at = estimated_at(entry)
    return (
        entry.get("day_input_version") == estimator.DAY_INPUT_VERSION
        and at is not None
        and at >= started
    )


def subprocess_runner(candidate: Candidate, timeout: int) -> tuple[int | None, str]:
    """Run `estimate-task.py --day`; (returncode, stderr tail), returncode None on timeout."""
    try:
        completed = subprocess.run(
            [
                "py", "-3.14", str(ESTIMATOR),
                "--day", candidate.day,
                "--reason", candidate.category,
                candidate.session_id, str(candidate.transcript),
            ],
            cwd=str(PROJECT_ROOT),
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=timeout,
        )
    except subprocess.TimeoutExpired:
        return None, f"таймаут {timeout}s"
    return completed.returncode, completed.stderr.strip()[-160:]


def run_queue(candidates, runner, read_tasks, attempts_path, timeout, out=print) -> dict:
    """Estimate the candidates in order; {"ok", "partial", "failed"} counts.

    Success is judged by what landed in tasks.json, not by the exit code alone:
    "partial" means a fresh estimate landed but the run was cut short.
    """
    counts = {"ok": 0, "partial": 0, "failed": 0}
    for index, candidate in enumerate(candidates, start=1):
        label = f"[{index}/{len(candidates)}] {candidate.session_id[:8]} {candidate.day} {candidate.category}"
        started = datetime.now().astimezone().replace(microsecond=0)
        returncode, error = runner(candidate, timeout)
        entry = read_tasks().get(candidate.task_key)
        if returncode is None:
            interruption = error or f"таймаут {timeout}s"
        elif returncode != 0:
            interruption = f"rc={returncode} {error}".strip()
        else:
            interruption = ""
        suffix = f"; {interruption}" if interruption else ""
        was = f" (было {candidate.hours} ч)" if candidate.hours is not None else ""

        if landed_now(entry, started):
            record_attempt(attempts_path, candidate.task_key, None)
            if interruption:
                counts["partial"] += 1
                out(f"{label} ⚠ {entry['ai_baseline_hours']} ч{was}, прогон прерван{suffix}")
            else:
                counts["ok"] += 1
                out(f"{label} ✓ {entry['ai_baseline_hours']} ч{was}")
        else:
            counts["failed"] += 1
            record_attempt(attempts_path, candidate.task_key, interruption or "оценка не записана")
            out(f"{label} ✗ оценка не записана{suffix}")
    return counts


def transcript_finder(tasks: dict, claude_root: Path = CLAUDE_PROJECTS_ROOT, codex_roots=CODEX_SESSION_ROOTS):
    """find(session_id, provider) -> an existing transcript Path, or None.

    The stored transcript_path wins while it still exists; otherwise the file is
    looked up by name: `<session_id>.jsonl` under ~/.claude/projects, or a Codex
    rollout whose name ends with the session id.
    """
    indexes: dict[str, dict[str, Path]] = {}

    def index(provider: str) -> dict[str, Path]:
        if provider not in indexes:
            found: dict[str, Path] = {}
            if provider == "codex":
                for root in codex_roots:
                    if root.exists():
                        for path in root.rglob("rollout-*.jsonl"):
                            match = _UUID_TAIL.search(path.stem)
                            if match:
                                found.setdefault(match.group(0), path)
            elif claude_root.exists():
                for path in claude_root.glob("*/*.jsonl"):
                    found.setdefault(path.stem, path)
            indexes[provider] = found
        return indexes[provider]

    def find(session_id: str, provider: str) -> Path | None:
        entry = tasks.get(session_id)
        stored = summary.resolve_transcript_path(entry.get("transcript_path")) if isinstance(entry, dict) else None
        return stored or index(provider).get(session_id)

    return find


def collect(args: argparse.Namespace):
    """(ledger events, tasks, today, transcript finder) from the live tracker."""
    today = summary.current_local_date()
    start = date.fromisoformat(args.since) if args.since else date(2000, 1, 1)
    end = date.fromisoformat(today) - timedelta(days=1)
    tasks = summary.read_tasks()
    return summary.read_events(start, end), tasks, today, transcript_finder(tasks)


def plural_days(count: int) -> str:
    tail = abs(count) % 100
    if 11 <= tail <= 14:
        return "дней"
    if tail % 10 == 1:
        return "день"
    if 2 <= tail % 10 <= 4:
        return "дня"
    return "дней"


def print_overview(candidates: list[Candidate], queue: list[Candidate]) -> None:
    by_category = {name: 0 for name in CATEGORIES}
    for candidate in queue:
        by_category[candidate.category] += 1
    no_transcript = sum(c.transcript is None for c in candidates)
    deferred = sum(c.transcript is not None and c.failures >= MAX_DAY_ATTEMPTS for c in candidates)
    parts = ", ".join(f"{name} {count}" for name, count in by_category.items())
    print(f"[closed-days] в очереди {len(queue)}: {parts}; без транскрипта {no_transcript}, "
          f"отложено после {MAX_DAY_ATTEMPTS} неудач {deferred}")


def print_details(candidates: list[Candidate], queue: list[Candidate], today: str, show: int) -> None:
    print("[closed-days] оценённые дни под подозрением (текущие часы):")
    for name in CATEGORIES[:-1]:
        group = [c for c in candidates if c.category == name]
        hours = sum(c.hours or 0.0 for c in group)
        lost = sum(c.transcript is None for c in group)
        note = f", без транскрипта {lost}" if lost else ""
        print(f"  {name:<17} {len(group):>4} {plural_days(len(group))}, {hours:.1f} ч{note}")
    missing = [c for c in candidates if c.category == "missing"]
    lost = sum(c.transcript is None for c in missing)
    print(f"[closed-days] без оценки: {len(missing)} {plural_days(len(missing))}, "
          f"из них без транскрипта {lost}")
    today_date = date.fromisoformat(today)
    windows = []
    for days in (7, 30):
        start = (today_date - timedelta(days=days - 1)).isoformat()
        windows.append(f"{days}д {sum(c.day >= start for c in missing)}")
    print(f"  в окнах дашборда (= units_unknown): {', '.join(windows)}")
    if queue:
        print(f"[closed-days] первые {min(show, len(queue))} в очереди:")
        for candidate in queue[:show]:
            hours = f"{candidate.hours} ч" if candidate.hours is not None else "—"
            print(f"  {candidate.category:<17} {candidate.session_id[:8]}  {candidate.day}  {hours}")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--list", action="store_true", help="Show the queue; estimate and write nothing.")
    parser.add_argument("--limit", type=int, help="Estimate at most N days in this run.")
    parser.add_argument("--timeout", type=int, default=600, help="Per-day timeout, seconds.")
    parser.add_argument("--category", action="append", choices=CATEGORIES,
                        help="Only this category (repeatable).")
    parser.add_argument("--since", help="Only days on/after YYYY-MM-DD.")
    parser.add_argument("--retry-failed", action="store_true",
                        help=f"Include days that already failed {MAX_DAY_ATTEMPTS} times.")
    parser.add_argument("--show", type=int, default=10, help="With --list: how many queued days to print.")
    args = parser.parse_args(argv)

    events, tasks, today, find_transcript = collect(args)
    candidates = build_queue(events, tasks, today, find_transcript, attempts=read_attempts(ATTEMPTS_FILE))
    queue = runnable(candidates, categories=set(args.category) if args.category else None,
                     retry_failed=args.retry_failed)

    print_overview(candidates, queue)
    if args.list:
        print_details(candidates, queue, today, args.show)
        return 0
    if args.limit is not None:
        queue = queue[: max(args.limit, 0)]
    if not queue:
        print("[closed-days] нечего оценивать")
        return 0

    counts = run_queue(queue, subprocess_runner, summary.read_tasks, ATTEMPTS_FILE, args.timeout)
    print(f"[closed-days] готово: полностью {counts['ok']}, частично {counts['partial']}, "
          f"не вышло {counts['failed']}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
