"""Unit tests for Slack slash-command handlers.

Popen is mocked everywhere — these tests must never spawn a real process.
"""

import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from openevolve.integrations import slack


class DispatchArgCleaningTest(unittest.TestCase):
    """Slack mobile wraps tokens in backticks/bold; args must be stripped."""

    def test_strips_backticks(self):
        self.assertEqual(slack._clean_arg("`fun-test`"), "fun-test")

    def test_strips_bold_and_italic(self):
        self.assertEqual(slack._clean_arg("*fun-test*"), "fun-test")
        self.assertEqual(slack._clean_arg("_fun-test_"), "fun-test")

    def test_strips_surrounding_whitespace(self):
        self.assertEqual(slack._clean_arg("  fun-test  "), "fun-test")


class RunHandlerTest(unittest.TestCase):
    """/openevolve run <name> — validation + spawn path."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        # Point _experiments_root at our tmp dir
        self._patch = patch.object(slack, "_experiments_root", return_value=self.tmp.name)
        self._patch.start()
        self.addCleanup(self._patch.stop)

    def _make_experiment(self, name: str, complete: bool = True) -> Path:
        exp = Path(self.tmp.name) / name
        exp.mkdir(parents=True)
        if complete:
            for f in ("initial_program.py", "evaluator.py", "config.yaml"):
                (exp / f).touch()
        return exp

    def test_missing_arg_returns_usage(self):
        msg = slack._handle_run([])
        self.assertIn("Usage", msg)

    def test_rejects_path_traversal(self):
        msg = slack._handle_run(["../etc"])
        self.assertIn("Invalid experiment name", msg)

    def test_reports_missing_experiment(self):
        msg = slack._handle_run(["nope"])
        self.assertIn("No experiment `nope`", msg)

    def test_reports_incomplete_experiment(self):
        self._make_experiment("half", complete=False)
        msg = slack._handle_run(["half"])
        self.assertIn("missing", msg)
        self.assertIn("evaluator.py", msg)
        self.assertIn("config.yaml", msg)

    def test_spawns_for_valid_experiment(self):
        exp = self._make_experiment("good")
        with patch("subprocess.Popen") as mock_popen:
            msg = slack._handle_run(["good"])
        self.assertIn("Launched experiment `good`", msg)
        self.assertEqual(mock_popen.call_count, 1)
        argv = mock_popen.call_args.args[0]
        self.assertEqual(argv[-4], str(exp / "initial_program.py"))
        self.assertEqual(argv[-3], str(exp / "evaluator.py"))
        self.assertEqual(argv[-1], str(exp / "config.yaml"))
        self.assertEqual(mock_popen.call_args.kwargs["cwd"], str(exp))
        self.assertTrue(mock_popen.call_args.kwargs["start_new_session"])


class RerunHandlerTest(unittest.TestCase):
    """/openevolve rerun — guards against double-launch."""

    def test_no_manifest(self):
        with patch("openevolve.run_manifest.load_last_run", return_value=None):
            msg = slack._handle_rerun([])
        self.assertIn("No previous run found", msg)

    def test_active_run_guarded(self):
        with tempfile.TemporaryDirectory() as out:
            # usage.jsonl with run_start but no run_end -> still active
            with open(os.path.join(out, "usage.jsonl"), "w") as f:
                f.write(json.dumps({"event": "run_start", "run_id": "r1"}) + "\n")
            manifest = {
                "run_id": "r1",
                "output_dir": out,
                "argv": ["echo", "x"],
                "cwd": out,
            }
            with patch("openevolve.run_manifest.load_last_run", return_value=manifest):
                msg = slack._handle_rerun([])
        self.assertIn("still be active", msg)
        self.assertIn("force", msg)

    def test_force_spawns_even_when_active(self):
        with tempfile.TemporaryDirectory() as out:
            with open(os.path.join(out, "usage.jsonl"), "w") as f:
                f.write(json.dumps({"event": "run_start", "run_id": "r1"}) + "\n")
            manifest = {
                "run_id": "r1",
                "output_dir": out,
                "argv": ["echo", "x"],
                "cwd": out,
            }
            with patch("openevolve.run_manifest.load_last_run", return_value=manifest), patch(
                "subprocess.Popen"
            ) as mock_popen:
                msg = slack._handle_rerun(["force"])
        self.assertIn("Rerun launched", msg)
        mock_popen.assert_called_once()


if __name__ == "__main__":
    unittest.main()
