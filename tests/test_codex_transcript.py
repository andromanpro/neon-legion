"""Codex rollout reader: role="user" is not the same as "the human typed this".

The Codex app injects plugin catalogues, environment blocks and whole
AGENTS.md dumps under the user role. Counting those would inflate the
attention denominator and — worse — poison the estimator, whose prompt budget
takes the first three user messages (message #1 of every desktop session is an
11.8 KB plugin list).
"""

from __future__ import annotations

import importlib.util
import json
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
_spec = importlib.util.spec_from_file_location(
    "codex_transcript", ROOT / "tracker" / "codex_transcript.py"
)
ct = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(ct)


def user_line(text: str, ts: str = "2026-08-17T15:40:35.397Z") -> dict:
    return {
        "timestamp": ts,
        "type": "response_item",
        "payload": {"type": "message", "role": "user",
                    "content": [{"type": "input_text", "text": text}]},
    }


def assistant_line(text: str, ts: str = "2026-08-17T15:40:43.579Z") -> dict:
    return {
        "timestamp": ts,
        "type": "response_item",
        "payload": {"type": "message", "role": "assistant",
                    "content": [{"type": "output_text", "text": text}]},
    }


def meta_line(**over) -> dict:
    payload = {"session_id": "01a01061", "originator": "Codex Desktop",
               "source": "vscode", "thread_source": "user"}
    payload.update(over)
    return {"timestamp": "2026-08-17T15:39:55.392Z", "type": "session_meta", "payload": payload}


def write(tmp: Path, lines: list[dict], name: str = "rollout.jsonl") -> Path:
    path = tmp / name
    path.write_text("\n".join(json.dumps(x, ensure_ascii=False) for x in lines), encoding="utf-8")
    return path


class InjectedDetectionTests(unittest.TestCase):
    def test_real_injected_blocks_are_filtered(self):
        for text in (
            "<recommended_plugins>\nHere is a list of plugins...",
            "<environment_context>\n  <current_date>2026-08-18</current_date>",
            "<turn_aborted>",
            "<codex_internal_context>x",
            "# AGENTS.md instructions for F:\\WorkAI\n\n<INSTRUCTIONS>",
        ):
            with self.subTest(text=text[:30]):
                self.assertTrue(ct.is_injected(text))

    def test_genuine_prompts_survive(self):
        for text in (
            "Запусти и дай ссылку",
            "я ебал",
            "# Task: S4 — XML/EDT parity normalization",
            "# Аудит 204 random-signal фраз для виджета",
            "# Files mentioned by the user:\n\n## shot.png: F:/tmp/shot.png",
            "Посмотри <environment_context> в доке — там опечатка",
            "<не тег> просто текст в угловых скобках",
        ):
            with self.subTest(text=text[:30]):
                self.assertFalse(ct.is_injected(text))

    def test_tag_must_lead_not_merely_appear(self):
        self.assertFalse(ct.is_injected("см. <recommended_plugins> ниже"))


class HumanDrivenTests(unittest.TestCase):
    def test_desktop_session_is_human_driven(self):
        self.assertTrue(ct.is_human_driven(
            {"originator": "Codex Desktop", "source": "vscode", "thread_source": "user"}))

    def test_cli_session_is_human_driven(self):
        self.assertTrue(ct.is_human_driven(
            {"originator": "Codex Desktop", "source": "cli", "thread_source": "user"}))

    def test_codex_exec_dispatch_is_not(self):
        # Launched by Claude from inside a Claude session — its hours are
        # already inside that session's baseline.
        self.assertFalse(ct.is_human_driven(
            {"originator": "codex_exec", "source": "exec", "thread_source": "user"}))
        self.assertFalse(ct.is_human_driven(
            {"originator": "Codex Desktop", "source": "exec", "thread_source": None}))

    def test_subagent_spawn_is_not(self):
        self.assertFalse(ct.is_human_driven({
            "originator": "Codex Desktop",
            "source": {"subagent": {"thread_spawn": {"depth": 1}}},
            "thread_source": "subagent",
        }))

    def test_garbage_meta_is_not(self):
        self.assertFalse(ct.is_human_driven({}))
        self.assertFalse(ct.is_human_driven(None))


class ReaderTests(unittest.TestCase):
    def test_reads_only_genuine_messages(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = write(Path(tmp), [
                meta_line(),
                user_line("<recommended_plugins>\nAirtable..."),
                user_line("Новый проект - делаем автомат"),
                assistant_line("Понял, начинаю"),
                user_line("# AGENTS.md instructions for F:\\WorkAI\n<INSTRUCTIONS>"),
                user_line("Запусти и дай ссылку"),
            ])
            users, assistants = ct.read_messages(path)
        self.assertEqual(users, ["Новый проект - делаем автомат", "Запусти и дай ссылку"])
        self.assertEqual(assistants, ["Понял, начинаю"])

    def test_human_timestamps_skip_injected(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = write(Path(tmp), [
                meta_line(),
                user_line("<recommended_plugins>x", ts="2026-08-17T10:00:00Z"),
                user_line("делай", ts="2026-08-17T10:05:00Z"),
                user_line("дальше", ts="2026-08-17T10:20:00Z"),
            ])
            stamps = ct.read_human_timestamps(path)
        self.assertEqual(len(stamps), 2)
        self.assertEqual(stamps, sorted(stamps))
        self.assertEqual((stamps[1] - stamps[0]).total_seconds(), 900)

    def test_developer_role_is_ignored(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = write(Path(tmp), [
                meta_line(),
                {"timestamp": "2026-08-17T10:00:00Z", "type": "response_item",
                 "payload": {"type": "message", "role": "developer",
                             "content": [{"type": "input_text", "text": "<app-context>"}]}},
            ])
            users, assistants = ct.read_messages(path)
        self.assertEqual(users, [])
        self.assertEqual(assistants, [])

    def test_non_message_records_ignored(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = write(Path(tmp), [
                meta_line(),
                {"timestamp": "2026-08-17T10:00:00Z", "type": "event_msg",
                 "payload": {"type": "task_started"}},
                {"timestamp": "2026-08-17T10:00:01Z", "type": "response_item",
                 "payload": {"type": "reasoning", "summary": []}},
            ])
            self.assertEqual(ct.read_messages(path), ([], []))

    def test_reads_meta(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = write(Path(tmp), [meta_line(session_id="abc"), user_line("hi")])
            self.assertEqual(ct.read_meta(path).get("session_id"), "abc")


class FormatSniffTests(unittest.TestCase):
    def test_rollout_detected(self):
        with tempfile.TemporaryDirectory() as tmp:
            self.assertTrue(ct.is_rollout(write(Path(tmp), [meta_line(), user_line("x")])))

    def test_claude_transcript_not_detected(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "claude.jsonl"
            path.write_text(json.dumps({
                "type": "user", "timestamp": "2026-08-17T10:00:00Z",
                "message": {"role": "user", "content": [{"type": "text", "text": "hi"}]},
            }), encoding="utf-8")
            self.assertFalse(ct.is_rollout(path))

    def test_missing_file_is_not_a_rollout(self):
        self.assertFalse(ct.is_rollout(Path("nope-does-not-exist.jsonl")))


if __name__ == "__main__":
    unittest.main()
