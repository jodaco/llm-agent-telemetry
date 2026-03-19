#!/usr/bin/env python3
"""Integration test: run Claude with telemetry and verify JSON output."""
from __future__ import annotations

import json
import shutil
import subprocess
import tempfile
import time
import unittest
from pathlib import Path

from tests.helpers import *
from agent_telemetry import AgentTelemetry


class TestClaudeIntegration(unittest.TestCase):

    @classmethod
    def setUpClass(cls):
        if not which("claude"):
            print("WARNING: claude not in PATH, skipping Claude integration tests")

    def setUp(self):
        if not which("claude"):
            self.skipTest("claude not in PATH")
        self.tmpdir = tempfile.mkdtemp(prefix="agent_telemetry_claude_")
        self.output_dir = make_test_output_dir()
        self.port = find_free_port()
        self.at = AgentTelemetry()
        self.at.project = "collector-test"
        self.at.subproject = "claude"
        self.at.port = self.port
        self.at.output_dir = self.output_dir
        self.at.endpoint = "http://127.0.0.1:{}".format(self.port)
        self.at._path = self.tmpdir
        # Write Claude config to temp dir — never touches project root
        self.at._write_claude_config(self.tmpdir)
        self.at.start_telemetry()
        self.assertTrue(wait_for_port(self.port), "Receiver should start")

    def tearDown(self):
        self.at.stop_telemetry()
        shutil.rmtree(self.tmpdir, ignore_errors=True)
        shutil.rmtree(self.output_dir, ignore_errors=True)

    def test_claude_generates_telemetry(self):
        result = subprocess.run(
            ["claude", "-p", "Say hello in one word", "--allowedTools", ""],
            capture_output=True, text=True, timeout=60,
            cwd=self.tmpdir,
        )
        time.sleep(3)

        # Verify telemetry landed in the output dir under a service-prefixed project
        base = Path(self.output_dir)
        matches = list(base.glob("*-collector-test/claude"))
        self.assertGreater(len(matches), 0, "Output directory should exist (*-collector-test/claude/)")
        out_dir = matches[0]

        files = list(out_dir.glob("otel-*.json"))
        self.assertGreater(len(files), 0, "Should have captured at least one event")

        signals = set()
        for f in files:
            data = json.loads(f.read_text())
            signals.add(data.get("signal"))
        self.assertIn("log", signals, "Should have captured log events")


if __name__ == "__main__":
    unittest.main(verbosity=2)
