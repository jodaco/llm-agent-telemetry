"""
agent_telemetry - Configure and capture OpenTelemetry data from Claude Code and Codex CLI.

Stdlib only. No dependencies. Targets Python 3.7+.
"""
from __future__ import annotations

import json
import os
import shutil
from pathlib import Path
from typing import Any, Dict, List, Optional

# Keys we own in .claude/settings.local.json -> env
CLAUDE_ENV_KEYS = [
    "CLAUDE_CODE_ENABLE_TELEMETRY",
    "OTEL_METRICS_EXPORTER",
    "OTEL_LOGS_EXPORTER",
    "OTEL_EXPORTER_OTLP_PROTOCOL",
    "OTEL_EXPORTER_OTLP_ENDPOINT",
    "OTEL_METRIC_EXPORT_INTERVAL",
    "OTEL_LOGS_EXPORT_INTERVAL",
    "OTEL_RESOURCE_ATTRIBUTES",
    "OTEL_LOG_USER_PROMPTS",
    "OTEL_LOG_TOOL_DETAILS",
]


class AgentTelemetry:
    """Configure OTEL telemetry for Claude Code and Codex CLI sessions."""

    def __init__(self):
        # All state, no config in constructor — use setup_telemetry() to configure
        self.project = ""
        self.subproject = ""
        self.enabled = True
        self.port = 4318
        self.output_dir = "telemetry-output"
        self.endpoint = "http://127.0.0.1:4318"
        self._path = None  # type: Optional[str]

        # Internal state
        self._receiver_process = None  # type: Any
        self._configured_paths = {}  # type: Dict[str, Dict[str, bool]]

    def setup_telemetry(
        self,
        project=None,        # type: Optional[str]
        subproject=None,     # type: Optional[str]
        enabled=True,        # type: bool
        port=4318,           # type: int
        output_dir="telemetry-output",  # type: str
        endpoint=None,       # type: Optional[str]
        path=None,           # type: Optional[str]
    ):
        # type: (...) -> None
        """Configure telemetry settings and write config files for Claude and Codex.

        Args:
            project: Project name. Defaults to env OTEL_PROJECT or cwd basename.
            subproject: Sub-project name. Defaults to env OTEL_SUBPROJECT or empty.
            enabled: If True, local receiver will be used. If False, external collector mode.
            port: Port for local OTLP receiver.
            output_dir: Where captured JSON goes.
            endpoint: OTLP endpoint URL. Defaults to http://127.0.0.1:{port}.
            path: Directory to write config files to. Defaults to cwd.
        """
        self.project = project or os.environ.get("OTEL_PROJECT") or os.path.basename(os.getcwd())
        self.subproject = subproject or os.environ.get("OTEL_SUBPROJECT") or ""
        self.enabled = enabled
        self.port = port
        self.output_dir = output_dir
        self.endpoint = endpoint or "http://127.0.0.1:{}".format(self.port)
        self._path = path

        # Write config files
        self._write_claude_config(path)
        self._write_codex_config(path)

    def start_telemetry(self):
        # type: () -> Any
        """Start the local OTLP receiver. Returns Popen handle.

        Does nothing if enabled=False (external collector mode).
        """
        import subprocess
        import sys

        if not self.enabled:
            return None

        if self.receiver_running:
            return self._receiver_process

        receiver_path = os.path.join(os.path.dirname(__file__), "receiver.py")
        self._receiver_process = subprocess.Popen(
            [
                sys.executable,
                receiver_path,
                "--host", "127.0.0.1",
                "--port", str(self.port),
                "--out-dir", self.output_dir,
            ],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
        )
        return self._receiver_process

    def stop_telemetry(self):
        # type: () -> None
        """Stop the receiver if running."""
        if self._receiver_process is not None:
            if self._receiver_process.stdout:
                self._receiver_process.stdout.close()
            if self._receiver_process.stderr:
                self._receiver_process.stderr.close()
            self._receiver_process.terminate()
            self._receiver_process.wait(timeout=5)
            self._receiver_process = None

    def set_project(self, project, subproject=None):
        # type: (str, Optional[str]) -> None
        """Change the project and/or subproject, and update config files on disk.

        Args:
            project: New project name.
            subproject: New sub-project name. Pass "" to clear. None leaves it unchanged.
        """
        self.project = project
        if subproject is not None:
            self.subproject = subproject

        # Rewrite config files at the configured path
        path = self._path
        self._write_claude_config(path)
        self._write_codex_config(path)

    def teardown(self, path=None):
        # type: (Optional[str]) -> None
        """Remove OTEL settings from both config files."""
        self._teardown_claude(path)
        self._teardown_codex(path)

    def save_artifacts(self, agent, src, subproject=None):
        # type: (str, str, Optional[str]) -> Path
        """Copy a file or directory into the output folder next to telemetry.

        Finds the correct output directory by globbing for directories that
        match the agent name and project. The receiver creates directories
        like {service.name}-{project}/{subproject}/ — this method matches
        the agent name against existing directory prefixes.

        Args:
            agent: Agent name to match (e.g. "claude", "codex"). Matched
                   against existing directory names in the output folder
                   (e.g. "claude" matches "claude-code-my-project/").
            src: File or directory to copy. Directories are copied recursively.
            subproject: Override the current subproject. Defaults to self.subproject.

        Returns:
            Path to the copied artifact.

        Raises:
            ValueError: If no matching directory is found in the output folder.
        """
        sub = subproject if subproject is not None else self.subproject
        agent_dir = self._find_agent_dir(agent, sub)
        dest_dir = agent_dir / "artifacts"

        src_path = Path(src)
        dest_dir.mkdir(parents=True, exist_ok=True)
        dest = dest_dir / src_path.name

        if src_path.is_dir():
            if dest.exists():
                shutil.rmtree(str(dest))
            shutil.copytree(str(src_path), str(dest))
        else:
            shutil.copy2(str(src_path), str(dest))

        return dest

    def _find_agent_dir(self, agent, subproject):
        # type: (str, str) -> Path
        """Find the output directory for an agent by globbing existing dirs.

        Looks for directories ending with -{project} whose service prefix
        contains the agent name. When multiple directories match, prefers
        the most recent (latest mtime) — this handles the case where both
        codex_exec-project/ and codex_cli_rs-project/ exist from different
        Codex versions.

        If no match exists, falls back to {agent}-{project}/.
        """
        out = Path(self.output_dir)
        suffix = "-{}".format(self.project)
        matches = []  # type: list
        if out.is_dir():
            for entry in sorted(out.iterdir()):
                if not entry.is_dir():
                    continue
                name = entry.name
                if name.endswith(suffix) and agent in name:
                    matches.append(entry)

        if matches:
            # Pick the most recently modified — the one the receiver is
            # actively writing to
            result = max(matches, key=lambda p: p.stat().st_mtime)
        else:
            # No existing dir found — fall back to {agent}-{project}
            result = out / "{}-{}".format(agent, self.project)

        if subproject:
            result = result / subproject
        return result

    # -- Properties --

    @property
    def receiver_running(self):
        # type: () -> bool
        if self._receiver_process is None:
            return False
        return self._receiver_process.poll() is None

    def configured_agents(self, path=None):
        # type: (Optional[str]) -> Dict[str, bool]
        """Return which agents are configured at a given path."""
        p = str(path or self._path or os.getcwd())
        return dict(self._configured_paths.get(p, {}))

    # -- Internal: output path --

    def _output_path(self, subproject=None, service=None):
        # type: (Optional[str], Optional[str]) -> Path
        sub = subproject if subproject is not None else self.subproject
        project_dir = self.project
        if service:
            project_dir = "{}-{}".format(service, self.project)
        p = Path(self.output_dir) / project_dir
        if sub:
            p = p / sub
        return p

    def _resource_attributes(self):
        # type: () -> str
        attrs = "project={}".format(self.project)
        if self.subproject:
            attrs += ",subproject={}".format(self.subproject)
        return attrs

    # -- Internal: Claude config --

    def _claude_env_dict(self):
        # type: () -> Dict[str, str]
        return {
            "CLAUDE_CODE_ENABLE_TELEMETRY": "1",
            "OTEL_METRICS_EXPORTER": "otlp",
            "OTEL_LOGS_EXPORTER": "otlp",
            "OTEL_EXPORTER_OTLP_PROTOCOL": "http/json",
            "OTEL_EXPORTER_OTLP_ENDPOINT": self.endpoint,
            "OTEL_METRIC_EXPORT_INTERVAL": "10000",
            "OTEL_LOGS_EXPORT_INTERVAL": "5000",
            "OTEL_RESOURCE_ATTRIBUTES": self._resource_attributes(),
            "OTEL_LOG_USER_PROMPTS": "1",
            "OTEL_LOG_TOOL_DETAILS": "1",
        }

    def _write_claude_config(self, path=None):
        # type: (Optional[str]) -> Path
        base = Path(path or os.getcwd())
        claude_dir = base / ".claude"
        claude_dir.mkdir(parents=True, exist_ok=True)
        config_file = claude_dir / "settings.local.json"

        existing = {}  # type: Dict[str, Any]
        if config_file.exists():
            try:
                existing = json.loads(config_file.read_text(encoding="utf-8"))
            except (json.JSONDecodeError, OSError):
                existing = {}

        env = existing.get("env", {})
        env.update(self._claude_env_dict())
        existing["env"] = env

        config_file.write_text(json.dumps(existing, indent=2) + "\n", encoding="utf-8")

        p = str(base)
        if p not in self._configured_paths:
            self._configured_paths[p] = {}
        self._configured_paths[p]["claude"] = True

        return config_file

    # -- Internal: Codex config --

    def _codex_toml_block(self):
        # type: () -> str
        endpoint = self.endpoint
        env_val = "project={}".format(self.project)
        if self.subproject:
            env_val += ",subproject={}".format(self.subproject)
        lines = [
            "[otel]",
            'environment = "{}"'.format(env_val),
            "log_user_prompt = true",
            "",
            "[otel.exporter.otlp-http]",
            'endpoint = "{}/v1/logs"'.format(endpoint),
            'protocol = "json"',
            "",
            "[otel.metrics_exporter.otlp-http]",
            'endpoint = "{}/v1/metrics"'.format(endpoint),
            'protocol = "json"',
        ]
        return "\n".join(lines)

    def _write_codex_config(self, path=None):
        # type: (Optional[str]) -> Path
        base = Path(path or os.getcwd())
        codex_dir = base / ".codex"
        codex_dir.mkdir(parents=True, exist_ok=True)
        config_file = codex_dir / "config.toml"

        new_block = self._codex_toml_block()

        if not config_file.exists():
            config_file.write_text(new_block + "\n", encoding="utf-8")
        else:
            content = config_file.read_text(encoding="utf-8")
            if "[otel]" not in content:
                if not content.endswith("\n"):
                    content += "\n"
                content += "\n" + new_block + "\n"
                config_file.write_text(content, encoding="utf-8")
            else:
                config_file.write_text(
                    self._replace_otel_sections(content, new_block),
                    encoding="utf-8",
                )

        p = str(base)
        if p not in self._configured_paths:
            self._configured_paths[p] = {}
        self._configured_paths[p]["codex"] = True

        return config_file

    @staticmethod
    def _replace_otel_sections(content, new_block):
        # type: (str, str) -> str
        lines = content.splitlines(True)
        result = []  # type: List[str]
        in_otel = False
        otel_inserted = False

        for line in lines:
            stripped = line.strip()
            if stripped.startswith("["):
                if stripped.startswith("[otel]") or stripped.startswith("[otel."):
                    if not otel_inserted:
                        result.append(new_block + "\n")
                        otel_inserted = True
                    in_otel = True
                    continue
                else:
                    in_otel = False

            if in_otel:
                continue
            result.append(line)

        if not otel_inserted:
            result.append(new_block + "\n")

        return "".join(result)

    # -- Internal: Teardown --

    def _teardown_claude(self, path=None):
        # type: (Optional[str]) -> None
        base = Path(path or self._path or os.getcwd())
        config_file = base / ".claude" / "settings.local.json"
        if not config_file.exists():
            return

        try:
            data = json.loads(config_file.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError):
            return

        env = data.get("env", {})
        for key in CLAUDE_ENV_KEYS:
            env.pop(key, None)

        if not env:
            data.pop("env", None)

        if not data:
            config_file.unlink()
        else:
            config_file.write_text(json.dumps(data, indent=2) + "\n", encoding="utf-8")

        p = str(base)
        if p in self._configured_paths:
            self._configured_paths[p].pop("claude", None)

    def _teardown_codex(self, path=None):
        # type: (Optional[str]) -> None
        base = Path(path or self._path or os.getcwd())
        config_file = base / ".codex" / "config.toml"
        if not config_file.exists():
            return

        content = config_file.read_text(encoding="utf-8")

        has_non_otel = False
        for line in content.splitlines():
            stripped = line.strip()
            if stripped.startswith("[") and not stripped.startswith("[otel]") and not stripped.startswith("[otel."):
                has_non_otel = True
                break

        if not has_non_otel:
            config_file.unlink()
        else:
            config_file.write_text(
                self._remove_otel_sections(content),
                encoding="utf-8",
            )

        p = str(base)
        if p in self._configured_paths:
            self._configured_paths[p].pop("codex", None)

    @staticmethod
    def _remove_otel_sections(content):
        # type: (str) -> str
        lines = content.splitlines(True)
        result = []  # type: List[str]
        in_otel = False

        for line in lines:
            stripped = line.strip()
            if stripped.startswith("["):
                if stripped.startswith("[otel]") or stripped.startswith("[otel."):
                    in_otel = True
                    continue
                else:
                    in_otel = False

            if in_otel:
                continue
            result.append(line)

        text = "".join(result)
        while text.endswith("\n\n"):
            text = text[:-1]
        return text
