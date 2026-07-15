"""Tests for the local-regex sentiment markers (count_profanity + count_appreciation).

These are NOT the LLM-oracle scores. They are deterministic regex counts that
backend/server.py surfaces as `sentiment.profanity_total` and
`sentiment.appreciation_total`. Calibration here = stable across runs.
"""
from __future__ import annotations

import importlib.util
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "tracker"))

# estimate-task.py has a dash in the filename — load via spec.
spec = importlib.util.spec_from_file_location(
    "estimate_task", ROOT / "tracker" / "estimate-task.py"
)
estimate_task = importlib.util.module_from_spec(spec)
spec.loader.exec_module(estimate_task)


class CountProfanityTests(unittest.TestCase):
    def test_clean_text_returns_zero(self) -> None:
        self.assertEqual(0, estimate_task.count_profanity([
            "Run the build please.",
            "Looks good, let's deploy.",
        ]))

    def test_ru_mat_counted(self) -> None:
        count = estimate_task.count_profanity([
            "опять ёбаная ошибка",
            "это говно не работает",
            "бля сколько можно",
        ])
        # 3 distinct hits at minimum
        self.assertGreaterEqual(count, 3)

    def test_en_profanity_counted(self) -> None:
        count = estimate_task.count_profanity([
            "what the fuck",
            "this shit is broken",
        ])
        self.assertGreaterEqual(count, 2)

    def test_inflected_forms_match(self) -> None:
        # All these are the same family but different inflections
        count = estimate_task.count_profanity([
            "заебал этот баг",  # ёб/еб family
            "выебал режим",
            "ебать",
        ])
        self.assertGreaterEqual(count, 3)


class CountAppreciationTests(unittest.TestCase):
    def test_clean_text_returns_zero(self) -> None:
        self.assertEqual(0, estimate_task.count_appreciation([
            "Just numbers here: 1, 2, 3.",
            "Config loaded.",
        ]))

    def test_direct_thanks_counted(self) -> None:
        count = estimate_task.count_appreciation([
            "спасибо большое",
            "благодарю",
            "thanks!",
            "thank you",
        ])
        self.assertGreaterEqual(count, 4)

    def test_short_acks_not_counted(self) -> None:
        # Tightened lexicon (10164→30 fix): bare acks/approval markers are NOT
        # gratitude — «отлично» means "proceed", not "thank you". They must not
        # inflate the appreciation count.
        count = estimate_task.count_appreciation([
            "отлично",
            "круто",
            "классно",
            "хорошо",
            "норм",
            "зашло",
            "то что надо",
        ])
        self.assertEqual(count, 0)

    def test_momentum_phrases_not_counted(self) -> None:
        # Momentum/continuation markers are energy, not gratitude.
        count = estimate_task.count_appreciation([
            "давай ещё",
            "погнали",
            "продолжай",
            "дальше",
            "keep going",
            "next step",
        ])
        self.assertEqual(count, 0)

    def test_emoji_not_counted(self) -> None:
        # Celebratory/momentum emoji read as energy, not thanks — excluded.
        count = estimate_task.count_appreciation([
            "🚀",
            "👍",
            "❤️",
            "🔥💯🎉",
        ])
        self.assertEqual(count, 0)

    def test_playful_not_counted(self) -> None:
        # Laughter and «))» smileys are not gratitude markers.
        count = estimate_task.count_appreciation([
            "ахаха",
            "ну круто))",
            "ага)",
        ])
        self.assertEqual(count, 0)

    def test_profanity_as_positive_not_counted_as_appreciation(self) -> None:
        # The old profanity-as-positive carve-out was dropped: «охуенно»/«нихуя
        # себе» are logged by the profanity counter, not the appreciation one.
        count = estimate_task.count_appreciation([
            "охуенно вышло",
            "нихуя себе",
        ])
        self.assertEqual(count, 0)

    def test_english_only_strong_praise_counted(self) -> None:
        # Generic acks ("perfect", "looks good", "nice one") don't count; only
        # unambiguous praise ("awesome") does.
        count = estimate_task.count_appreciation([
            "great work",   # no
            "perfect",      # no
            "looks good",   # no
            "awesome",      # yes
            "nice one",     # no
        ])
        self.assertEqual(count, 1)


class SymmetryTests(unittest.TestCase):
    """Both counters consume the same user_messages list shape."""

    def test_both_handle_empty_list(self) -> None:
        self.assertEqual(0, estimate_task.count_profanity([]))
        self.assertEqual(0, estimate_task.count_appreciation([]))

    def test_both_handle_empty_strings(self) -> None:
        self.assertEqual(0, estimate_task.count_profanity(["", "  ", "\n"]))
        self.assertEqual(0, estimate_task.count_appreciation(["", "  ", "\n"]))

    def test_mixed_message_both_signals(self) -> None:
        # Same message can contribute to both counters when context differs.
        messages = [
            "спасибо, заебись получилось",  # appreciation + mat
            "охуенно отлично",  # appreciation × 2 (profanity-as-positive + direct ack)
        ]
        profanity = estimate_task.count_profanity(messages)
        appreciation = estimate_task.count_appreciation(messages)
        self.assertGreater(profanity, 0)
        self.assertGreater(appreciation, 0)
        # Profanity here is mostly carve-out cases (counted in BOTH on purpose):
        # «заебись» pattern catches as profanity, «охуенно» catches as positive.
        # That's intentional — each signal asks a different question.


if __name__ == "__main__":
    unittest.main()
