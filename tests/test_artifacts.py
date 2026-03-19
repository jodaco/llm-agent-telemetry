#!/usr/bin/env python3
"""Tests for save_artifacts: file copy, directory copy, agent-scoped save."""
from __future__ import annotations

import shutil
import tempfile
import unittest
from pathlib import Path

from tests.helpers import *
from agent_telemetry import AgentTelemetry


class TestSaveArtifacts(unittest.TestCase):

    def setUp(self):
        self.tmpdir = tempfile.mkdtemp(prefix="agent_telemetry_artifacts_")
        self.output_dir = make_test_output_dir()
        self.at = AgentTelemetry()
        self.at.setup_telemetry(
            project="artifact-test",
            subproject="sub1",
            output_dir=self.output_dir,
            path=self.tmpdir,
        )
        # Simulate the receiver having created the agent output directory
        (Path(self.output_dir) / "claude-code-artifact-test" / "sub1").mkdir(parents=True, exist_ok=True)

    def tearDown(self):
        shutil.rmtree(self.tmpdir, ignore_errors=True)
        shutil.rmtree(self.output_dir, ignore_errors=True)

    def test_save_file(self):
        src = Path(self.tmpdir) / "results.txt"
        src.write_text("test results")

        dest = self.at.save_artifacts("claude", str(src))
        self.assertTrue(dest.exists())
        self.assertEqual(dest.read_text(), "test results")
        self.assertIn("claude-code-artifact-test", str(dest))
        self.assertIn("artifacts", str(dest))
        self.assertIn("sub1", str(dest))

    def test_save_directory(self):
        src_dir = Path(self.tmpdir) / "logs"
        src_dir.mkdir()
        (src_dir / "a.txt").write_text("a")
        (src_dir / "b.txt").write_text("b")

        dest = self.at.save_artifacts("claude", str(src_dir))
        self.assertTrue(dest.is_dir())
        self.assertTrue((dest / "a.txt").exists())
        self.assertTrue((dest / "b.txt").exists())

    def test_save_to_project_level_without_subproject(self):
        self.at.subproject = ""
        src = Path(self.tmpdir) / "notes.txt"
        src.write_text("notes")

        dest = self.at.save_artifacts("claude", str(src))
        expected = Path(self.output_dir) / "claude-code-artifact-test" / "artifacts" / "notes.txt"
        self.assertEqual(dest, expected)

    def test_save_artifacts_across_subprojects(self):
        """Save artifacts to multiple subprojects via set_project."""
        # Create codex dirs too
        (Path(self.output_dir) / "codex_exec-artifact-test" / "sub3").mkdir(parents=True, exist_ok=True)

        # Subproject 1 — claude
        src1 = Path(self.tmpdir) / "report1.txt"
        src1.write_text("report for sub1")
        self.at.save_artifacts("claude", str(src1))

        # Switch to subproject 2 — claude
        self.at.set_project("artifact-test", "sub2")
        src2 = Path(self.tmpdir) / "report2.txt"
        src2.write_text("report for sub2")
        self.at.save_artifacts("claude", str(src2))

        # Switch to subproject 3 — codex
        self.at.set_project("artifact-test", "sub3")
        src3 = Path(self.tmpdir) / "analysis.c"
        src3.write_text("int main() { return 0; }")
        log_dir = Path(self.tmpdir) / "build-logs"
        log_dir.mkdir()
        (log_dir / "make.log").write_text("gcc -o analysis analysis.c")
        self.at.save_artifacts("codex", str(src3))
        self.at.save_artifacts("codex", str(log_dir))

        # Verify claude artifacts (under artifacts/ subfolder)
        claude_base = Path(self.output_dir) / "claude-code-artifact-test"
        self.assertTrue((claude_base / "sub1" / "artifacts" / "report1.txt").exists())
        self.assertEqual((claude_base / "sub1" / "artifacts" / "report1.txt").read_text(), "report for sub1")
        self.assertTrue((claude_base / "sub2" / "artifacts" / "report2.txt").exists())

        # Verify codex artifacts
        codex_base = Path(self.output_dir) / "codex_exec-artifact-test"
        self.assertTrue((codex_base / "sub3" / "artifacts" / "analysis.c").exists())
        self.assertTrue((codex_base / "sub3" / "artifacts" / "build-logs" / "make.log").exists())

    def test_save_falls_back_when_no_dir_exists(self):
        """When no matching directory exists, creates {agent}-{project}/."""
        src = Path(self.tmpdir) / "file.txt"
        src.write_text("test")
        self.at.project = "new-project"
        self.at.subproject = "s1"

        dest = self.at.save_artifacts("myagent", str(src))
        self.assertTrue(dest.exists())
        self.assertIn("myagent-new-project", str(dest))
        self.assertIn("s1", str(dest))
        self.assertIn("artifacts", str(dest))

    def test_save_matches_codex_cli_rs(self):
        """Glob matches codex_cli_rs- prefix when searching for 'codex'."""
        # Simulate codex_cli_rs receiver output
        (Path(self.output_dir) / "codex_cli_rs-artifact-test" / "sub1").mkdir(parents=True, exist_ok=True)

        src = Path(self.tmpdir) / "output.txt"
        src.write_text("codex output")

        dest = self.at.save_artifacts("codex", str(src))
        self.assertTrue(dest.exists())
        # Should match codex_cli_rs- or codex_exec-, not fall back to codex-
        self.assertIn("codex_", str(dest))


if __name__ == "__main__":
    unittest.main(verbosity=2)
