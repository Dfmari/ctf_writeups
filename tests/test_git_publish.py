"""Safety checks for explicitly publishing Git changes to dev."""

import subprocess
import sys
import unittest
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import prepare_publish as pp


class GitPublishTests(unittest.TestCase):
    def setUp(self):
        self.repo = Path("example-repo")
        self.available = patch.object(pp, "git_available", return_value=True)
        self.branch = patch.object(pp, "git_head", return_value="dev")
        self.run = patch.object(pp.subprocess, "run", return_value=subprocess.CompletedProcess([], 0, "ok", ""))
        self.available.start()
        self.head = self.branch.start()
        self.commands = self.run.start()
        self.addCleanup(self.available.stop)
        self.addCleanup(self.branch.stop)
        self.addCleanup(self.run.stop)

    def test_dev_push_uses_explicit_remote_ref(self):
        success, output = pp.git_commit_and_push_dev(self.repo, "Development update")
        self.assertTrue(success)
        self.assertEqual([call.args[0][3:] for call in self.commands.call_args_list], [
            ["add", "-A"],
            ["commit", "-m", "Development update"],
            ["push", "origin", "HEAD:refs/heads/dev"],
        ])
        self.assertIn("HEAD:refs/heads/dev", output)

    def test_main_and_detached_head_rejected_before_staging(self):
        for branch in ("main", "HEAD", "feature", ""):
            with self.subTest(branch=branch):
                self.head.return_value = branch
                success, output = pp.git_commit_and_push_dev(self.repo, "Update")
                self.assertFalse(success)
                self.assertIn("Switch to the local dev branch", output)
                self.commands.assert_not_called()

    def test_current_branch_behavior_unchanged(self):
        success, _ = pp.git_commit_and_push(self.repo, "Update")
        self.assertTrue(success)
        self.assertEqual(self.commands.call_args.args[0][3:], ["push"])
        self.head.assert_not_called()

    def test_empty_message_does_not_stage(self):
        success, _ = pp.git_commit_and_push_dev(self.repo, "  ")
        self.assertFalse(success)
        self.commands.assert_not_called()

    def test_invalid_target_does_not_stage(self):
        success, _ = pp.git_commit_and_push(self.repo, "Update", target_branch="main")
        self.assertFalse(success)
        self.commands.assert_not_called()

    def test_non_repository_does_not_stage(self):
        with patch.object(pp, "git_available", return_value=False):
            success, _ = pp.git_commit_and_push_dev(self.repo, "Update")
        self.assertFalse(success)
        self.commands.assert_not_called()

    def test_failed_stage_or_commit_never_pushes(self):
        for failure_index in (0, 1):
            with self.subTest(failure_index=failure_index):
                self.commands.reset_mock()
                self.commands.side_effect = [subprocess.CompletedProcess([], 0, "", "")] * failure_index + [
                    subprocess.CompletedProcess([], 1, "", "operation failed")
                ]
                success, output = pp.git_commit_and_push_dev(self.repo, "Update")
                self.assertFalse(success)
                self.assertIn("operation failed", output)
                self.assertEqual(self.commands.call_count, failure_index + 1)

    def test_failed_dev_push_has_no_fallback_to_main(self):
        self.commands.side_effect = [
            subprocess.CompletedProcess([], 0, "", ""),
            subprocess.CompletedProcess([], 0, "", ""),
            subprocess.CompletedProcess([], 1, "", "push rejected"),
        ]
        success, output = pp.git_commit_and_push_dev(self.repo, "Update")
        self.assertFalse(success)
        self.assertIn("push rejected", output)
        self.assertEqual(self.commands.call_count, 3)


if __name__ == "__main__":
    unittest.main()
