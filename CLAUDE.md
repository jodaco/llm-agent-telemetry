# agent-telemetry

A Python module for capturing and viewing OpenTelemetry data from Claude Code and Codex CLI sessions. Stdlib only — no pip install, no venv. Targets Python 3.7+.

## Rules

- Stdlib only. No pip dependencies.
- Python 3.7+ compatibility (`from __future__ import annotations`, type comments).
- Never write to `~/.claude/` or `~/.codex/` — project-scoped config only.
- Receiver filenames must start with `otel-` — this is how telemetry is distinguished from artifacts.
- Tests: `python3 -m unittest discover -s tests -p 'test_*.py' -v`. All 23 must pass before merging changes.
- If `claude` or `codex` is not in PATH, integration tests print a warning and skip — they do not fail.

---

## Part 1: Collector

Writes project-scoped config files and runs a local OTLP receiver to capture telemetry as flat JSON.

### Configuration

All via `setup_telemetry()` method arguments (the constructor takes no arguments):

| Argument | Default | Description |
|---|---|---|
| `project` | `os.path.basename(os.getcwd())` | Project name |
| `subproject` | `None` | Sub-project grouping (e.g. `task-1`) |
| `enabled` | `True` | `False` = don't start local receiver, use external collector |
| `port` | `4318` | Port for local OTLP receiver |
| `output_dir` | `telemetry-output` | Where captured JSON goes |
| `endpoint` | `http://127.0.0.1:{port}` | OTLP endpoint URL |
| `path` | `os.getcwd()` | Directory to write config files into |

### Usage — Library

```python
from agent_telemetry import AgentTelemetry

at = AgentTelemetry()

# Configure and write config files to cwd (or specify a path)
at.setup_telemetry(project="my-project", subproject="task-1")

# Start the local receiver
at.start_telemetry()

# Switch subproject mid-session — rewrites config files on disk
at.set_project("my-project", "task-2")

# Save artifacts alongside telemetry (agent name + source path)
# Globs output dir for a directory matching the agent name and project
at.save_artifacts("claude", "results.txt")
at.save_artifacts("codex", "build-logs/")

# Stop receiver and clean up config files
at.stop_telemetry()
at.teardown()
```

### Usage — CLI

```bash
# Start the receiver — just an OTLP listener, no config files written
python3 -m agent_telemetry receiver --port 4318 --output telemetry-output

# Write config files to cwd — does NOT start the receiver
python3 -m agent_telemetry setup --project my-project --subproject task-1
python3 -m agent_telemetry setup claude --project my-project --subproject task-1
python3 -m agent_telemetry setup codex --project my-project --subproject task-1

# Write config to a specific directory
python3 -m agent_telemetry setup --project my-project --dir /path/to/project

# Clean up config files (remove OTEL settings from project configs)
python3 -m agent_telemetry teardown
python3 -m agent_telemetry teardown claude
python3 -m agent_telemetry teardown codex

# Save artifacts alongside telemetry (agent name required)
python3 -m agent_telemetry save claude results.txt --project my-project --subproject task-1
python3 -m agent_telemetry save codex build-logs/ --project my-project --subproject task-1 --output telemetry-output
```

The `receiver` and `setup` subcommands are completely independent.

### What gets written

**`.claude/settings.local.json`** (merged into existing if present):
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

Claude reads `OTEL_RESOURCE_ATTRIBUTES` and passes `project`/`subproject` as separate resource attributes.

**`.codex/config.toml`** (merged into existing if present):
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

Codex only has `otel.environment` (a single string). We encode `project=X,subproject=Y` into it. The receiver parses this format from the `env` resource attribute.

Config files are **project-scoped** — written to `.claude/` and `.codex/` in the target directory. Never touches `~/.claude/` or `~/.codex/`.

### How the receiver routes events

The receiver extracts project/subproject from OTLP resource attributes in this priority:

1. Explicit `project`/`subproject` keys (set by Claude via `OTEL_RESOURCE_ATTRIBUTES`)
2. Parse `env` for `project=X,subproject=Y` format (set by Codex via `otel.environment`)
3. Fall back to `service.name` for project, `_default` for subproject

The project folder is then prefixed with `service.name` (e.g. `claude-code`, `codex_exec`) so data from different agents stays separated even when they share the same project/subproject names.

### Standalone vs embedded

- `python3 -m agent_telemetry receiver` — standalone OTLP listener. Just accepts payloads and writes JSON. No config files, no agent setup.
- `from agent_telemetry import AgentTelemetry` — embedded in a project. Writes config files so Claude/Codex pick up telemetry settings automatically.

### Saving artifacts

Copy arbitrary files into the output directory alongside telemetry data. Works like `cp` — files get copied, directories get copied recursively.

`save_artifacts(agent, src)` takes an agent name (e.g. `"claude"`, `"codex"`) and a source path. It globs the output directory for a directory matching `*{agent}*-{project}/` to find where the receiver stored telemetry for that agent. Artifacts are placed in the same directory so they appear together in the viewer.

If no matching directory exists yet (receiver hasn't received data), it falls back to `{agent}-{project}/{subproject}/`.

Artifacts are saved into an `artifacts/` subdirectory within the agent's output folder, keeping them cleanly separated from `otel-*.json` telemetry files.

### File layout

```
{output_dir}/
  {service.name}-{project}/
    {subproject}/
      otel-{timestamp}-{seq}-{signal}-{event_name}.json  ← telemetry (otel- prefix)
      artifacts/                                         ← saved artifacts
        results.txt
        build-logs/                                      ← artifact directory (recursive)
```

Example: `telemetry-output/claude-code-my-project/task-1/`

### Receiver raw logging

For debugging, the receiver supports `--raw-log` to dump every decoded OTLP request (only available when running `receiver.py` directly, not via the CLI wrapper):

```bash
python3 agent_telemetry/receiver.py --raw-log /tmp/raw-requests.log
```

### Tests

Tests are split by concern. Run all:

```bash
python3 -m unittest discover -s tests -p 'test_*.py' -v
```

Run individually:

```bash
python3 -m unittest tests.test_setup -v
python3 -m unittest tests.test_receiver -v
python3 -m unittest tests.test_artifacts -v
python3 -m unittest tests.test_claude -v
python3 -m unittest tests.test_codex -v
```

Each test creates its own isolated output directory under `$TMPDIR` and cleans it up in `tearDown`. Tests never write to the project's `.claude/` or `.codex/` directories — all config is written to temp dirs.

| File | Tests | What it covers |
|---|---|---|
| `test_setup.py` | 10 | Config creation, merging, teardown, set_project |
| `test_receiver.py` | 5 | Health check, logs, traces, metrics, otel- filename prefix |
| `test_artifacts.py` | 6 | File copy, dir copy, agent-scoped save, glob matching, fallback |
| `test_claude.py` | 1 | Real Claude run, verify JSON output in correct directory |
| `test_codex.py` | 1 | Real Codex run, verify JSON output in correct directory |

---

## Part 2: Viewer (`telemetry_viewer`)

Standalone web viewer for telemetry captured by the collector. Separate module — reads the collector's output directory but has no dependency on it.

### Usage — CLI

```bash
python3 -m telemetry_viewer --port 8080 --data-dir telemetry-output
```

Default port: 8080. Serves a web UI at `http://127.0.0.1:8080`.

### Routes

| Route | Description |
|---|---|
| `/` | Project listing (auto-redirects if only one project) |
| `/p/{project}` | Subproject listing for a project |
| `/p/{project}/{sub}` | Subproject view — "View Conversation" link + inline artifact listing, or redirect to log if no artifacts |
| `/p/{project}/{sub}/log` | Conversation log view |
| `/p/{project}/{sub}/artifacts[/path]` | Artifact directory listing (supports nested browsing) |
| `/raw/{project}/{sub}/{path}` | Raw file download (supports nested paths, path traversal protected) |

### Navigation

**Project listing** → **Subproject listing** → **Conversation log / Artifacts**

1. **Project listing** — lists all project directories found in `--data-dir`. Shows subproject count per project. If only one project exists, auto-redirects to its subproject listing.

2. **Subproject listing** — shows subprojects with file count and type indicators (telemetry, artifacts, or both).

3. **Subproject view** — automatic routing based on contents:
   - **No artifacts** → redirects straight to `/log`
   - **Has artifacts** → shows "View Conversation" link (if telemetry exists) plus inline artifact listing with clickable files and browsable directories

Breadcrumb navigation on all pages (Projects / project / subproject / view).

### Identifying telemetry vs artifacts

Filename starts with `otel-` and ends with `.json` → telemetry. Everything else is an artifact.

### Conversation log view

Renders OTEL telemetry events as an interactive conversation timeline. Self-contained HTML — no external dependencies.

**Summary bar** at the top showing:
- Model name(s) — extracted from Claude `api_request` events and Codex SSE `response.completed` events
- Total cost (USD) — from Claude `cost_usd` attributes
- Token counts (input / output / cached) — from Claude `api_request` attributes or Codex/Claude token usage metrics
- User prompt count, API call count, tool call count
- Total event count

**Interactive tool timeline** (fixed panel at top of page):
- Color-coded dots for each tool event, positioned by timestamp on a horizontal scrollable track
- Golden-angle hue distribution for distinct tool colors
- Time axis with auto-scaled tick intervals (seconds to hours)
- Duration label (start → end time)
- Legend with per-tool filter checkboxes (select all / clear all)
- Click a dot to scroll to and highlight the corresponding event card
- Collapsible — "Hide" button collapses the panel, "Show tool timeline" button restores it
- Horizontal scroll via mouse wheel

**Event cards** — one per telemetry event, color-coded by type:
- **User prompt** (blue border) — displays the prompt text
- **API request** (purple border) — model, token counts, cost, duration
- **Token usage** (teal border) — key-value table of token counts per type (input, output, cached, reasoning). Detected from Claude `claude_code.token.usage` metrics, Codex `codex.turn.token_usage` metrics, and Codex SSE `response.completed` events. Always visible (not hidden by meta toggle).
- **Tool decision** (orange border) — tool name, decision, source
- **Tool result** (orange border) — tool name, success/failure chip, duration, expandable parameters and output
- **WebSocket/SSE events** (gray border) — model, duration (hidden by default as meta)
- **Metrics** (green border) — metric name with context (model, tool), data points labeled by varying attributes only (hidden by default as meta)
- **Traces** (gray border) — span name, trace ID (hidden by default as meta)
- **System log** (gray border) — internal runtime messages (hidden by default as meta)
- **Errors** (red border)

**Event card features:**
- Status chips (green "success" / red "failed") on tool results
- "On timeline" button — scrolls the timeline to center on this event's marker
- "Next this tool" button — jumps to the next event using the same tool
- Expandable "Parameters" section for tool calls (auto-formats JSON)
- Expandable "Output" section for tool results (with byte count, preview for large output)
- Expandable "Full JSON" section showing the raw event data
- Sequence number display from `event.sequence` attribute
- Filename display from the source `otel-*.json` file

**Filter controls:**
- Per-event-type checkboxes to show/hide event categories
- "Show meta blocks" toggle — meta blocks (websocket, metrics, system logs, traces) are **hidden by default**. Click to reveal them. Token usage and tool events are always visible.

**Sorting:** events sorted by timestamp, then by `event.sequence` attribute.

### Artifacts view

Artifacts are shown inline on the subproject page (not a separate page). If artifacts are in an `artifacts/` subdirectory (the convention used by `save_artifacts()`), its contents are listed directly. Legacy root-level artifacts are also supported.

- Table with Name and Size columns
- Files are clickable — served via `/raw/` endpoint with correct MIME types (`mimetypes.guess_type`)
- Directories are clickable — navigates to `/p/{project}/{sub}/artifacts/{path}` for recursive browsing
- Breadcrumb navigation through nested artifact directories
- Hidden files (dot-prefixed) excluded

### Raw file serving

The `/raw/{project}/{sub}/{filename}` endpoint serves artifact files directly:
- Path traversal protection (rejects `..` and absolute paths)
- MIME type detection via `mimetypes.guess_type`
- Chunked streaming (64KB chunks) for large files
- Falls back to `application/octet-stream` for unknown types

### Implementation details

- `server.py` — stdlib `http.server.HTTPServer` with `BaseHTTPRequestHandler`. All HTML/CSS/JS is generated server-side as self-contained pages (no external CDN, no static files). Light theme with system-ui fonts.
- `__main__.py` — argparse CLI, serves the viewer.

---

## Module Structure

```
agent_telemetry/
  __init__.py      # AgentTelemetry class (setup_telemetry, start/stop, set_project, teardown, save_artifacts)
  __main__.py      # CLI entry point (receiver, setup, teardown, save)
  receiver.py      # OTLP/HTTP receiver with --raw-log support
telemetry_viewer/
  __init__.py      # Version string
  __main__.py      # CLI entry point (serve)
  server.py        # HTTP server, routing, HTML/CSS/JS rendering
tests/
  helpers.py         # Shared test utilities (find_free_port, which, wait_for_port, send_otel_payload)
  test_setup.py      # 10 tests: config creation, merging, teardown, set_project
  test_receiver.py   # 5 tests: health check, logs, traces, metrics, filename prefix
  test_artifacts.py  # 6 tests: file copy, dir copy, agent-scoped save, glob matching, fallback
  test_claude.py     # 1 test: Claude integration
  test_codex.py      # 1 test: Codex integration
```

Stdlib only. No dependencies. Targets Python 3.7+.

## Plan

### Completed: Collector

- [x] `agent_telemetry/__init__.py` — `AgentTelemetry` class
- [x] `agent_telemetry/__main__.py` — CLI
- [x] `agent_telemetry/receiver.py` — OTLP/HTTP receiver with `otel-` prefix, `service.name`-prefixed project dirs, `env` parsing for Codex
- [x] Config file merge logic (JSON for Claude, line-based TOML for Codex)
- [x] Tests split into 5 files (23 tests total)
- [x] `.gitignore` updated

### Completed: Viewer (`telemetry_viewer` module)

- [x] Create `telemetry_viewer/__init__.py` and `telemetry_viewer/__main__.py`
- [x] Create `telemetry_viewer/server.py` — HTTP server with routing
- [x] OTEL detection: filename starts with `otel-` and ends with `.json` → telemetry, everything else → artifact
- [x] Conversation log rendering: chronological event cards, color-coded, expandable JSON
- [x] Token/cost summary bar at top of conversation view
- [x] Interactive tool timeline with color-coded markers, legend filtering, and bidirectional navigation
- [x] Event type filter checkboxes and meta block toggle
- [x] Artifacts view with file listing and raw file download
- [x] Light theme, self-contained HTML (no external deps)
- [x] Token Usage cards for both Claude and Codex metrics
- [x] Meta blocks hidden by default, smart metric labeling

### TODO: Clean up

- [x] Generate `README.md` with usage instructions
- [ ] Test: import from external project, CLI from external project
