"""Tests for the human-attention denominator (summary.py).

The productivity multiplier's denominator counts the *human's* engaged time
(genuine prompts) rather than *AI busy time* (every event). These tests pin the
prompt/tool-result discrimination, the parallel-overlap behaviour, and the
missing-transcript fallback.
"""

import importlib.util
import json
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

_spec = importlib.util.spec_from_file_location(
    "summary_under_test", str(Path(__file__).resolve().parents[1] / "tracker" / "summary.py")
)
summary = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(summary)


def _user_prompt(text):
    return {"type": "user", "message": {"role": "user", "content": text}}


def _user_text_blocks(text):
    return {"type": "user", "message": {"role": "user", "content": [{"type": "text", "text": text}]}}


def _tool_result():
    return {
        "type": "user",
        "message": {"role": "user", "content": [{"type": "tool_result", "content": "ok"}]},
    }


def _sidechain_prompt():
    return {"type": "user", "isSidechain": True, "message": {"role": "user", "content": "sub-agent"}}


class IsHumanPromptTests(unittest.TestCase):
    def test_genuine_string_prompt(self):
        self.assertTrue(summary.is_human_prompt(_user_prompt("сделай X")))

    def test_genuine_text_block_prompt(self):
        self.assertTrue(summary.is_human_prompt(_user_text_blocks("hello")))

    def test_tool_result_is_not_human(self):
        self.assertFalse(summary.is_human_prompt(_tool_result()))

    def test_sidechain_is_not_human(self):
        self.assertFalse(summary.is_human_prompt(_sidechain_prompt()))

    def test_assistant_is_not_human(self):
        self.assertFalse(summary.is_human_prompt({"type": "assistant", "message": {"role": "assistant"}}))

    def test_empty_prompt_is_not_human(self):
        self.assertFalse(summary.is_human_prompt(_user_prompt("   ")))


class ReadHumanTimestampsTests(unittest.TestCase):
    def test_parses_only_human_prompts(self):
        base = datetime(2026, 5, 20, 10, 0, tzinfo=timezone.utc)
        lines = [
            {**_user_prompt("a"), "timestamp": base.isoformat()},
            {**_tool_result(), "timestamp": (base + timedelta(seconds=5)).isoformat()},
            {**_tool_result(), "timestamp": (base + timedelta(seconds=10)).isoformat()},
            {**_user_prompt("b"), "timestamp": (base + timedelta(minutes=20)).isoformat()},
            {**_sidechain_prompt(), "timestamp": (base + timedelta(minutes=21)).isoformat()},
        ]
        with tempfile.TemporaryDirectory() as d:
            p = Path(d) / "t.jsonl"
            p.write_text("\n".join(json.dumps(x) for x in lines), encoding="utf-8")
            ts = summary.read_human_message_timestamps(p)
        self.assertEqual(len(ts), 2)  # 2 genuine prompts, not the 2 tool_results / sidechain

    def test_missing_file_returns_empty(self):
        self.assertEqual(summary.read_human_message_timestamps(Path("does-not-exist.jsonl")), [])


class HumanAttentionHoursTests(unittest.TestCase):
    def test_parallel_overlap_human_lt_ai(self):
        # Two sessions: dense AI events (every 30s for 30 min) but only 2 sparse
        # human prompts each. Human attention must be << AI-active.
        base = datetime(2026, 5, 20, 12, 0, tzinfo=timezone.utc)
        ai_ts = {"s1": [], "s2": []}
        with tempfile.TemporaryDirectory() as d:
            tasks = {}
            for sid, offset in (("s1", 0), ("s2", 1)):
                lines = []
                # dense AI assistant events
                for i in range(60):
                    t = base + timedelta(seconds=offset + i * 30)
                    ai_ts[sid].append(t)
                    lines.append({"type": "assistant", "message": {"role": "assistant"},
                                  "timestamp": t.isoformat()})
                # two human prompts 25 min apart (> 5-min gap → not bridged)
                for j, mins in enumerate((0, 25)):
                    lines.append({**_user_prompt(f"p{j}"),
                                  "timestamp": (base + timedelta(minutes=mins)).isoformat()})
                p = Path(d) / f"{sid}.jsonl"
                p.write_text("\n".join(json.dumps(x) for x in lines), encoding="utf-8")
                tasks[sid] = {"transcript_path": str(p)}

            human_h, fb = summary.human_attention_hours(["s1", "s2"], tasks, ai_ts)
            ai_merged = summary.active_time_hours_merged(
                [{"session_id": s, "ts": t.isoformat()} for s in ai_ts for t in ai_ts[s]], 2
            )
        self.assertEqual(fb, 0)
        self.assertLess(human_h, ai_merged)   # human attention strictly below AI-busy
        self.assertEqual(human_h, 0.0)         # prompts 25 min apart, never bridged at 5 min

    def test_missing_transcript_falls_back_to_ai(self):
        base = datetime(2026, 5, 20, 9, 0, tzinfo=timezone.utc)
        ai_ts = {"s1": [base, base + timedelta(minutes=2), base + timedelta(minutes=4)]}
        tasks = {"s1": {"transcript_path": "nonexistent.jsonl"}}
        human_h, fb = summary.human_attention_hours(["s1"], tasks, ai_ts)
        self.assertEqual(fb, 1)
        self.assertAlmostEqual(human_h, 4 / 60, places=4)  # 2+2 min bridged at 5-min gap

    def test_empty_input(self):
        self.assertEqual(summary.human_attention_hours([], {}, {}), (0.0, 0))

    def test_chunk_unit_restricts_to_day(self):
        # A 2-day session; chunk unit for day-1 must ignore day-2 prompts (Q4).
        d1 = datetime(2026, 5, 20, 10, 0, tzinfo=timezone.utc)
        d2 = datetime(2026, 5, 21, 10, 0, tzinfo=timezone.utc)
        with tempfile.TemporaryDirectory() as d:
            lines = [
                {**_user_prompt("a"), "timestamp": d1.isoformat()},
                {**_user_prompt("b"), "timestamp": (d1 + timedelta(minutes=3)).isoformat()},
                {**_user_prompt("c"), "timestamp": d2.isoformat()},
                {**_user_prompt("d"), "timestamp": (d2 + timedelta(minutes=3)).isoformat()},
            ]
            p = Path(d) / "s.jsonl"
            p.write_text("\n".join(json.dumps(x) for x in lines), encoding="utf-8")
            tasks = {"s": {"transcript_path": str(p)}}
            # whole session: two 3-min spans bridged at 5-min gap = 6 min total
            whole, _ = summary._human_attention_hours_for_units([("s", None)], tasks, {})
            # day-1 only: a single 3-min span
            day1, _ = summary._human_attention_hours_for_units([("s", "2026-05-20")], tasks, {})
        self.assertAlmostEqual(whole, 6 / 60, places=4)
        self.assertAlmostEqual(day1, 3 / 60, places=4)
        self.assertLess(day1, whole)  # day restriction excludes day-2 prompts


if __name__ == "__main__":
    unittest.main()
