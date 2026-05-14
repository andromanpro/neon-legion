from __future__ import annotations

import json
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from tools.reverse_autopilot import (
    Pattern,
    main,
    mine_patterns,
    render_proposal,
)


def _user_event(text: str, ts: str = "2026-05-12T10:00:00+03:00") -> str:
    return json.dumps({
        "type": "user",
        "timestamp": ts,
        "message": {"content": text},
    })


def _user_event_listcontent(text: str, ts: str = "2026-05-12T10:00:00+03:00") -> str:
    return json.dumps({
        "type": "user",
        "timestamp": ts,
        "message": {"content": [{"type": "text", "text": text}]},
    })


def _tool_result_event() -> str:
    return json.dumps({
        "type": "user",
        "message": {"content": [{"type": "tool_result", "content": "some output"}]},
    })


class MinePatternsTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = Path(tempfile.mkdtemp())
        self.proj = self.tmp / "F--WorkAI"
        self.proj.mkdir()

    def tearDown(self) -> None:
        import shutil

        shutil.rmtree(self.tmp, ignore_errors=True)

    def _write_session(self, session_id: str, lines: list[str]) -> None:
        (self.proj / f"{session_id}.jsonl").write_text("\n".join(lines) + "\n", encoding="utf-8")

    def test_repeated_prefix_across_sessions_detected(self) -> None:
        self._write_session("sess-a", [
            _user_event("run build for the dashboard project"),
            _user_event("run build for the dashboard widget"),
        ])
        self._write_session("sess-b", [
            _user_event("run build for the snapshot pipeline"),
        ])

        patterns = mine_patterns(self.tmp, prefix_words=4)

        key = "run build for the"
        self.assertIn(key, patterns)
        p = patterns[key]
        self.assertEqual(3, p.occurrences)
        self.assertEqual(2, len(p.sessions))

    def test_list_content_user_messages_handled(self) -> None:
        self._write_session("sess-c", [
            _user_event_listcontent("давай дальше по плану продолжаем"),
            _user_event_listcontent("давай дальше по плану следующее"),
        ])

        patterns = mine_patterns(self.tmp, prefix_words=4)
        key = "давай дальше по плану"
        self.assertIn(key, patterns)
        self.assertEqual(2, patterns[key].occurrences)

    def test_tool_results_skipped(self) -> None:
        # tool_result item should NOT register as user-side text.
        self._write_session("sess-d", [_tool_result_event(), _user_event("real user message words here")])
        patterns = mine_patterns(self.tmp, prefix_words=4)
        # Only the real user message should produce a prefix.
        # The tool_result entry has no text → no key.
        keys = list(patterns.keys())
        self.assertEqual(["real user message words"], keys)

    def test_short_messages_ignored(self) -> None:
        # 3-word message + prefix_words=4 → no key produced.
        self._write_session("sess-e", [_user_event("ok thanks done")])
        patterns = mine_patterns(self.tmp, prefix_words=4)
        self.assertEqual({}, patterns)

    def test_examples_capped_at_three(self) -> None:
        self._write_session("sess-f", [_user_event(f"run build number {i} now") for i in range(10)])
        patterns = mine_patterns(self.tmp, prefix_words=3)
        p = next(iter(patterns.values()))
        self.assertEqual(10, p.occurrences)
        self.assertLessEqual(len(p.examples), 3)

    def test_first_last_seen_tracked(self) -> None:
        self._write_session("sess-g", [
            _user_event("scan tracker for stale data", ts="2026-04-01T10:00:00+03:00"),
            _user_event("scan tracker for stale data", ts="2026-05-12T10:00:00+03:00"),
        ])
        patterns = mine_patterns(self.tmp, prefix_words=4)
        p = patterns["scan tracker for stale"]
        self.assertEqual("2026-04-01T10:00:00+03:00", p.first_seen)
        self.assertEqual("2026-05-12T10:00:00+03:00", p.last_seen)


class RenderProposalTests(unittest.TestCase):
    def test_action_verb_proposes_script_form(self) -> None:
        pattern = Pattern(
            key="run build for dashboard",
            occurrences=5,
            sessions={"a", "b"},
            examples=["run build for dashboard tonight"],
            first_seen="2026-05-01T10:00:00",
            last_seen="2026-05-12T10:00:00",
        )
        text = render_proposal(pattern)
        self.assertIn("Occurrences:** 5", text)
        self.assertIn("script", text.lower())
        self.assertIn("tools/run-build-for-dashboard.py", text)

    def test_question_word_proposes_prompt_template(self) -> None:
        pattern = Pattern(
            key="как починить ошибку в",
            occurrences=4,
            sessions={"a", "b", "c"},
        )
        text = render_proposal(pattern)
        self.assertIn("prompt-template", text.lower())
        self.assertIn("prompts/", text)

    def test_short_prefix_marked_as_noise(self) -> None:
        pattern = Pattern(
            key="ok thanks",
            occurrences=8,
            sessions={"a", "b"},
        )
        text = render_proposal(pattern)
        self.assertIn("noise", text.lower())

    def test_proposal_lists_examples(self) -> None:
        pattern = Pattern(
            key="run build now",
            occurrences=3,
            sessions={"a"},
            examples=["run build now please", "run build now and verify"],
        )
        text = render_proposal(pattern)
        self.assertIn("> run build now please", text)
        self.assertIn("> run build now and verify", text)


class CliTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = Path(tempfile.mkdtemp())
        self.proj = self.tmp / "projects" / "F--WorkAI"
        self.proj.mkdir(parents=True)
        # 3 sessions all using "run build for dashboard"
        for i, session in enumerate(["a", "b", "c"]):
            (self.proj.parent / f"... not session ...").mkdir(exist_ok=True)
            (self.proj / f"sess-{session}.jsonl").write_text(
                _user_event(f"run build for dashboard task {i}") + "\n",
                encoding="utf-8",
            )
        self.output = self.tmp / "out"

    def tearDown(self) -> None:
        import shutil

        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_cli_writes_one_proposal_when_threshold_met(self) -> None:
        rc = main([
            "--projects-dir", str(self.proj.parent),
            "--output-dir", str(self.output),
            "--min-occurrences", "3",
            "--min-sessions", "2",
            "--prefix-words", "4",
        ])
        self.assertEqual(0, rc)
        files = sorted(p.name for p in self.output.glob("*.md"))
        self.assertEqual(1, len(files))
        self.assertTrue(files[0].startswith("repeat-"))

    def test_cli_writes_nothing_below_threshold(self) -> None:
        # Single occurrence, threshold=3 → no proposal.
        for f in self.proj.iterdir():
            f.unlink()
        (self.proj / "sess-only.jsonl").write_text(
            _user_event("unique single message about nothing") + "\n",
            encoding="utf-8",
        )
        rc = main([
            "--projects-dir", str(self.proj.parent),
            "--output-dir", str(self.output),
            "--min-occurrences", "3",
        ])
        self.assertEqual(0, rc)
        files = list(self.output.glob("*.md")) if self.output.exists() else []
        self.assertEqual([], files)

    def test_cli_exits_2_on_missing_projects_dir(self) -> None:
        rc = main([
            "--projects-dir", str(self.tmp / "nope"),
            "--output-dir", str(self.output),
        ])
        self.assertEqual(2, rc)


class SystemNoiseFilterTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = Path(tempfile.mkdtemp())
        self.proj = self.tmp / "F--WorkAI"
        self.proj.mkdir()

    def tearDown(self) -> None:
        import shutil

        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_system_reminder_prefix_dropped(self) -> None:
        (self.proj / "sess.jsonl").write_text(
            "\n".join([
                _user_event("<system-reminder>blah blah</system-reminder> some content here"),
                _user_event("real human message that should be kept"),
            ]) + "\n",
            encoding="utf-8",
        )
        patterns = mine_patterns(self.tmp, prefix_words=4)
        keys = list(patterns.keys())
        self.assertNotIn("system reminder blah blah", keys)
        self.assertIn("real human message that", keys)

    def test_session_resume_preamble_dropped(self) -> None:
        (self.proj / "sess.jsonl").write_text(
            _user_event("This session is being continued from a previous one") + "\n",
            encoding="utf-8",
        )
        patterns = mine_patterns(self.tmp, prefix_words=4)
        self.assertEqual({}, patterns)


class PatternDataclassTests(unittest.TestCase):
    def test_pattern_default_factories_are_independent(self) -> None:
        # Guard against the classic mutable-default footgun.
        p1 = Pattern(key="x")
        p2 = Pattern(key="y")
        p1.sessions.add("a")
        self.assertEqual(set(), p2.sessions)
        p1.examples.append("e")
        self.assertEqual([], p2.examples)


if __name__ == "__main__":
    unittest.main()
