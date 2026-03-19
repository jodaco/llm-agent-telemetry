"""
CLI entry point for agent_telemetry.

Usage:
    python -m agent_telemetry receiver [--port PORT] [--output DIR]
    python -m agent_telemetry setup [--project NAME] [--subproject NAME] [--port PORT] [--dir PATH]
    python -m agent_telemetry teardown [--dir PATH]
    python -m agent_telemetry save AGENT FILE [--project NAME] [--subproject NAME] [--output DIR]
"""
from __future__ import annotations

import argparse
import os
import sys

from . import AgentTelemetry


def cmd_receiver(args):
    from . import receiver as recv_mod
    recv_mod.main_standalone(host=args.host, port=args.port, out_dir=args.output)


def cmd_setup(args):
    at = AgentTelemetry()
    # Configure state without writing yet
    at.project = args.project or os.environ.get("OTEL_PROJECT") or os.path.basename(os.getcwd())
    at.subproject = args.subproject or ""
    at.port = args.port
    at.endpoint = "http://127.0.0.1:{}".format(args.port)

    agent = args.agent
    path = args.dir
    if agent == "claude":
        at._write_claude_config(path)
        print("Wrote .claude/ config in {}".format(path or "."))
    elif agent == "codex":
        at._write_codex_config(path)
        print("Wrote .codex/ config in {}".format(path or "."))
    else:
        at.setup_telemetry(
            project=args.project,
            subproject=args.subproject,
            port=args.port,
            path=path,
        )
        print("Wrote .claude/ and .codex/ config in {}".format(path or "."))


def cmd_teardown(args):
    at = AgentTelemetry()
    agent = args.agent
    if agent == "claude":
        at._teardown_claude(args.dir)
        print("Removed OTEL settings from .claude/")
    elif agent == "codex":
        at._teardown_codex(args.dir)
        print("Removed OTEL settings from .codex/")
    else:
        at.teardown(args.dir)
        print("Removed OTEL settings from .claude/ and .codex/")


def cmd_save(args):
    at = AgentTelemetry()
    at.project = args.project or os.environ.get("OTEL_PROJECT") or os.path.basename(os.getcwd())
    at.subproject = args.subproject or os.environ.get("OTEL_SUBPROJECT") or ""
    at.output_dir = args.output
    dest = at.save_artifacts(args.agent, args.file)
    print("Saved to {}".format(dest))


def main():
    parser = argparse.ArgumentParser(
        prog="agent_telemetry",
        description="Configure and capture OTEL telemetry from Claude Code and Codex CLI.",
    )
    sub = parser.add_subparsers(dest="command")

    # receiver
    p_recv = sub.add_parser("receiver", help="Start standalone OTLP receiver")
    p_recv.add_argument("--host", default="127.0.0.1")
    p_recv.add_argument("--port", type=int, default=4318)
    p_recv.add_argument("--output", default="telemetry-output")

    # setup
    p_setup = sub.add_parser("setup", help="Write agent config files")
    p_setup.add_argument("agent", nargs="?", choices=["claude", "codex"], default=None,
                         help="Write config for a specific agent (default: both)")
    p_setup.add_argument("--project", default=None)
    p_setup.add_argument("--subproject", default=None)
    p_setup.add_argument("--port", type=int, default=4318)
    p_setup.add_argument("--dir", default=None)

    # teardown
    p_tear = sub.add_parser("teardown", help="Remove OTEL config files")
    p_tear.add_argument("agent", nargs="?", choices=["claude", "codex"], default=None,
                         help="Remove config for a specific agent (default: both)")
    p_tear.add_argument("--dir", default=None)

    # save
    p_save = sub.add_parser("save", help="Save artifacts to output directory")
    p_save.add_argument("agent", help="Agent name (e.g. claude, codex)")
    p_save.add_argument("file", help="File or directory to save")
    p_save.add_argument("--project", default=None)
    p_save.add_argument("--subproject", default=None)
    p_save.add_argument("--output", default="telemetry-output")

    args = parser.parse_args()

    if args.command == "receiver":
        cmd_receiver(args)
    elif args.command == "setup":
        cmd_setup(args)
    elif args.command == "teardown":
        cmd_teardown(args)
    elif args.command == "save":
        cmd_save(args)
    else:
        parser.print_help()
        sys.exit(1)


if __name__ == "__main__":
    main()
