#!/usr/bin/env python3
"""Tests for the OTLP receiver: health check, logs, traces, metrics, filename prefix."""
from __future__ import annotations

import json
import shutil
import tempfile
import time
import unittest
from pathlib import Path
from urllib.request import urlopen

from tests.helpers import *
from agent_telemetry import AgentTelemetry


class TestReceiver(unittest.TestCase):

    def setUp(self):
        self.tmpdir = tempfile.mkdtemp(prefix="agent_telemetry_recv_")
        self.output_dir = make_test_output_dir()
        self.port = find_free_port()
        self.at = AgentTelemetry()
        self.at.setup_telemetry(
            project="curl-testing",
            subproject="test1",
            port=self.port,
            output_dir=self.output_dir,
            path=self.tmpdir,
        )
        self.at.start_telemetry()
        self.assertTrue(wait_for_port(self.port), "Receiver should start within 5s")

    def tearDown(self):
        self.at.stop_telemetry()
        shutil.rmtree(self.tmpdir, ignore_errors=True)
        shutil.rmtree(self.output_dir, ignore_errors=True)

    def test_receiver_health_check(self):
        resp = json.loads(urlopen("http://127.0.0.1:{}".format(self.port), timeout=5).read())
        self.assertTrue(resp.get("ok"))

    def test_receiver_accepts_logs(self):
        payload = {
            "resourceLogs": [{
                "resource": {
                    "attributes": [
                        {"key": "project", "value": {"stringValue": "curl-testing"}},
                        {"key": "subproject", "value": {"stringValue": "test1"}},
                        {"key": "service.name", "value": {"stringValue": "test-agent"}},
                    ]
                },
                "scopeLogs": [{
                    "logRecords": [{
                        "attributes": [
                            {"key": "event.name", "value": {"stringValue": "test.hello"}},
                            {"key": "event.timestamp", "value": {"stringValue": "2026-03-18T00:00:00Z"}},
                        ],
                        "body": {"stringValue": "hello from test"},
                    }]
                }]
            }]
        }
        resp = send_otel_payload(self.port, "v1/logs", payload)
        self.assertIn("partialSuccess", resp)

        out_dir = Path(self.output_dir) / "test-agent-curl-testing" / "test1"
        time.sleep(0.5)
        files = list(out_dir.glob("otel-*.json"))
        self.assertGreater(len(files), 0, "Should have at least one otel JSON file")

        data = json.loads(files[0].read_text())
        self.assertEqual(data["signal"], "log")
        self.assertEqual(data["event_name"], "test.hello")

    def test_receiver_accepts_traces(self):
        payload = {
            "resourceSpans": [{
                "resource": {
                    "attributes": [
                        {"key": "project", "value": {"stringValue": "curl-testing"}},
                        {"key": "subproject", "value": {"stringValue": "test2"}},
                        {"key": "service.name", "value": {"stringValue": "test-agent"}},
                    ]
                },
                "scopeSpans": [{
                    "spans": [{
                        "name": "test.span",
                        "traceId": "abc123",
                        "spanId": "def456",
                        "attributes": [],
                    }]
                }]
            }]
        }
        resp = send_otel_payload(self.port, "v1/traces", payload)
        self.assertIn("partialSuccess", resp)

        out_dir = Path(self.output_dir) / "test-agent-curl-testing" / "test2"
        time.sleep(0.5)
        files = list(out_dir.glob("otel-*.json"))
        self.assertGreater(len(files), 0)
        data = json.loads(files[0].read_text())
        self.assertEqual(data["signal"], "trace")

    def test_receiver_accepts_metrics(self):
        payload = {
            "resourceMetrics": [{
                "resource": {
                    "attributes": [
                        {"key": "project", "value": {"stringValue": "curl-testing"}},
                        {"key": "subproject", "value": {"stringValue": "test3"}},
                        {"key": "service.name", "value": {"stringValue": "test-agent"}},
                    ]
                },
                "scopeMetrics": [{
                    "metrics": [{
                        "name": "test.counter",
                        "sum": {
                            "dataPoints": [{
                                "asDouble": 42.0,
                                "attributes": [],
                            }]
                        }
                    }]
                }]
            }]
        }
        resp = send_otel_payload(self.port, "v1/metrics", payload)
        self.assertIn("partialSuccess", resp)

        out_dir = Path(self.output_dir) / "test-agent-curl-testing" / "test3"
        time.sleep(0.5)
        files = list(out_dir.glob("otel-*.json"))
        self.assertGreater(len(files), 0)
        data = json.loads(files[0].read_text())
        self.assertEqual(data["signal"], "metric")

    def test_receiver_otel_prefix_in_filenames(self):
        payload = {
            "resourceLogs": [{
                "resource": {"attributes": [
                    {"key": "project", "value": {"stringValue": "curl-testing"}},
                    {"key": "subproject", "value": {"stringValue": "test1"}},
                    {"key": "service.name", "value": {"stringValue": "test-agent"}},
                ]},
                "scopeLogs": [{"logRecords": [{
                    "attributes": [{"key": "event.name", "value": {"stringValue": "prefix.check"}}],
                    "body": {"stringValue": "checking prefix"},
                }]}]
            }]
        }
        send_otel_payload(self.port, "v1/logs", payload)
        time.sleep(0.5)

        out_dir = Path(self.output_dir) / "test-agent-curl-testing" / "test1"
        for f in out_dir.glob("*.json"):
            self.assertTrue(f.name.startswith("otel-"), "File {} should start with otel-".format(f.name))


if __name__ == "__main__":
    unittest.main(verbosity=2)
