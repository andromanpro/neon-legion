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
import json
import os
import re
import sys
from collections import defaultdict
from datetime import datetime
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
    words = [w.lower() for w in WORD_RE.findall(text)]
    word_count = len(words)

    trigram = _trigram_repetition(words)
    generic = _generic_density(text.lower(), cfg_["generic_phrases"], word_count)
    hedge = _hedge_imperative(words, cfg_["hedge_words"], cfg_["imperative_words"])

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
        "--output",
        default=str(PROJECT_ROOT / "tracker" / "slop.json"),
        help="Path to write slop.json aggregate.",
    )
    args = parser.parse_args(argv)

    runs_dir = Path(args.runs_dir)
    if not runs_dir.is_dir():
        print(f"[slop-score] runs dir not found: {runs_dir}", file=sys.stderr)
        return 2

    scored: list[dict] = []
    for entry in sorted(runs_dir.iterdir()):
        if not entry.is_dir():
            continue
        scored.extend(score_run(entry))

    summary = aggregate(scored)
    payload = {
        "schema_version": 1,
        "generated_at": datetime.now().astimezone().isoformat(timespec="seconds"),
        "config": _resolve_config({}),
        "scored": scored,
        "summary": summary,
    }
    output = Path(args.output)
    atomic_write(output, json.dumps(payload, ensure_ascii=False, indent=2) + "\n")
    print(f"[slop-score] wrote {output}")
    print(
        f"[slop-score] runs_scanned={sum(1 for _ in runs_dir.iterdir() if _.is_dir())} "
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
    hedge: tuple[str, ...],
    imperative: tuple[str, ...],
) -> float:
    hedge_set = set(hedge)
    imperative_set = set(imperative)
    h = sum(1 for w in words if w in hedge_set)
    i = sum(1 for w in words if w in imperative_set)
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
    """Stdlib-only TOML parse — we only need [role.<name>] → provider."""
    out: dict[str, str] = {}
    if not roles_path.exists():
        return out
    current_role = None
    role_pat = re.compile(r"^\s*\[role\.([^\]]+)\]\s*$")
    provider_pat = re.compile(r"^\s*provider\s*=\s*\"([^\"]*)\"\s*$")
    for line in roles_path.read_text(encoding="utf-8").splitlines():
        m = role_pat.match(line)
        if m:
            current_role = m.group(1).strip()
            continue
        if current_role:
            m = provider_pat.match(line)
            if m:
                out[current_role] = m.group(1).strip()
                current_role = None
    return out


if __name__ == "__main__":
    raise SystemExit(main())
