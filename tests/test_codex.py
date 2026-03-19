#!/usr/bin/env python3
"""Integration test: run Codex with telemetry and verify JSON output."""
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


class TestCodexIntegration(unittest.TestCase):

    @classmethod
    def setUpClass(cls):
        if not which("codex"):
            print("WARNING: codex not in PATH, skipping Codex integration tests")

    def setUp(self):
        if not which("codex"):
            self.skipTest("codex not in PATH")
        self.tmpdir = tempfile.mkdtemp(prefix="agent_telemetry_codex_")
        self.output_dir = make_test_output_dir()
        # Codex requires a trusted directory (git repo)
        subprocess.run(["git", "init"], cwd=self.tmpdir, capture_output=True)
        self.port = find_free_port()
        self.at = AgentTelemetry()
        self.at.project = "collector-test"
        self.at.subproject = "codex"
        self.at.port = self.port
        self.at.output_dir = self.output_dir
        self.at.endpoint = "http://127.0.0.1:{}".format(self.port)
        self.at._path = self.tmpdir
        # Write Codex config to temp dir
        self.at._write_codex_config(self.tmpdir)
        self.at.start_telemetry()
        self.assertTrue(wait_for_port(self.port), "Receiver should start")

    def tearDown(self):
        self.at.stop_telemetry()
        shutil.rmtree(self.tmpdir, ignore_errors=True)
        shutil.rmtree(self.output_dir, ignore_errors=True)

    def test_codex_generates_telemetry(self):
        # Pass OTEL config via -c flags
        endpoint = "http://127.0.0.1:{}".format(self.port)
        result = subprocess.run(
            [
                "codex", "exec",
                "-c", 'otel.environment="project=collector-test,subproject=codex"',
                "-c", "otel.log_user_prompt=true",
                "-c", 'otel.exporter.otlp-http.endpoint="{}/v1/logs"'.format(endpoint),
                "-c", 'otel.exporter.otlp-http.protocol="json"',
                "-c", 'otel.metrics_exporter.otlp-http.endpoint="{}/v1/metrics"'.format(endpoint),
                "-c", 'otel.metrics_exporter.otlp-http.protocol="json"',
                "Say hello in one word",
            ],
            capture_output=True, text=True, timeout=60,
            cwd=self.tmpdir,
        )
        time.sleep(15)

        # Codex sets its own project/subproject from the git repo name,
        # and service.name varies by version (codex_exec, codex_cli_rs).
        # Look for any codex-prefixed directory with otel files.
        base = Path(self.output_dir)
        otel_files = []
        if base.is_dir():
            for match in sorted(base.iterdir()):
                if not match.is_dir() or "codex" not in match.name:
                    continue
                found = list(match.rglob("otel-*.json"))
                if found:
                    otel_files = found
                    break

        if not otel_files:
            print("Codex stdout:", result.stdout[-200:] if result.stdout else "")
            print("Codex stderr:", result.stderr[-200:] if result.stderr else "")
            print("Codex returncode:", result.returncode)
            print("Output dirs:", [d.name for d in base.iterdir()] if base.is_dir() else "empty")
        self.assertGreater(len(otel_files), 0, "Should have captured at least one event")

        signals = set()
        for f in otel_files:
            data = json.loads(f.read_text())
            signals.add(data.get("signal"))
        self.assertIn("log", signals, "Should have captured log events")


if __name__ == "__main__":
    unittest.main(verbosity=2)
