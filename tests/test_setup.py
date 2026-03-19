#!/usr/bin/env python3
"""Tests for config file creation, merging, teardown, and set_project."""
from __future__ import annotations

import json
import shutil
import tempfile
import unittest
from pathlib import Path

from tests.helpers import *
from agent_telemetry import AgentTelemetry


class TestSetupTeardown(unittest.TestCase):

    def setUp(self):
        self.tmpdir = tempfile.mkdtemp(prefix="agent_telemetry_test_")
        self.at = AgentTelemetry()

    def tearDown(self):
        shutil.rmtree(self.tmpdir, ignore_errors=True)

    def test_setup_creates_claude_config(self):
        self.at.setup_telemetry(project="test-proj", subproject="sub1", path=self.tmpdir)
        config = Path(self.tmpdir) / ".claude" / "settings.local.json"
        self.assertTrue(config.exists(), "Claude config should be created")
        data = json.loads(config.read_text())
        self.assertEqual(data["env"]["CLAUDE_CODE_ENABLE_TELEMETRY"], "1")
        self.assertIn("project=test-proj", data["env"]["OTEL_RESOURCE_ATTRIBUTES"])
        self.assertIn("subproject=sub1", data["env"]["OTEL_RESOURCE_ATTRIBUTES"])

    def test_setup_creates_codex_config(self):
        self.at.setup_telemetry(project="test-proj", subproject="sub1", path=self.tmpdir)
        config = Path(self.tmpdir) / ".codex" / "config.toml"
        self.assertTrue(config.exists(), "Codex config should be created")
        content = config.read_text()
        self.assertIn("[otel]", content)
        self.assertIn('environment = "project=test-proj,subproject=sub1"', content)
        self.assertIn("[otel.exporter.otlp-http]", content)

    def test_setup_merges_claude_config(self):
        """Existing non-OTEL settings should be preserved."""
        claude_dir = Path(self.tmpdir) / ".claude"
        claude_dir.mkdir(parents=True)
        existing = {"permissions": {"allow": ["Bash(git:*)"]}, "env": {"MY_VAR": "keep_me"}}
        (claude_dir / "settings.local.json").write_text(json.dumps(existing))

        self.at.setup_telemetry(project="test-proj", path=self.tmpdir)
        data = json.loads((claude_dir / "settings.local.json").read_text())

        self.assertEqual(data["permissions"]["allow"], ["Bash(git:*)"])
        self.assertEqual(data["env"]["MY_VAR"], "keep_me")
        self.assertEqual(data["env"]["CLAUDE_CODE_ENABLE_TELEMETRY"], "1")

    def test_setup_merges_codex_config(self):
        """Existing non-OTEL sections should be preserved."""
        codex_dir = Path(self.tmpdir) / ".codex"
        codex_dir.mkdir(parents=True)
        existing = '[analytics]\nenabled = false\n'
        (codex_dir / "config.toml").write_text(existing)

        self.at.setup_telemetry(project="test-proj", path=self.tmpdir)
        content = (codex_dir / "config.toml").read_text()

        self.assertIn("[analytics]", content)
        self.assertIn("enabled = false", content)
        self.assertIn("[otel]", content)

    def test_setup_replaces_existing_otel_in_codex(self):
        """Existing [otel] sections should be replaced, not duplicated."""
        codex_dir = Path(self.tmpdir) / ".codex"
        codex_dir.mkdir(parents=True)
        existing = '[otel]\nenvironment = "old-project"\nlog_user_prompt = true\n'
        (codex_dir / "config.toml").write_text(existing)

        self.at.setup_telemetry(project="new-project", path=self.tmpdir)
        content = (codex_dir / "config.toml").read_text()

        self.assertNotIn("old-project", content)
        self.assertIn("new-project", content)
        self.assertEqual(content.count("[otel]"), 1)

    def test_teardown_removes_claude_config(self):
        self.at.setup_telemetry(project="test-proj", path=self.tmpdir)
        config = Path(self.tmpdir) / ".claude" / "settings.local.json"
        self.assertTrue(config.exists())

        self.at.teardown(self.tmpdir)
        self.assertFalse(config.exists(), "Config with only OTEL keys should be deleted")

    def test_teardown_preserves_non_otel_claude(self):
        claude_dir = Path(self.tmpdir) / ".claude"
        claude_dir.mkdir(parents=True)
        existing = {"permissions": {"allow": ["Bash(git:*)"]}}
        (claude_dir / "settings.local.json").write_text(json.dumps(existing))

        self.at.setup_telemetry(project="test-proj", path=self.tmpdir)
        self.at.teardown(self.tmpdir)

        config = claude_dir / "settings.local.json"
        self.assertTrue(config.exists(), "Config with non-OTEL keys should be preserved")
        data = json.loads(config.read_text())
        self.assertIn("permissions", data)
        self.assertNotIn("env", data)

    def test_teardown_removes_codex_config(self):
        self.at.setup_telemetry(project="test-proj", path=self.tmpdir)
        config = Path(self.tmpdir) / ".codex" / "config.toml"
        self.assertTrue(config.exists())

        self.at.teardown(self.tmpdir)
        self.assertFalse(config.exists(), "Config with only OTEL sections should be deleted")

    def test_teardown_preserves_non_otel_codex(self):
        codex_dir = Path(self.tmpdir) / ".codex"
        codex_dir.mkdir(parents=True)
        existing = '[analytics]\nenabled = false\n'
        (codex_dir / "config.toml").write_text(existing)

        self.at.setup_telemetry(project="test-proj", path=self.tmpdir)
        self.at.teardown(self.tmpdir)

        config = codex_dir / "config.toml"
        self.assertTrue(config.exists(), "Config with non-OTEL sections should be preserved")
        content = config.read_text()
        self.assertIn("[analytics]", content)
        self.assertNotIn("[otel]", content)

    def test_set_project_updates_configs(self):
        self.at.setup_telemetry(project="proj-a", subproject="sub-a", path=self.tmpdir)

        self.at.set_project("proj-b", "sub-b")

        claude_data = json.loads(
            (Path(self.tmpdir) / ".claude" / "settings.local.json").read_text()
        )
        self.assertIn("project=proj-b", claude_data["env"]["OTEL_RESOURCE_ATTRIBUTES"])
        self.assertIn("subproject=sub-b", claude_data["env"]["OTEL_RESOURCE_ATTRIBUTES"])

        codex_content = (Path(self.tmpdir) / ".codex" / "config.toml").read_text()
        self.assertIn("proj-b", codex_content)
        self.assertNotIn("proj-a", codex_content)


if __name__ == "__main__":
    unittest.main(verbosity=2)
