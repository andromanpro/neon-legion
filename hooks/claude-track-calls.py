#!/usr/bin/env python
"""Stop hook: records every billable assistant message of the finished turn.

Until 2026-08-18 this hook kept only the *last* assistant message in the
transcript, so a turn that ran N tool calls contributed one ledger entry
instead of N. Measured across the sessions whose transcripts were still on
disk: 1160 of 84893 calls never reached the ledger from the hook, and 1104
of those sat in the three busiest sessions. A later `backfill.py` pass
re-reads the transcripts and heals most of it — but only while the
transcript still exists, so for a session that rotates away first the loss
is permanent.

Collection contract:

* identity  — (provider, session_id, message_uuid), carried in `event_id`.
              `readmodel._event_dedupe_group` prefers `event_id` over its
              legacy tuple, and that tuple contains `ts` — which the hook
              and the backfill stamp differently for the same message. So
              without `event_id` a replay counts the same call twice.
* time      — the transcript's own timestamp, never the hook's wall clock.
              A turn that starts before and ends after midnight used to
              land in the wrong day.
* watermark — `.last-uuids.json` is a cursor, not state: losing it re-emits
              rather than loses, because the receiver dedupes on `event_id`.
* failure   — never raises into the session, but never fails silently
              either: problems go to the ops channel (`ops-events.jsonl`),
              which is alerted on and never summed.
"""
import ctypes
import json
import os
import sys
import time
from datetime import datetime
from pathlib import Path

# Force UTF-8 on stdin/stdout/stderr (Windows default is cp1251).
# Without this, Cyrillic paths in `cwd` come through as mojibake when
# Claude Code's JSON arrives on a Windows console default codepage. Once
# stored in JSONL as UTF-8, the mojibake is permanent (#20). The fallback
# `errors='replace'` ensures malformed input doesn't crash the hook.
try:
    sys.stdin.reconfigure(encoding="utf-8", errors="replace")
    sys.stdout.reconfigure(encoding="utf-8")
    sys.stderr.reconfigure(encoding="utf-8")
except AttributeError:
    pass


PROJECT_ROOT = Path(__file__).resolve().parents[1]
TRACKER_DIR = PROJECT_ROOT / "tracker"
EVENTS_FILE = TRACKER_DIR / "claude-events.jsonl"
OPS_FILE = TRACKER_DIR / "ops-events.jsonl"
LAST_UUIDS_FILE = TRACKER_DIR / ".last-uuids.json"
LOCK_FILE = TRACKER_DIR / ".claude-events.lock"

# A crashed writer used to disable collection indefinitely: the lock file
# stayed behind and every later run bailed out on FileExistsError. Found
# live on 2026-08-18 — pid 17456 died holding it and the ledger stopped
# growing. A dead holder is evicted immediately, so this cap only applies
# to a lock whose pid cannot be read at all.
LOCK_STALE_SECONDS = 600

# Where Claude Code parks subagent transcripts, relative to a session's own:
# `<session>.jsonl` alongside `<session>/subagents/[workflows/<wf>/]agent-*.jsonl`.
SUBAGENT_DIR_NAME = "subagents"

# Per-1M-token USD list rates (standard API, verified against the platform
# pricing page 2026-08-18). Derived from base input: cache read = 0.1x,
# a 5-minute cache write = 1.25x, a 1-HOUR cache write = 2x.
#
# The 1h rate is why this table has two write columns. Everything used to be
# charged at the 5-minute rate — and 95.1% of the cache-creation tokens ever
# written by these sessions carry a 1-hour TTL, so the cache line came out
# roughly a third short (~$3.8k over the transcripts still on disk alone).
#
# Opus 4.7/4.8/5 are all $5/$25 standard — the earlier $15/$75 was legacy
# Claude-3-Opus and inflated the "$ saved" headline ~3x.
OPUS_PRICING = {"in": 5.00, "out": 25.00, "cache_read": 0.50, "cache_write_5m": 6.25, "cache_write_1h": 10.00}
SONNET_PRICING = {"in": 3.00, "out": 15.00, "cache_read": 0.30, "cache_write_5m": 3.75, "cache_write_1h": 6.00}
HAIKU_PRICING = {"in": 1.00, "out": 5.00, "cache_read": 0.10, "cache_write_5m": 1.25, "cache_write_1h": 2.00}
# Fable 5 ($10/$50) — 2x standard Opus 4.8. Previously unrecognized → priced at
# $0 with an unknown-pricing flag, so Fable work showed as "didn't contribute".
FABLE_PRICING = {"in": 10.00, "out": 50.00, "cache_read": 1.00, "cache_write_5m": 12.50, "cache_write_1h": 20.00}


def pricing_for_model(model: str) -> dict | None:
    # Family prefixes, not version-pinned ones. Version-pinned matching burned us
    # twice: fable-5 (invisible until added) and opus-5 (13.6k events / 6.6B tokens
    # billed $0 for a week because "claude-opus-5" didn't match "claude-opus-4").
    # A family-rate estimate for a future version beats a silent zero; if Anthropic
    # reprices a family, update the map and recost.py the history.
    if not model:
        return None
    # Opus 4.x and 5 share the $5/$25 rate (verified 2026-07-31, platform docs).
    if model.startswith("claude-opus-"):
        return OPUS_PRICING
    # Sonnet 4.x and 5 share the $3/$15 standard rate.
    if model.startswith("claude-sonnet-"):
        return SONNET_PRICING
    if model.startswith("claude-haiku-"):
        return HAIKU_PRICING
    # Fable/Mythos tier: $10/$50.
    if model.startswith("claude-fable-") or model.startswith("claude-mythos-"):
        return FABLE_PRICING
    return None


def record_ops(code: str, detail: str, component: str = "claude-track-calls") -> None:
    """Write an operational signal — something to alert on, never to sum.

    Deliberately a separate file: readmodel ingests an explicit list of
    `*-events.jsonl` ledgers and this is not one of them. Before this
    existed, a collector that stopped working looked exactly like a quiet
    week.

    Shared by the other hooks through this module, hence `component`.
    """
    try:
        TRACKER_DIR.mkdir(parents=True, exist_ok=True)
        line = json.dumps(
            {
                "ts": datetime.now().astimezone().isoformat(timespec="milliseconds"),
                "component": component,
                "code": code,
                "detail": detail,
                "pid": os.getpid(),
            },
            ensure_ascii=False,
            separators=(",", ":"),
        ) + "\n"
        with OPS_FILE.open("a", encoding="utf-8", newline="\n") as target:
            target.write(line)
    except Exception:
        pass


def read_hook_input() -> dict | None:
    raw = sys.stdin.read()
    if not raw.strip():
        return None

    data = json.loads(raw)
    if not isinstance(data, dict):
        return None

    session_id = data.get("session_id")
    transcript_path = data.get("transcript_path")
    working_dir = data.get("cwd")
    if not isinstance(session_id, str) or not isinstance(transcript_path, str) or not isinstance(working_dir, str):
        return None

    return {
        "session_id": session_id,
        "transcript_path": transcript_path,
        "working_dir": working_dir,
    }


def agent_id_of(transcript_path: Path) -> str | None:
    """Which subagent stream a transcript belongs to, None for a main one.

    The whole relative path under `subagents/`, not just the file stem:
    inside one session `subagents/agent-X` and
    `subagents/workflows/wf-Y/agent-X` are different streams that can share
    a stem, and collapsing them would let one cursor hide the other's work.
    """
    parts = transcript_path.parts
    if SUBAGENT_DIR_NAME not in parts:
        return None
    start = len(parts) - parts[::-1].index(SUBAGENT_DIR_NAME)
    tail = list(parts[start:])
    if not tail:
        return None
    tail[-1] = Path(tail[-1]).stem
    return "/".join(tail)


def session_dir_of(transcript_path: Path) -> Path | None:
    """The `<project>/<session>` directory an agent transcript sits under.

    Used as the fallback when a record carries no `sessionId` of its own:
    the directory holding `subagents/` is named after the parent session,
    and the one above it after the project.
    """
    parts = transcript_path.parts
    if SUBAGENT_DIR_NAME not in parts:
        return None
    index = len(parts) - 1 - parts[::-1].index(SUBAGENT_DIR_NAME)
    return Path(*parts[:index]) if index > 0 else None


def stream_key(session_id: str, agent_id: str | None = None) -> str:
    """Cursor identity of one append-ordered stream of records.

    A session's main transcript is one stream; every subagent transcript
    beneath it is another. Both writers key `.last-uuids.json` with this.
    Keying a subagent's records by the bare parent session id instead would
    park an agent uuid on the main cursor, and the next Stop hook — not
    finding that uuid in the main transcript — would re-emit the whole file
    every single turn.
    """
    return session_id if not agent_id else f"{session_id}#{agent_id}"


def stream_key_of_event(event: dict) -> str:
    """The stream an already-written ledger event came from."""
    return stream_key(str(event.get("session_id") or ""), event.get("agent_id"))


def subagent_transcripts(main_transcript_path: str | Path) -> list[Path]:
    """Agent transcripts of one session.

    Claude Code parks them in a directory named after the session, beside
    the session's own transcript: `<session>.jsonl` and
    `<session>/subagents/[workflows/<wf>/]agent-<hash>.jsonl`. A subagent
    never fires Stop, so without this sweep its calls reach the ledger only
    if a backfill runs before the transcript is rotated away — 6046 of them
    were sitting uncounted on disk when this was written.
    """
    root = Path(main_transcript_path).with_suffix("") / SUBAGENT_DIR_NAME
    if not root.is_dir():
        return []
    return sorted(root.rglob("agent-*.jsonl"))


def read_billable_assistants(transcript_path: str | Path) -> list[dict]:
    """Every assistant message Anthropic actually billed, in file order.

    A malformed line is skipped rather than fatal: the session is still
    appending to this file while we read it, so a half-written tail line is
    normal — and it used to abort the whole run through the catch-all in
    main().

    `session_id` and `working_dir` are carried per record because agent
    transcripts have no hook input to fall back on: their records name the
    parent session in `sessionId` and the real directory in `cwd`.
    """
    records: list[dict] = []
    path = Path(transcript_path)
    if not path.exists() or not path.is_file():
        return records

    with path.open("r", encoding="utf-8") as transcript:
        for line in transcript:
            if not line.strip():
                continue

            try:
                event = json.loads(line)
            except json.JSONDecodeError:
                continue
            if not isinstance(event, dict) or event.get("type") != "assistant":
                continue

            message = event.get("message")
            if not isinstance(message, dict):
                continue

            if message.get("model") == "<synthetic>":
                continue

            usage = message.get("usage")
            model = message.get("model")
            if not isinstance(usage, dict) or not isinstance(model, str) or not model:
                continue

            message_uuid = event.get("uuid")
            if not isinstance(message_uuid, str) or not message_uuid:
                continue

            session_id = event.get("sessionId")
            working_dir = event.get("cwd")
            records.append({
                "message_uuid": message_uuid,
                "timestamp": event.get("timestamp"),
                "session_id": session_id if isinstance(session_id, str) and session_id else None,
                "working_dir": working_dir if isinstance(working_dir, str) and working_dir else None,
                "message": message,
                "usage": usage,
            })

    return records


def records_since(records: list[dict], watermark: str | None) -> list[dict]:
    """The tail of `records` that follows `watermark`.

    Searches from the end because the cursor normally sits inside the last
    turn, which keeps the scan proportional to what is new rather than to
    the whole session. A watermark that is not in this transcript means the
    cursor was lost or the file was replaced: re-emit everything and let
    the receiver dedupe on event_id — an extra line is recoverable, a
    missing call is not.
    """
    if not watermark:
        return records
    for index in range(len(records) - 1, -1, -1):
        if records[index]["message_uuid"] == watermark:
            return records[index + 1:]
    return records


def as_int(value: object) -> int:
    try:
        return int(value or 0)
    except (TypeError, ValueError):
        return 0


def usage_tokens(usage: dict) -> dict:
    """Token counts as billed, including the cache-write TTL split.

    One definition for both writers: the hook and backfill.py used to pull
    the same fields apart separately, which is how the 1-hour cache rate
    could be missed in both at once.

    `cache_creation_tokens` stays the total, so every existing consumer
    keeps working; the split is additive. What the split does not account
    for is reported as unclassified and priced at the cheaper 5-minute
    rate — understating is the safe direction, and the ops channel says so
    out loud rather than letting it pass silently.
    """
    creation = usage.get("cache_creation")
    has_split = isinstance(creation, dict)
    one_hour = five_minute = 0
    if has_split:
        one_hour = as_int(creation.get("ephemeral_1h_input_tokens"))
        five_minute = as_int(creation.get("ephemeral_5m_input_tokens"))

    total_creation = as_int(usage.get("cache_creation_input_tokens"))
    # No split at all is the older usage shape, not a surprise — it is priced
    # the way it always was. A split that does not add up IS a surprise: some
    # third TTL exists and we are guessing at its rate.
    unclassified = max(total_creation - one_hour - five_minute, 0) if has_split else 0
    return {
        "input_tokens": as_int(usage.get("input_tokens")),
        "output_tokens": as_int(usage.get("output_tokens")),
        "cache_creation_tokens": total_creation,
        "cache_creation_1h_tokens": min(one_hour, total_creation),
        "cache_creation_5m_tokens": min(five_minute, total_creation),
        "cache_read_tokens": as_int(usage.get("cache_read_input_tokens")),
        "unclassified_cache_creation_tokens": unclassified,
    }


def estimate_cost(
    model: str,
    input_tokens: int,
    output_tokens: int,
    cache_creation_tokens: int,
    cache_read_tokens: int,
    cache_creation_1h_tokens: int = 0,
) -> float | None:
    """USD for one call. `cache_creation_tokens` is the total written; the
    1-hour part of it costs 2x base input and the rest 1.25x.

    A caller that does not know the split gets the old, cheaper answer —
    the same understatement as before rather than a new overstatement.

    Not rounded to cents: the result is summed over hundreds of thousands
    of events, and rounding each one to four places first put up to $13.50
    of drift into the total while quietly flooring the smallest calls to
    zero. Ten places is float noise, not money.
    """
    pricing = pricing_for_model(model)
    if pricing is None:
        return None

    one_hour = min(max(cache_creation_1h_tokens, 0), max(cache_creation_tokens, 0))
    short_lived = max(cache_creation_tokens - one_hour, 0)
    cost = (
        input_tokens * pricing["in"]
        + output_tokens * pricing["out"]
        + cache_read_tokens * pricing["cache_read"]
        + short_lived * pricing["cache_write_5m"]
        + one_hour * pricing["cache_write_1h"]
    ) / 1_000_000
    return round(cost, 10)


def build_event(hook_input: dict, record: dict, agent_id: str | None = None) -> dict:
    message = record["message"]
    usage = record["usage"]
    message_uuid = record["message_uuid"]
    content = message.get("content")
    if not isinstance(content, list):
        content = []

    model = str(message.get("model"))
    tokens = usage_tokens(usage)
    input_tokens = tokens["input_tokens"]
    output_tokens = tokens["output_tokens"]
    cache_creation_tokens = tokens["cache_creation_tokens"]
    cache_read_tokens = tokens["cache_read_tokens"]

    if agent_id:
        # An agent transcript has no hook input of its own; its records name
        # the parent session and the real directory. Attribution stays on the
        # parent on purpose: the read model holds one task per session_id, and
        # parallel agents live inside the parent's wall clock — splitting them
        # into sessions of their own would sum overlapping intervals and push
        # the productivity multiplier down for no reason.
        session_id = record["session_id"] or hook_input["session_id"]
        working_dir = record["working_dir"] or hook_input["working_dir"]
    else:
        session_id = hook_input["session_id"]
        working_dir = hook_input["working_dir"]

    timestamp = record["timestamp"]
    if not isinstance(timestamp, str) or not timestamp:
        timestamp = datetime.now().astimezone().isoformat(timespec="milliseconds")

    event = {
        "schema_version": 1,
        # The transcript's own timestamp, so the same message carries the
        # same ts whether the hook or the backfill wrote it.
        "ts": timestamp,
        "session_id": session_id,
        "message_uuid": message_uuid,
        # Stable identity for the receiver's dedupe. Same shape in
        # backfill.build_tracker_event — keep the two in step.
        "event_id": f"claude:{session_id}:{message_uuid}",
        "model": model,
        "input_tokens": input_tokens,
        "output_tokens": output_tokens,
        "cache_creation_tokens": cache_creation_tokens,
        # Additive detail: the total above stays the field everything reads.
        "cache_creation_1h_tokens": tokens["cache_creation_1h_tokens"],
        "cache_creation_5m_tokens": tokens["cache_creation_5m_tokens"],
        "cache_read_tokens": cache_read_tokens,
        "cost_estimate_usd": estimate_cost(
            model,
            input_tokens,
            output_tokens,
            cache_creation_tokens,
            cache_read_tokens,
            tokens["cache_creation_1h_tokens"],
        ),
        # No duration_ms. A transcript records when a message was written, not
        # how long the model took, and the gap to the previous entry is mostly
        # tool and human time. Writing 0 turned "we do not know" into a
        # measurement — one that would drag any average straight to the floor.
        # The field is absent until something can actually time a call.
        "working_dir": working_dir,
        "tool_uses": sum(1 for block in content if isinstance(block, dict) and block.get("type") == "tool_use"),
        "stop_reason": message.get("stop_reason", ""),
    }
    if agent_id:
        event["agent_id"] = agent_id
        event["is_sidechain"] = True
    return event


def report_usage_anomalies(records: list[dict]) -> None:
    """One ops line per surprise per run — deliberately not per event.

    These are the things the pricing model cannot charge correctly: a
    cache-creation split that does not add up, a server-side tool call (web
    search and web fetch bill separately and no collector counts them), a
    service tier other than standard (batch and priority have their own
    rates). None of them has ever fired here — which is exactly why the
    first one must be visible instead of silently mispriced.
    """
    unclassified = 0
    server_calls = 0
    tiers: set[str] = set()
    for record in records:
        usage = record["usage"]
        unclassified += usage_tokens(usage)["unclassified_cache_creation_tokens"]
        server = usage.get("server_tool_use")
        if isinstance(server, dict):
            server_calls += sum(as_int(value) for value in server.values())
        tier = usage.get("service_tier")
        if isinstance(tier, str) and tier and tier != "standard":
            tiers.add(tier)

    if unclassified:
        record_ops("cache_creation_unclassified", f"tokens={unclassified} priced_at_5m_rate")
    if server_calls:
        record_ops("server_tool_use_unpriced", f"requests={server_calls}")
    if tiers:
        record_ops("service_tier_unpriced", f"tiers={sorted(tiers)}")


def process_alive(pid: int) -> bool:
    """Best-effort liveness probe. An unknown answer counts as alive, so an
    ambiguous case never costs us someone else's lock.

    Note for the POSIX habit: `os.kill(pid, 0)` is a probe on POSIX, but on
    Windows CPython maps os.kill onto TerminateProcess — it would kill the
    process it is asking about. Hence the ctypes path.
    """
    if pid <= 0:
        return True
    if os.name == "nt":
        try:
            kernel32 = ctypes.windll.kernel32
        except (AttributeError, OSError):
            return True
        process_query_limited_information = 0x1000
        still_active = 259
        error_invalid_parameter = 87
        handle = kernel32.OpenProcess(process_query_limited_information, False, pid)
        if not handle:
            # Only "no such process" proves death; access denied means it
            # exists and simply is not ours.
            return kernel32.GetLastError() != error_invalid_parameter
        exit_code = ctypes.c_ulong()
        queried = kernel32.GetExitCodeProcess(handle, ctypes.byref(exit_code))
        kernel32.CloseHandle(handle)
        if not queried:
            return True
        return exit_code.value == still_active
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except OSError:
        return True
    return True


def clear_stale_lock() -> None:
    try:
        age = time.time() - LOCK_FILE.stat().st_mtime
    except OSError:
        return

    try:
        holder = int(LOCK_FILE.read_text(encoding="ascii").strip() or 0)
    except (OSError, ValueError):
        holder = 0

    # A living holder keeps its lock however long it works — backfill.py
    # legitimately holds this while it walks every transcript on disk, and
    # an age-based steal would hand the ledger to two writers at once. Only
    # a provably dead holder is evicted immediately; the age cap applies
    # solely when the lock carries no readable pid to ask about.
    if holder > 0:
        if process_alive(holder):
            return
    elif age < LOCK_STALE_SECONDS:
        return

    try:
        LOCK_FILE.unlink()
    except OSError:
        return
    record_ops("lock_stale_cleared", f"holder_pid={holder} age_s={age:.0f}")


def acquire_lock() -> int | None:
    """O_EXCL lock with a short retry (two overlapping Stop hooks contend for
    well under a second) and recovery from a lock whose holder died.

    A final miss still returns None and the events are skipped THIS run —
    acceptable by design: the watermark is not advanced either, so the next
    run re-reads the same records, and backfill.py re-reads the transcripts
    on every deploy (dedup by session_id+message_uuid).
    """
    for attempt in range(4):
        try:
            fd = os.open(str(LOCK_FILE), os.O_CREAT | os.O_EXCL | os.O_WRONLY)
            os.write(fd, str(os.getpid()).encode("ascii"))
            return fd
        except FileExistsError:
            clear_stale_lock()
            if attempt < 3:
                time.sleep(0.05)
    return None


def release_lock(fd: int | None) -> None:
    if fd is not None:
        os.close(fd)
    try:
        LOCK_FILE.unlink()
    except OSError:
        pass


def read_last_uuids() -> dict:
    if not LAST_UUIDS_FILE.exists():
        return {}

    try:
        with LAST_UUIDS_FILE.open("r", encoding="utf-8") as source:
            data = json.load(source)
    except (OSError, json.JSONDecodeError):
        return {}
    return data if isinstance(data, dict) else {}


def atomic_replace_text(path: Path, temp_path: Path, text: str) -> None:
    with temp_path.open("w", encoding="utf-8", newline="\n") as target:
        target.write(text)
        target.flush()
        os.fsync(target.fileno())
    os.replace(temp_path, path)


def needs_leading_newline(path: Path | None = None) -> bool:
    """Whether the ledger's last line is unterminated. O(1) — the previous
    writer answered this by reading the entire file.

    Takes an explicit path because backfill.py shares this helper and owns
    its own EVENTS_FILE constant; relying on this module's would silently
    inspect the wrong file the day the two diverge.
    """
    target = EVENTS_FILE if path is None else path
    try:
        size = target.stat().st_size
    except OSError:
        return False
    if size == 0:
        return False
    try:
        with target.open("rb") as source:
            source.seek(-1, os.SEEK_END)
            return source.read(1) != b"\n"
    except OSError:
        return False


def append_new_events(hook_input: dict, streams: list[tuple[str, str | None, list[dict]]]) -> int:
    """Append everything past each stream's cursor. Returns how many.

    `streams` is (stream_key, agent_id, records) — the session's own
    transcript plus one entry per subagent transcript beneath it. Cursors
    are read inside the lock so two overlapping Stop hooks cannot both
    decide that the same records are new, and they advance only for streams
    that actually contributed.
    """
    TRACKER_DIR.mkdir(parents=True, exist_ok=True)

    fd = acquire_lock()
    if fd is None:
        pending = sum(len(records) for _key, _agent, records in streams)
        record_ops("lock_unavailable", f"session={hook_input['session_id']} pending={pending}")
        return 0

    uuids_tmp = TRACKER_DIR / f".last-uuids.json.tmp.{os.getpid()}"
    try:
        last_uuids = read_last_uuids()
        events: list[dict] = []
        advanced: dict[str, str] = {}
        for key, agent_id, records in streams:
            fresh = records_since(records, last_uuids.get(key))
            if not fresh:
                continue
            events.extend(build_event(hook_input, record, agent_id) for record in fresh)
            report_usage_anomalies(fresh)
            advanced[key] = fresh[-1]["message_uuid"]

        if not events:
            return 0

        payload = "".join(
            json.dumps(event, ensure_ascii=False, separators=(",", ":")) + "\n" for event in events
        )
        if needs_leading_newline():
            payload = "\n" + payload

        # Append, never rewrite. The old writer read the whole ledger and
        # replaced it through a temp copy: 218 MB of I/O per turn at the
        # current 109 MB, and every writer that died mid-flight left its
        # copy behind — 22 orphans totalling 1.8 GB by 2026-08-18.
        with EVENTS_FILE.open("a", encoding="utf-8", newline="\n") as target:
            target.write(payload)
            target.flush()
            os.fsync(target.fileno())

        last_uuids.update(advanced)
        atomic_replace_text(
            LAST_UUIDS_FILE,
            uuids_tmp,
            json.dumps(last_uuids, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        )
        return len(events)
    finally:
        try:
            uuids_tmp.unlink()
        except OSError:
            pass
        release_lock(fd)


def main() -> int:
    try:
        hook_input = read_hook_input()
        if hook_input is None:
            return 0

        session_id = hook_input["session_id"]
        transcript_path = hook_input["transcript_path"]
        streams: list[tuple[str, str | None, list[dict]]] = [
            (stream_key(session_id), None, read_billable_assistants(transcript_path)),
        ]
        for agent_transcript in subagent_transcripts(transcript_path):
            agent_id = agent_id_of(agent_transcript)
            streams.append((
                stream_key(session_id, agent_id),
                agent_id,
                read_billable_assistants(agent_transcript),
            ))

        if not any(records for _key, _agent, records in streams):
            return 0

        append_new_events(hook_input, streams)
    except Exception as exc:
        # Bookkeeping must never take the session down with it — but a
        # collector that fails quietly reads as a quiet week, so leave a
        # trace in the ops channel.
        record_ops("hook_failed", f"{type(exc).__name__}: {exc}")
        return 0

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
