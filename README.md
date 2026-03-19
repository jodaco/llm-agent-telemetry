# agent-telemetry
Capture and view OpenTelemetry data from Claude Code and Codex CLI sessions. The ideal use case is running Claude or Codex inside a Docker container and collecting telemetry for later study. Stdlib only — no pip install, no venv. Targets Python 3.7+.

Telemetry is organized into project/subproject directories, prefixed by the agent's service name:

```
telemetry-output/
  {agent}-{project}/
    {subproject}/
```

For example, a project with multiple tasks analyzed by different agents:

```
telemetry-output/
  claude-code-my-project/
    task-1/
    task-2/
  codex_exec-my-project/
    task-1/
    task-2/
```

## Quick Start

There are two ways to use agent-telemetry: **standalone** (CLI commands) or **as a library** (embedded in a Python script).

---

## Standalone Usage

Two independent steps: configure the agent, then run the receiver.

### 1. Configure the agent

There are three ways to point Claude Code or Codex at the receiver. Pick whichever fits your workflow.

#### Option A: Use the setup command

The quickest path. Writes project-scoped config files for Claude and/or Codex in the current directory:

```bash
# Both agents
python3 -m agent_telemetry setup --project my-project --subproject task-1

# Claude only
python3 -m agent_telemetry setup claude --project my-project --subproject task-1

# Codex only
python3 -m agent_telemetry setup codex --project my-project --subproject task-1

# Write config to a different directory
python3 -m agent_telemetry setup --project my-project --subproject task-1 --dir /path/to/project
```

This writes `.claude/settings.local.json` and/or `.codex/config.toml` in the target directory. These are project-scoped overrides — they only affect agent instances launched from that directory, so any unrelated Claude or Codex sessions you have open elsewhere are not affected.

> **Codex VS Code GUI limitation:** The VS Code extension for Codex only reads `~/.codex/config.toml` (the global config), not project-scoped `.codex/config.toml`. If you use the Codex VS Code GUI, you'll need to write your config to `~/.codex/config.toml` instead, which means _all_ Codex sessions will send telemetry to this receiver until you remove it. This is a non-issue for `codex exec` (CLI), which reads project-scoped config normally. For this reason, Option A is ideal for CLI usage and Docker containers.

> **Note on mixed results:** If you already have an agent session open in the same directory (e.g. you're chatting with Claude in VS Code), its telemetry will also flow to the receiver under the same project/subproject. Keep this in mind if you need clean separation between sessions.

#### Option B: Write config files manually

If you need to tweak settings beyond what the setup command provides, write the config files yourself.

**Codex** — `.codex/config.toml` (project directory, or `~/.codex/config.toml` for VS Code GUI):

```toml
[otel]
environment = "project=my-project,subproject=task-1"
log_user_prompt = true

[otel.exporter.otlp-http]
endpoint = "http://127.0.0.1:4318/v1/logs"
protocol = "json"

[otel.metrics_exporter.otlp-http]
endpoint = "http://127.0.0.1:4318/v1/metrics"
protocol = "json"
```

**Claude** — `.claude/settings.local.json` (project directory):

```json
{
  "env": {
    "CLAUDE_CODE_ENABLE_TELEMETRY": "1",
    "OTEL_METRICS_EXPORTER": "otlp",
    "OTEL_LOGS_EXPORTER": "otlp",
    "OTEL_EXPORTER_OTLP_PROTOCOL": "http/json",
    "OTEL_EXPORTER_OTLP_ENDPOINT": "http://127.0.0.1:4318",
    "OTEL_METRIC_EXPORT_INTERVAL": "10000",
    "OTEL_LOGS_EXPORT_INTERVAL": "5000",
    "OTEL_RESOURCE_ATTRIBUTES": "project=my-project,subproject=task-1",
    "OTEL_LOG_USER_PROMPTS": "1",
    "OTEL_LOG_TOOL_DETAILS": "1"
  }
}
```

#### Option C: Pass config at launch time (no files)

Skip config files entirely and pass settings inline when you invoke the agent.

**Claude** — environment variables (officially documented):

```bash
CLAUDE_CODE_ENABLE_TELEMETRY=1 \
OTEL_METRICS_EXPORTER=otlp \
OTEL_LOGS_EXPORTER=otlp \
OTEL_EXPORTER_OTLP_PROTOCOL=http/json \
OTEL_EXPORTER_OTLP_ENDPOINT=http://127.0.0.1:4318 \
OTEL_METRIC_EXPORT_INTERVAL=10000 \
OTEL_LOGS_EXPORT_INTERVAL=5000 \
OTEL_RESOURCE_ATTRIBUTES="project=my-project,subproject=task-1" \
OTEL_LOG_USER_PROMPTS=1 \
OTEL_LOG_TOOL_DETAILS=1 \
claude -p "the prompt"
```

**Codex** — `-c` flag (overrides any `config.toml` value at launch):

```bash
codex exec \
  -c 'otel.environment="project=my-project,subproject=task-1"' \
  -c 'otel.log_user_prompt=true' \
  -c 'otel.exporter.otlp-http.endpoint="http://127.0.0.1:4318/v1/logs"' \
  -c 'otel.exporter.otlp-http.protocol="json"' \
  -c 'otel.metrics_exporter.otlp-http.endpoint="http://127.0.0.1:4318/v1/metrics"' \
  -c 'otel.metrics_exporter.otlp-http.protocol="json"' \
  "the prompt"
```

> **Note:** Codex env vars (e.g. `OTEL_EXPORTER_OTLP_ENDPOINT`) may work in some versions but are not officially documented — the `-c` flag is the reliable approach.

### 2. Start the receiver

```bash
python3 -m agent_telemetry receiver --port 4318 --output telemetry-output
```

This starts a local OTLP/HTTP listener that captures telemetry as flat JSON files into `telemetry-output/`.

### 3. Run your agent

Now launch Claude Code or Codex CLI from the project directory. Telemetry flows automatically.

### Changing project or subproject

The `--project` and `--subproject` names are yours to choose. A natural pattern is to use the project name for the overall engagement and the subproject for individual targets:

- `--project my-project --subproject task-1`
- `--project my-project --subproject task-2`
- `--project another-project --subproject experiment-a`
- `--project another-project --subproject experiment-b`

**Important: agents must be restarted after changing config.**

- **Claude Code (VS Code pane):** Close the Claude pane and re-open it. It picks up new settings on launch.
- **Claude CLI / Codex CLI:** Exit and restart the CLI process.
- **Codex (VS Code GUI):** The VS Code GUI only reads the global `~/.codex/config.toml`, not project-scoped config. This means using Codex GUI for telemetry temporarily requires writing to your global config. CLI usage (`codex exec`) reads project-scoped `.codex/config.toml` and does not have this limitation.

### Clean up

Remove OTEL settings from agent config files:

```bash
python3 -m agent_telemetry teardown
python3 -m agent_telemetry teardown claude
python3 -m agent_telemetry teardown codex
```

---

## Library Usage

When embedded in a Python script, changing project/subproject between agent invocations is seamless — no manual restarts needed.

```python
from agent_telemetry import AgentTelemetry

at = AgentTelemetry()

# Configure and write config files
at.setup_telemetry(project="my-project", subproject="task-1")

# Start the local OTLP receiver
at.start_telemetry()

# Run Claude against the first target
#   claude -p "analyze task-1"
# ... agent runs, telemetry is captured ...

# Switch to the next target — rewrites config files on disk
at.set_project("my-project", "task-2")

# Run Claude against the second target
#   claude -p "analyze task-2"
# ... agent runs, telemetry goes to the new subproject directory ...

# Save artifacts alongside telemetry (agent name + source path)
at.save_artifacts("claude", "results.txt")
at.save_artifacts("codex", "build-logs/")

# Stop receiver and clean up config files
at.stop_telemetry()
at.teardown()
```

When agents are invoked programmatically (`claude -p "the prompt"` or `codex exec "the prompt"`), each invocation picks up the current config at launch. This means `set_project()` between invocations works without any manual restart — the next `claude -p` or `codex exec` call reads the updated config automatically.

This makes the library approach natural for scripted workflows where each run should be tagged separately.

---

## Viewing Telemetry

Once you have captured telemetry, view it in a web browser:

```bash
python3 -m telemetry_viewer --port 8080 --data-dir telemetry-output
```

Open `http://127.0.0.1:8080` to browse projects, subprojects, conversation logs, and artifacts.

The viewer navigates: **Project listing** → **Subproject listing** → **Subproject view**. If only one project exists, it skips straight to the subproject listing. If a subproject has no artifacts, it goes straight to the conversation log. If artifacts exist, the subproject page shows a "View Conversation" link plus an inline artifact listing with browsable directories.

The conversation log view includes:
- Summary bar with model, cost, token counts, and event counts
- Interactive tool timeline with color-coded markers and per-tool filtering
- Event cards color-coded by type (user prompt, API request, tool decision, tool result, etc.)
- Expandable parameters, output, and full JSON per event
- "On timeline" and "Next this tool" navigation buttons on tool events

---

## CLI Reference

```
python3 -m agent_telemetry receiver [--host HOST] [--port PORT] [--output DIR]
python3 -m agent_telemetry setup [claude|codex] [--project NAME] [--subproject NAME] [--port PORT] [--dir PATH]
python3 -m agent_telemetry teardown [claude|codex] [--dir PATH]
python3 -m agent_telemetry save AGENT FILE [--project NAME] [--subproject NAME] [--output DIR]
python3 -m telemetry_viewer [--port PORT] [--data-dir DIR]
```

| Command | Description |
|---|---|
| `agent_telemetry receiver` | Start standalone OTLP/HTTP listener. Writes JSON to `--output` dir. |
| `agent_telemetry setup` | Write agent config files. Does NOT start the receiver. |
| `agent_telemetry teardown` | Remove OTEL settings from agent config files. |
| `agent_telemetry save` | Copy files/directories into the agent's output directory alongside telemetry. Requires agent name (e.g. `claude`, `codex`). Globs the output dir to find the matching service-prefixed folder. |
| `telemetry_viewer` | Start web viewer for captured telemetry. |

## Output Structure

```
telemetry-output/
  claude-code-my-project/
    task-1/
      otel-1710000000-001-log-user_message.json
      otel-1710000000-002-log-tool_use.json
      artifacts/                             # saved artifacts
        results.txt
        build-logs/
    task-2/
      otel-1710000100-001-log-user_message.json
  codex_exec-my-project/
    task-1/
      otel-1710000000-001-log-exec_start.json
```

Files prefixed with `otel-` are telemetry. Artifacts are saved into the `artifacts/` subdirectory by `save_artifacts()`.

## Tests

```bash
python3 -m unittest discover -s tests -p 'test_*.py' -v
```

Integration tests (`test_claude.py`, `test_codex.py`) require `claude` / `codex` in PATH. If not found, they print a warning and skip — they do not fail.

## Acknowledgements

Inspired by [viewclaudecode](https://github.com/TomAPU/viewclaudecode), [viewcodexlog](https://github.com/TomAPU/viewcodexlog), and [parsecodexlog](https://github.com/TomAPU/parsecodexlog). Those tools parse agent-specific log formats directly. This project takes a different approach: by using OpenTelemetry, we get uniform project/subproject labels across agents instead of each agent grouping everything by date, making it easy to tell which telemetry came from where.

## Requirements

- Python 3.7+
- No external dependencies (stdlib only)
