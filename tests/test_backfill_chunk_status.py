import contextlib
import importlib.util
import io
import sys
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path
from unittest import mock


ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))
if str(ROOT / "tracker") not in sys.path:
    sys.path.insert(0, str(ROOT / "tracker"))


def load_script(filename):
    path = ROOT / "tracker" / filename
    spec = importlib.util.spec_from_file_location(filename.removesuffix(".py"), path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class BackfillChunkStatusTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.scripts = (
            load_script("backfill-codex-estimates.py"),
            load_script("backfill-stranded-estimates.py"),
        )

    def test_session_baseline_without_complete_marker_is_partial(self):
        for script in self.scripts:
            with self.subTest(script=script.__name__):
                status, hours, progress = script.estimation_status({
                    "ai_baseline_hours": 160.0,
                    "chunk_estimation_complete": False,
                    "chunk_estimation_completed": 10,
                    "chunk_estimation_expected": 46,
                })
                self.assertEqual(status, "partial")
                self.assertEqual(hours, 160.0)
                self.assertEqual(progress, "чанки 10/46")

    def test_only_explicitly_complete_chunking_is_success(self):
        for script in self.scripts:
            with self.subTest(script=script.__name__):
                status, hours, progress = script.estimation_status({
                    "ai_baseline_hours": 12.0,
                    "chunk_estimation_complete": True,
                    "chunk_estimation_completed": 3,
                    "chunk_estimation_expected": 3,
                })
                self.assertEqual(status, "complete")
                self.assertEqual(hours, 12.0)
                self.assertEqual(progress, "чанки 3/3")

    def test_legacy_hours_without_status_are_not_reported_as_complete(self):
        for script in self.scripts:
            with self.subTest(script=script.__name__):
                status, _hours, progress = script.estimation_status({
                    "ai_baseline_hours": 8.0,
                })
                self.assertEqual(status, "partial")
                self.assertEqual(progress, "чанки ?/?")

    def test_timeout_with_session_baseline_is_visible_as_partial(self):
        session_id = "partial-session"
        partial_entry = {
            "ai_baseline_hours": 160.0,
            "chunk_estimation_complete": False,
            "chunk_estimation_completed": 10,
            "chunk_estimation_expected": 46,
        }
        now = datetime.now(timezone.utc)

        with tempfile.TemporaryDirectory() as tempdir:
            transcript = Path(tempdir) / "session.jsonl"
            transcript.write_text("{}\n", encoding="utf-8")
            for script in self.scripts:
                with self.subTest(script=script.__name__):
                    if script.__name__ == "backfill-codex-estimates":
                        rows = [(session_id, transcript, now, 1)]
                        args = ["--sessions-root", tempdir]
                        row_patch = mock.patch.object(
                            script, "candidates", return_value=rows
                        )
                    else:
                        rows = [(session_id, transcript, now)]
                        args = []
                        row_patch = mock.patch.object(
                            script, "stranded_sessions", return_value=rows
                        )

                    stdout = io.StringIO()
                    with (
                        row_patch,
                        mock.patch.object(
                            script.summary,
                            "read_tasks",
                            side_effect=[{}, {session_id: partial_entry}],
                        ),
                        mock.patch.object(
                            script.subprocess,
                            "run",
                            side_effect=script.subprocess.TimeoutExpired(
                                cmd="estimator", timeout=600
                            ),
                        ),
                        contextlib.redirect_stdout(stdout),
                    ):
                        self.assertEqual(script.main(args), 0)

                    output = stdout.getvalue()
                    self.assertIn("нарезка не завершена (чанки 10/46)", output)
                    self.assertIn("таймаут", output)
                    self.assertIn("частично 1", output)


if __name__ == "__main__":
    unittest.main()
