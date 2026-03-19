"""
CLI entry point for telemetry_viewer.

Usage:
    python -m telemetry_viewer [--port PORT] [--data-dir DIR]
"""
from __future__ import annotations

import argparse


def main():
    # type: () -> None
    parser = argparse.ArgumentParser(
        prog="telemetry_viewer",
        description="Web viewer for agent-telemetry OTEL output.",
    )
    parser.add_argument("--port", type=int, default=8080)
    parser.add_argument("--data-dir", default="telemetry-output")

    args = parser.parse_args()

    from .server import serve
    serve(args.data_dir, args.port)


if __name__ == "__main__":
    main()
