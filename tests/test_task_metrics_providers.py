"""Which events may carry a productivity baseline.

Claude sessions always. Codex sessions ONLY when the human drove them: a
`codex exec` dispatch runs inside a Claude session whose baseline already
covers that work, so counting it again would double-count. Desktop Codex has
no orchestrator at all — excluding it (as the code did until 2026-08-18) hid
19,129 calls, 99.7% of Codex usage, from the multiplier.
"""

from __future__ import annotations

import importlib.util
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
_spec = importlib.util.spec_from_file_location("nl_summary", ROOT / "tracker" / "summary.py")
summary = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(summary)


def ev(provider, **over):
    base = {"provider": provider, "session_id": "s", "ts": "2026-08-18T10:00:00+03:00"}
    base.update(over)
    return base


class TaskMetricEventFilterTests(unittest.TestCase):
    def _kept(self, events):
        return summary.events_for_task_metrics(events)

    def test_claude_events_always_kept(self):
        events = [ev("anthropic", model="claude-opus-5")]
        self.assertEqual(self._kept(events), events)

    def test_codex_desktop_is_kept(self):
        events = [ev("openai", codex_origin="desktop")]
        self.assertEqual(self._kept(events), events)

    def test_codex_tui_is_kept(self):
        events = [ev("openai", codex_origin="tui")]
        self.assertEqual(self._kept(events), events)

    def test_codex_exec_dispatch_is_dropped(self):
        # Its hours live in the dispatching Claude session's baseline.
        self.assertEqual(self._kept([ev("openai", codex_origin="headless")]), [])

    def test_codex_auto_review_is_dropped(self):
        self.assertEqual(self._kept([ev("openai", codex_origin="auto_review")]), [])

    def test_origin_inferred_when_field_absent(self):
        # No codex_origin key → codex_origin() infers from originator/source.
        kept = self._kept([ev("openai", originator="Codex Desktop", source="vscode")])
        self.assertEqual(len(kept), 1)
        dropped = self._kept([ev("openai", originator="codex_exec", source="exec")])
        self.assertEqual(dropped, [])

    def test_openrouter_and_opencode_stay_out(self):
        self.assertEqual(self._kept([ev("openrouter"), ev("opencode")]), [])

    def test_mixed_stream_keeps_only_eligible(self):
        events = [
            ev("anthropic", session_id="c1"),
            ev("openai", session_id="d1", codex_origin="desktop"),
            ev("openai", session_id="h1", codex_origin="headless"),
            ev("openrouter", session_id="o1"),
        ]
        kept = self._kept(events)
        self.assertEqual([e["session_id"] for e in kept], ["c1", "d1"])


if __name__ == "__main__":
    unittest.main()
