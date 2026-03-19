"""
Lightweight OTLP/HTTP receiver that stores clean, parsed events as flat JSON files
organized by project/subproject.

Accepts standard OTLP on :4318 (same port as the official collector).

File layout:
  {out_dir}/
    {project}/
      {subproject}/
        otel-{timestamp}-{seq:05d}-{signal}-{event_name}.json
"""
from __future__ import annotations

import argparse
import gzip
import json
import threading
from datetime import datetime, timezone
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any


def decode_body(raw, content_encoding):
    # type: (bytes, str | None) -> bytes
    if content_encoding == "gzip":
        return gzip.decompress(raw)
    return raw


def decode_any_value(value):
    # type: (Any) -> Any
    if not isinstance(value, dict):
        return value
    for key in ("stringValue", "intValue", "doubleValue", "boolValue", "bytesValue"):
        if key in value:
            return value[key]
    if "arrayValue" in value:
        return [decode_any_value(v) for v in value["arrayValue"].get("values", [])]
    if "kvlistValue" in value:
        return {
            item["key"]: decode_any_value(item.get("value"))
            for item in value["kvlistValue"].get("values", [])
            if "key" in item
        }
    return value


def attrs_to_dict(attributes):
    # type: (list | None) -> dict
    result = {}
    for item in attributes or []:
        key = item.get("key")
        if key:
            result[key] = decode_any_value(item.get("value"))
    return result


# Keys to check for project/subproject
PROJECT_KEYS = ("project",)
SUBPROJECT_KEYS = ("subproject",)


def parse_env_attrs(env_val):
    # type: (str) -> dict
    """Parse 'project=X,subproject=Y' style env string into a dict."""
    result = {}
    if "=" in env_val:
        for pair in env_val.split(","):
            if "=" in pair:
                k, v = pair.split("=", 1)
                result[k.strip()] = v.strip()
    return result


def pick_project_subproject(attrs):
    # type: (dict) -> tuple
    """Extract service name, project, and subproject from resource attributes.

    Priority for project/subproject:
    1. Explicit 'project'/'subproject' keys (set by Claude via OTEL_RESOURCE_ATTRIBUTES)
    2. Parse 'env' for 'project=X,subproject=Y' (set by Codex via otel.environment)
    3. Fall back to service.name / _default

    Returns (service_name, project, subproject). The service_name is used to
    prefix the project folder so data from different agents stays separated.
    """
    service_name = attrs.get("service.name", "")
    project = attrs.get("project")
    subproject = attrs.get("subproject")

    if not project:
        # Try parsing env for embedded key-value pairs
        env_val = attrs.get("env", "")
        if env_val and "=" in env_val:
            parsed = parse_env_attrs(env_val)
            project = parsed.get("project")
            subproject = subproject or parsed.get("subproject")

    if not project:
        # Fall back to other standard keys
        for key in ("service.name", "deployment.environment.name", "deployment.environment"):
            val = attrs.get(key)
            if val not in (None, "", "unknown"):
                project = str(val)
                break

    project = str(project) if project else "_default"
    subproject = str(subproject) if subproject else "_default"

    # Prefix project with service name so data from different agents stays separated
    if service_name:
        project = "{}-{}".format(service_name, project)

    return service_name, project, subproject


def safe_filename(s):
    # type: (str) -> str
    return "".join(c if c.isalnum() or c in "-_." else "_" for c in s)


class Receiver:
    def __init__(self, out_dir):
        # type: (Path) -> None
        self.out_dir = out_dir
        self._lock = threading.Lock()
        self._counter = 0

    def next_seq(self):
        # type: () -> int
        with self._lock:
            self._counter += 1
            return self._counter

    def write_event(self, project, subproject, signal, event_name, data):
        # type: (str, str, str, str, dict) -> Path
        seq = self.next_seq()
        ts = datetime.now(timezone.utc).strftime("%Y%m%d-%H%M%S")
        fname = "otel-{}-{:05d}-{}-{}.json".format(ts, seq, signal, safe_filename(event_name))

        dir_path = self.out_dir / safe_filename(project) / safe_filename(subproject)
        dir_path.mkdir(parents=True, exist_ok=True)

        out_path = dir_path / fname
        out_path.write_text(json.dumps(data, indent=2, sort_keys=False), encoding="utf-8")
        return out_path

    def process_logs(self, body):
        # type: (dict) -> tuple
        count = 0
        dest = None  # type: str | None
        for resource_log in body.get("resourceLogs", []):
            resource_attrs = attrs_to_dict(resource_log.get("resource", {}).get("attributes"))
            _, project, subproject = pick_project_subproject(resource_attrs)

            for scope_log in resource_log.get("scopeLogs", []):
                for record in scope_log.get("logRecords", []):
                    log_attrs = attrs_to_dict(record.get("attributes"))
                    merged = {}
                    merged.update(resource_attrs)
                    merged.update(log_attrs)
                    event_name = log_attrs.get("event.name", "log")
                    body_value = decode_any_value(record.get("body"))

                    event = {
                        "signal": "log",
                        "event_name": event_name,
                        "timestamp": log_attrs.get("event.timestamp", datetime.now(timezone.utc).isoformat()),
                        "project": project,
                        "subproject": subproject,
                        "body": body_value,
                        "attributes": merged,
                    }
                    self.write_event(project, subproject, "log", event_name, event)
                    dest = "{}/{}".format(safe_filename(project), safe_filename(subproject))
                    count += 1
        return count, dest

    def process_traces(self, body):
        # type: (dict) -> tuple
        count = 0
        dest = None  # type: str | None
        for resource_span in body.get("resourceSpans", []):
            resource_attrs = attrs_to_dict(resource_span.get("resource", {}).get("attributes"))
            _, project, subproject = pick_project_subproject(resource_attrs)

            for scope_span in resource_span.get("scopeSpans", []):
                for span in scope_span.get("spans", []):
                    span_attrs = attrs_to_dict(span.get("attributes"))
                    span_name = span.get("name", "span")

                    merged = {}
                    merged.update(resource_attrs)
                    merged.update(span_attrs)

                    event = {
                        "signal": "trace",
                        "span_name": span_name,
                        "trace_id": span.get("traceId", ""),
                        "span_id": span.get("spanId", ""),
                        "parent_span_id": span.get("parentSpanId", ""),
                        "start_time": span.get("startTimeUnixNano", ""),
                        "end_time": span.get("endTimeUnixNano", ""),
                        "project": project,
                        "subproject": subproject,
                        "attributes": merged,
                    }
                    self.write_event(project, subproject, "trace", span_name, event)
                    dest = "{}/{}".format(safe_filename(project), safe_filename(subproject))
                    count += 1
        return count, dest

    def process_metrics(self, body):
        # type: (dict) -> tuple
        count = 0
        dest = None  # type: str | None
        for resource_metric in body.get("resourceMetrics", []):
            resource_attrs = attrs_to_dict(resource_metric.get("resource", {}).get("attributes"))
            _, project, subproject = pick_project_subproject(resource_attrs)

            for scope_metric in resource_metric.get("scopeMetrics", []):
                for metric in scope_metric.get("metrics", []):
                    name = metric.get("name", "metric")

                    data_points = []
                    for metric_type in ("sum", "gauge", "histogram", "summary"):
                        if metric_type in metric:
                            data_points = metric[metric_type].get("dataPoints", [])
                            break

                    event = {
                        "signal": "metric",
                        "metric_name": name,
                        "timestamp": datetime.now(timezone.utc).isoformat(),
                        "project": project,
                        "subproject": subproject,
                        "data_points": [
                            {
                                "attributes": attrs_to_dict(dp.get("attributes")),
                                **{k: v for k, v in dp.items() if k != "attributes"},
                            }
                            for dp in data_points
                        ],
                        "resource_attributes": resource_attrs,
                    }
                    self.write_event(project, subproject, "metric", name, event)
                    dest = "{}/{}".format(safe_filename(project), safe_filename(subproject))
                    count += 1
        return count, dest


class OtlpHandler(BaseHTTPRequestHandler):
    receiver = None  # type: Receiver | None
    raw_log = None  # type: Path | None
    server_version = "otel-receiver/2.0"
    sys_version = ""

    def do_POST(self):
        length = int(self.headers.get("Content-Length", "0"))
        raw = self.rfile.read(length)
        encoding = self.headers.get("Content-Encoding")
        decoded = decode_body(raw, encoding)

        try:
            body = json.loads(decoded.decode("utf-8"))
        except (json.JSONDecodeError, UnicodeDecodeError):
            self.send_response(HTTPStatus.BAD_REQUEST)
            self.end_headers()
            return

        # Dump decoded request if logging enabled
        if self.raw_log:
            entry = {
                "path": self.path,
                "headers": dict(self.headers),
                "body": body,
            }
            with open(str(self.raw_log), "a", encoding="utf-8") as f:
                f.write(json.dumps(entry, indent=2) + "\n---\n")

        path = self.path.lower()
        if "logs" in path:
            count, dest = self.receiver.process_logs(body)
        elif "traces" in path:
            count, dest = self.receiver.process_traces(body)
        elif "metrics" in path:
            count, dest = self.receiver.process_metrics(body)
        else:
            count, dest = 0, None

        response = json.dumps({"partialSuccess": {}}).encode("utf-8")
        self.send_response(HTTPStatus.OK)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(response)))
        self.end_headers()
        self.wfile.write(response)

        if count:
            print("  [{}] wrote {} event(s) -> {}".format(path, count, dest))

    def do_GET(self):
        body = json.dumps({"ok": True}).encode("utf-8")
        self.send_response(HTTPStatus.OK)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, fmt, *args):
        pass  # quiet


def run_server(host="127.0.0.1", port=4318, out_dir="telemetry-output", raw_log=None):
    # type: (str, int, str, str | None) -> None
    """Run the OTLP receiver (blocking)."""
    receiver = Receiver(Path(out_dir))
    OtlpHandler.receiver = receiver
    OtlpHandler.raw_log = Path(raw_log) if raw_log else None

    if raw_log:
        print("Raw request log: {}".format(raw_log))

    with ThreadingHTTPServer((host, port), OtlpHandler) as server:
        print("OTLP receiver on http://{}:{}".format(host, port))
        print("Storing events under {}/{{project}}/{{subproject}}/".format(out_dir))
        server.serve_forever()


def main_standalone(host="127.0.0.1", port=4318, out_dir="telemetry-output", raw_log=None):
    # type: (str, int, str, str | None) -> None
    """Entry point for standalone receiver mode."""
    run_server(host=host, port=port, out_dir=out_dir, raw_log=raw_log)


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Lightweight OTLP receiver.")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=4318)
    parser.add_argument("--out-dir", default="telemetry-output")
    parser.add_argument("--raw-log", default=None, help="Path to dump raw OTLP requests")
    args = parser.parse_args()
    run_server(host=args.host, port=args.port, out_dir=args.out_dir, raw_log=args.raw_log)
