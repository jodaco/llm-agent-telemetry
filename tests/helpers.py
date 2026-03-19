"""Shared test helpers for agent_telemetry tests."""
from __future__ import annotations

import json
import os
import shutil
import socket
import sys
import tempfile
import time
from pathlib import Path
from urllib.request import urlopen, Request

# Add parent to path so we can import agent_telemetry
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
TEST_PORT = 33333


def make_test_output_dir():
    """Create a unique temp directory for test output. Caller must clean up."""
    return tempfile.mkdtemp(prefix="agent_telemetry_output_")


def find_free_port():
    """Find a free port on localhost."""
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def which(cmd):
    """Check if a command is in PATH."""
    for p in os.environ.get("PATH", "").split(os.pathsep):
        full = os.path.join(p, cmd)
        if os.path.isfile(full) and os.access(full, os.X_OK):
            return full
    return None


def wait_for_port(port, timeout=5):
    """Wait for a port to become available."""
    start = time.time()
    while time.time() - start < timeout:
        try:
            with socket.create_connection(("127.0.0.1", port), timeout=1):
                return True
        except (ConnectionRefusedError, OSError):
            time.sleep(0.1)
    return False


def send_otel_payload(port, path, payload):
    """Send an OTLP JSON payload to the receiver."""
    url = "http://127.0.0.1:{}/{}".format(port, path)
    data = json.dumps(payload).encode("utf-8")
    req = Request(url, data=data, headers={"Content-Type": "application/json"})
    resp = urlopen(req, timeout=5)
    return json.loads(resp.read().decode("utf-8"))
