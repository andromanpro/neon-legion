#!/usr/bin/env python
"""Heuristic 'slop' detector for assistant output / role deliverables.

Scores 0-100 (higher = more slop) using three pluggable signals:

1. **Trigram repetition** — `1 - (unique_trigrams / total_trigrams)`. Models
   that fill instead of solve repeat themselves at the n-gram level.
2. **Generic-phrase density** — hits against a tunable blocklist of
   filler openers ("In summary,", "It's important to note", etc.) per
   100 words.
3. **Hedge:imperative ratio** — `hedge_words / max(1, imperative_words)`,
   capped at 1.0. High ratios mean the model is suggesting rather than
   doing.

Per-message score is a weighted sum (defaults: 0.4 / 0.3 / 0.3). All
heuristics + weights tunable via `[slop_score]` in `config.toml`.

Aggregates per (agent, role) across orchestrate-runs/* and writes
`tracker/slop.json` with the rolling averages so a future dashboard
widget can render slope-lines.

Stdlib only.
"""

from __future__ import annotations

import argparse
import glob
import json
import os
import re
import sys
from collections import defaultdict
from datetime import datetime, timedelta
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT))

from tools import config as cfg  # noqa: E402


DEFAULT_GENERIC_PHRASES = (
    "in summary",
    "it is important to note",
    "it's important to note",
    "in conclusion",
    "as an ai language model",
    "let me know if you have",
    "let me know if you need",
    "feel free to ask",
    "i hope this helps",
    "please note that",
    "as a side note",
    "with that being said",
    "that being said",
    "in essence",
    "broadly speaking",
    "generally speaking",
    "to put it simply",
    "for example,",
    "for instance,",
    "in order to",
)

DEFAULT_HEDGE_WORDS = (
    "might", "may", "could", "would", "should", "perhaps", "possibly",
    "potentially", "consider", "consider that", "consider whether",
    "it seems", "appears to", "tends to", "often", "sometimes",
    "in general", "typically", "arguably", "presumably",
)

DEFAULT_IMPERATIVE_WORDS = (
    "run", "use", "do", "fix", "check", "verify", "add", "remove",
    "replace", "rename", "build", "test", "deploy", "commit", "push",
    "merge", "scan", "read", "write", "set", "ensure", "call", "import",
    "install", "open", "close", "rebase", "restart", "execute", "create",
    "delete", "update", "save", "compute", "extract", "render", "parse",
    "validate", "compile", "rebuild", "stage", "tag",
)

DEFAULT_WEIGHTS = {
    "trigram": 0.4,
    "generic": 0.3,
    "hedge": 0.3,
}

WORD_RE = re.compile(r"[A-Za-zА-Яа-яЁё][A-Za-zА-Яа-яЁё']*")


def score_text(text: str, *, config: dict | None = None) -> dict:
    """Score one message / role deliverable. Returns components + 0-100 total."""
    cfg_ = _resolve_config(config or {})
    lower_text = text.lower()
    words = [w.lower() for w in WORD_RE.findall(text)]
    word_count = len(words)

    trigram = _trigram_repetition(words)
    generic = _generic_density(lower_text, cfg_["generic_phrases"], word_count)
    hedge = _hedge_imperative(
        words, lower_text, cfg_["hedge_words"], cfg_["imperative_words"]
    )

    weights = cfg_["weights"]
    overall = 100.0 * (
        weights["trigram"] * trigram
        + weights["generic"] * generic
        + weights["hedge"] * hedge
    )
    return {
        "score": round(overall, 1),
        "components": {
            "trigram_repetition": round(trigram, 4),
            "generic_phrase_density": round(generic, 4),
            "hedge_imperative_ratio": round(hedge, 4),
        },
        "word_count": word_count,
    }


def score_run(run_dir: Path, *, config: dict | None = None) -> list[dict]:
    """Score every role deliverable in one orchestrate run."""
    state_path = run_dir / "state.json"
    if not state_path.exists():
        return []
    state = json.loads(state_path.read_text(encoding="utf-8"))
    roles_path = state.get("roles_path") or str(run_dir / "roles.used.toml")
    role_to_agent = _read_role_providers(Path(roles_path)) if Path(roles_path).exists() else {}

    out: list[dict] = []
    for step in state.get("steps") or []:
        role = step.get("role", "unknown")
        response_path_str = step.get("response_path") or step.get("output_path") or ""
        if not response_path_str:
            continue
        response_path = Path(response_path_str)
        # state.json may store absolute paths; resolve relative to run_dir as fallback
        if not response_path.exists():
            response_path = run_dir / response_path.name
        if not response_path.exists():
            continue
        text = response_path.read_text(encoding="utf-8", errors="replace")
        result = score_text(text, config=config)
        result.update({
            "run_id": state.get("run_id", run_dir.name),
            "created_at": state.get("created_at"),
            "role": role,
            "agent": role_to_agent.get(role, "unknown"),
        })
        out.append(result)
    return out


def _model_short(name: object) -> str:
    """Compact per-agent label, mirrors backend/server.py _model_short so the
    slop widget's agents match the models widget ("opus 4.8", "gpt-5.6-sol")."""
    if not isinstance(name, str):
        return "unknown"
    bare = name.lower()
    for prefix in ("anthropic/", "openai/", "claude-", "claude/"):
        if bare.startswith(prefix):
            bare = bare[len(prefix):]
            break
    parts = bare.split("-")
    if len(parts) >= 2 and parts[1].isdigit():
        if len(parts) >= 3 and parts[2].isdigit():
            return f"{parts[0]} {parts[1]}.{parts[2]}"
        return f"{parts[0]} {parts[1]}"
    return bare


def _assistant_text(content: object) -> str:
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        return "\n".join(
            str(block.get("text", ""))
            for block in content
            if isinstance(block, dict) and block.get("type") == "text"
        )
    return ""


def _parse_ts(value: object) -> datetime | None:
    if not isinstance(value, str) or not value:
        return None
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None


# Cap concatenated per-(session,model) text so a marathon session's megabytes
# don't make the regex pass pathological. 200k chars is ~30k words — plenty for
# a stable slop estimate.
_MAX_SCORE_CHARS = 200_000


def score_transcripts(
    scan_dir: Path,
    *,
    lookback_days: int = 30,
    now: datetime | None = None,
    config: dict | None = None,
) -> list[dict]:
    """Score the AI's own responses from Claude transcripts, one scored item per
    (session, model). agent = pretty model name, role = "assistant". Only
    aggregate scores leave this function — raw response text never does.

    Reads ~/.claude/projects/*/*.jsonl (assistant events with a text body),
    dedupes by message uuid, and restricts to the lookback window by local day.
    """
    current = (now or datetime.now().astimezone())
    cutoff = current - timedelta(days=max(1, int(lookback_days)))
    pattern = str(Path(scan_dir).expanduser() / "*" / "*.jsonl")

    buckets: dict[tuple[str, str], dict] = {}
    seen_uuids: set[str] = set()
    for path_str in glob.glob(pattern):
        try:
            with open(path_str, encoding="utf-8") as source:
                for line in source:
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        event = json.loads(line)
                    except json.JSONDecodeError:
                        continue
                    if event.get("type") != "assistant":
                        continue
                    message = event.get("message")
                    if not isinstance(message, dict):
                        continue
                    model = message.get("model")
                    if not isinstance(model, str) or not model or model == "<synthetic>":
                        continue
                    uuid = event.get("uuid")
                    if isinstance(uuid, str) and uuid:
                        if uuid in seen_uuids:
                            continue
                        seen_uuids.add(uuid)
                    ts = _parse_ts(event.get("timestamp") or event.get("ts"))
                    if ts is None:
                        continue
                    ts = ts.astimezone()
                    if ts < cutoff or ts > current:
                        continue
                    text = _assistant_text(message.get("content"))
                    if not text.strip():
                        continue
                    session_id = event.get("session_id")
                    if not isinstance(session_id, str) or not session_id:
                        session_id = Path(path_str).stem
                    key = (session_id, model)
                    bucket = buckets.get(key)
                    if bucket is None:
                        bucket = buckets[key] = {"chunks": [], "chars": 0, "created": ts}
                    if bucket["chars"] < _MAX_SCORE_CHARS:
                        bucket["chunks"].append(text)
                        bucket["chars"] += len(text)
                    if ts < bucket["created"]:
                        bucket["created"] = ts
        except (OSError, UnicodeDecodeError):
            continue

    scored: list[dict] = []
    for (session_id, model), bucket in buckets.items():
        joined = "\n".join(bucket["chunks"])[:_MAX_SCORE_CHARS]
        result = score_text(joined, config=config)
        result.update({
            "run_id": session_id,
            "created_at": bucket["created"].isoformat(timespec="seconds"),
            "role": "assistant",
            "agent": _model_short(model),
        })
        scored.append(result)
    return scored


def aggregate(scored: list[dict]) -> dict:
    """Aggregate scored runs into per-agent / per-role / per-session blocks."""
    by_session = defaultdict(list)
    by_agent_role = defaultdict(list)
    by_role = defaultdict(list)
    by_agent = defaultdict(list)
    for s in scored:
        by_session[s["run_id"]].append(s)
        by_agent_role[(s["agent"], s["role"])].append(s)
        by_role[s["role"]].append(s)
        by_agent[s["agent"]].append(s)

    def _mean(items: list[dict]) -> float:
        if not items:
            return 0.0
        return round(sum(i["score"] for i in items) / len(items), 1)

    sessions = []
    for sid, items in sorted(by_session.items(), key=lambda kv: kv[1][0].get("created_at") or ""):
        sessions.append({
            "run_id": sid,
            "created_at": items[0].get("created_at"),
            "mean_score": _mean(items),
            "messages": len(items),
        })

    agent_role = []
    for (agent, role), items in sorted(by_agent_role.items()):
        agent_role.append({
            "agent": agent,
            "role": role,
            "mean_score": _mean(items),
            "samples": len(items),
        })

    by_role_summary = [
        {"role": role, "mean_score": _mean(items), "samples": len(items)}
        for role, items in sorted(by_role.items())
    ]
    by_agent_summary = [
        {"agent": agent, "mean_score": _mean(items), "samples": len(items)}
        for agent, items in sorted(by_agent.items())
    ]

    return {
        "sessions": sessions,
        "by_agent_role": agent_role,
        "by_role": by_role_summary,
        "by_agent": by_agent_summary,
        "messages_scored": len(scored),
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--runs-dir",
        default=str(PROJECT_ROOT / "orchestrate-runs"),
        help="Directory containing orchestrate-runs/*/state.json subdirs.",
    )
    parser.add_argument(
        "--source",
        choices=("runs", "transcripts", "both"),
        default="runs",
        help="What to score: orchestrate 'runs', Claude 'transcripts' (per model), or 'both'.",
    )
    parser.add_argument(
        "--scan-dir",
        default=str(Path.home() / ".claude" / "projects"),
        help="Claude projects dir for --source transcripts/both.",
    )
    parser.add_argument(
        "--lookback-days",
        type=int,
        default=30,
        help="Transcript lookback window in days (transcripts/both).",
    )
    parser.add_argument(
        "--output",
        default=str(PROJECT_ROOT / "tracker" / "slop.json"),
        help="Path to write slop.json aggregate.",
    )
    args = parser.parse_args(argv)

    scored: list[dict] = []
    runs_scanned = 0

    if args.source in ("runs", "both"):
        runs_dir = Path(args.runs_dir)
        if runs_dir.is_dir():
            for entry in sorted(runs_dir.iterdir()):
                if not entry.is_dir():
                    continue
                runs_scanned += 1
                scored.extend(score_run(entry))
        elif args.source == "runs":
            print(f"[slop-score] runs dir not found: {runs_dir}", file=sys.stderr)
            return 2
        else:
            print(f"[slop-score] runs dir not found (continuing, transcripts only): {runs_dir}", file=sys.stderr)

    transcript_items = 0
    if args.source in ("transcripts", "both"):
        transcript_scored = score_transcripts(Path(args.scan_dir), lookback_days=args.lookback_days)
        transcript_items = len(transcript_scored)
        scored.extend(transcript_scored)

    summary = aggregate(scored)
    payload = {
        "schema_version": 1,
        "generated_at": datetime.now().astimezone().isoformat(timespec="seconds"),
        "config": _resolve_config({}),
        "source": args.source,
        "scored": scored,
        "summary": summary,
    }
    output = Path(args.output)
    atomic_write(output, json.dumps(payload, ensure_ascii=False, indent=2) + "\n")
    print(f"[slop-score] wrote {output}")
    print(
        f"[slop-score] source={args.source} runs_scanned={runs_scanned} "
        f"transcript_items={transcript_items} "
        f"messages_scored={summary['messages_scored']} "
        f"agents={len(summary['by_agent'])}"
    )
    return 0


def atomic_write(path: Path, content: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(f".{path.name}.tmp.{os.getpid()}")
    try:
        tmp.write_text(content, encoding="utf-8", newline="\n")
        os.replace(tmp, path)
    finally:
        if tmp.exists():
            tmp.unlink()


# --- internals ----------------------------------------------------------------


def _trigram_repetition(words: list[str]) -> float:
    """Fraction of non-unique trigrams in the sequence. 0 → no repetition."""
    if len(words) < 3:
        return 0.0
    grams = [tuple(words[i:i + 3]) for i in range(len(words) - 2)]
    unique = len(set(grams))
    total = len(grams)
    if total == 0:
        return 0.0
    return 1.0 - (unique / total)


def _generic_density(lower_text: str, phrases: tuple[str, ...], word_count: int) -> float:
    """Generic-phrase hits per 100 words, capped at 1.0."""
    if word_count == 0:
        return 0.0
    hits = sum(lower_text.count(p) for p in phrases)
    density = hits / max(word_count, 1) * 100  # per-100-words
    return min(density / 5.0, 1.0)  # ≥5 hits per 100 words → max signal


def _hedge_imperative(
    words: list[str],
    lower_text: str,
    hedge: tuple[str, ...],
    imperative: tuple[str, ...],
) -> float:
    """Return hedge:imperative ratio, capped at 1.0.

    Splits hedge entries into single-word (set membership against tokenized
    words) vs multi-word phrases (substring count against lowered text).
    Previously the multi-word entries («consider that», «it seems», etc.)
    were silently dead code because tokenization broke them apart before
    the set check — DeepSeek MED.
    """
    hedge_single = {w for w in hedge if " " not in w}
    hedge_phrases = [w for w in hedge if " " in w]
    imperative_single = {w for w in imperative if " " not in w}
    imperative_phrases = [w for w in imperative if " " in w]

    h = sum(1 for w in words if w in hedge_single)
    h += sum(lower_text.count(p) for p in hedge_phrases)
    i = sum(1 for w in words if w in imperative_single)
    i += sum(lower_text.count(p) for p in imperative_phrases)

    if h == 0:
        return 0.0
    if i == 0:
        return 1.0
    return min(h / max(i, 1), 1.0)


def _resolve_config(overrides: dict) -> dict:
    """Build config dict from config.toml + overrides."""
    weights = dict(DEFAULT_WEIGHTS)
    weights_cfg = cfg.get("slop_score.weights", {}, dict) or {}
    for k in ("trigram", "generic", "hedge"):
        if k in weights_cfg:
            try:
                weights[k] = float(weights_cfg[k])
            except (TypeError, ValueError):
                pass
    weights.update(overrides.get("weights", {}))

    generic_phrases = overrides.get("generic_phrases") or cfg.get(
        "slop_score.generic_phrases", list(DEFAULT_GENERIC_PHRASES), list
    )
    hedge_words = overrides.get("hedge_words") or cfg.get(
        "slop_score.hedge_words", list(DEFAULT_HEDGE_WORDS), list
    )
    imperative_words = overrides.get("imperative_words") or cfg.get(
        "slop_score.imperative_words", list(DEFAULT_IMPERATIVE_WORDS), list
    )

    return {
        "weights": weights,
        "generic_phrases": tuple(p.lower() for p in generic_phrases),
        "hedge_words": tuple(w.lower() for w in hedge_words),
        "imperative_words": tuple(w.lower() for w in imperative_words),
    }


def _read_role_providers(roles_path: Path) -> dict[str, str]:
    """Thin wrapper around the shared `tools.config.read_role_providers`.

    Centralized in `tools/config.py` to handle ALL TOML quote styles
    (DeepSeek MED — the old in-file regex only matched `"double"` quotes).
    """
    return cfg.read_role_providers(roles_path)


if __name__ == "__main__":
    raise SystemExit(main())
