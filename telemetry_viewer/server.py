"""
HTTP server for the telemetry viewer.

Serves a self-contained web UI that renders OTEL telemetry captured by
agent_telemetry.  Stdlib only, no external dependencies.

Routes:
    /                       Project listing (or redirect if only one)
    /p/{project}            Subproject listing
    /p/{project}/{sub}      Subproject view (log, artifacts, or both)
    /p/{project}/{sub}/log  Conversation log
    /p/{project}/{sub}/artifacts  Artifact listing
    /raw/{path}             Raw file download
"""
from __future__ import annotations

import html
import json
import mimetypes
import os
import re
import urllib.parse
from datetime import datetime, timezone
from http.server import HTTPServer, BaseHTTPRequestHandler

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _is_telemetry(name):
    # type: (str) -> bool
    return name.startswith("otel-") and name.endswith(".json")


def _scan_projects(data_dir):
    # type: (str) -> list[str]
    if not os.path.isdir(data_dir):
        return []
    return sorted(
        d for d in os.listdir(data_dir)
        if os.path.isdir(os.path.join(data_dir, d)) and not d.startswith(".")
    )


def _scan_subprojects(project_dir):
    # type: (str) -> list[str]
    if not os.path.isdir(project_dir):
        return []
    return sorted(
        d for d in os.listdir(project_dir)
        if os.path.isdir(os.path.join(project_dir, d)) and not d.startswith(".")
    )


def _classify_subproject(sub_dir):
    # type: (str) -> tuple[bool, bool]
    """Return (has_telemetry, has_artifacts).

    Artifacts may be at the subproject root (legacy) or inside an
    artifacts/ subdirectory (current convention).
    """
    has_tel = False
    has_art = False
    for name in os.listdir(sub_dir):
        if _is_telemetry(name):
            has_tel = True
        elif name == "artifacts" and os.path.isdir(os.path.join(sub_dir, name)):
            # Check if the artifacts/ dir has any contents
            art_dir = os.path.join(sub_dir, name)
            if any(not f.startswith(".") for f in os.listdir(art_dir)):
                has_art = True
        elif not name.startswith("."):
            has_art = True
        if has_tel and has_art:
            break
    return has_tel, has_art


def _load_events(sub_dir):
    # type: (str) -> list[dict]
    """Load all otel-*.json files, parse, and sort by timestamp then sequence."""
    events = []
    for name in os.listdir(sub_dir):
        if not _is_telemetry(name):
            continue
        fpath = os.path.join(sub_dir, name)
        try:
            with open(fpath, "r") as f:
                obj = json.load(f)
            obj["_filename"] = name
            events.append(obj)
        except (json.JSONDecodeError, OSError):
            continue

    def sort_key(ev):
        # type: (dict) -> tuple
        ts = ev.get("timestamp", "")
        seq = 0
        attrs = ev.get("attributes", {})
        if isinstance(attrs, dict):
            seq = attrs.get("event.sequence", 0)
            if isinstance(seq, str):
                try:
                    seq = int(seq)
                except ValueError:
                    seq = 0
        return (ts, seq)

    events.sort(key=sort_key)
    return events


def _format_size(size):
    # type: (int) -> str
    if size < 1024:
        return "{} B".format(size)
    elif size < 1024 * 1024:
        return "{:.1f} KB".format(size / 1024.0)
    else:
        return "{:.1f} MB".format(size / (1024.0 * 1024.0))


def _format_ts(ts_str):
    # type: (str) -> str
    """Format ISO timestamp to a shorter display form."""
    if not ts_str:
        return ""
    # Remove trailing Z and microseconds for display
    try:
        ts_str = ts_str.replace("Z", "+00:00")
        dt = datetime.fromisoformat(ts_str)
        return dt.strftime("%H:%M:%S") + " UTC"
    except (ValueError, TypeError):
        return str(ts_str)[:19]


def _try_parse_json(s):
    # type: (str) -> object
    try:
        return json.loads(s)
    except (json.JSONDecodeError, TypeError):
        return None


def _build_tool_color_map(tool_names):
    # type: (list[str]) -> dict[str, str]
    """Assign colors to tools using golden-angle hue distribution."""
    colors = {}
    base_hue = 210
    golden = 137.508
    for idx, name in enumerate(sorted(set(tool_names))):
        hue = (base_hue + idx * golden) % 360
        colors[name] = "hsl({}, 72%, 48%)".format(int(round(hue)))
    return colors


def _parse_iso_to_minutes(ts_str, start_ts):
    # type: (str, str) -> float
    """Return minutes offset from start_ts."""
    try:
        ts = datetime.fromisoformat(ts_str.replace("Z", "+00:00"))
        start = datetime.fromisoformat(start_ts.replace("Z", "+00:00"))
        delta = (ts - start).total_seconds() / 60.0
        return max(0.0, delta)
    except (ValueError, TypeError, AttributeError):
        return 0.0


def _choose_tick_interval(total_minutes):
    # type: (float) -> float
    """Pick a tick interval that yields roughly 4-8 ticks."""
    candidates = [0.5, 1, 2, 5, 10, 15, 30, 60, 120, 300, 600]
    for c in candidates:
        if total_minutes / c <= 8:
            return c
    return total_minutes / 4.0


# ---------------------------------------------------------------------------
# HTML Templates
# ---------------------------------------------------------------------------

_CSS = """\
* { box-sizing: border-box; margin: 0; padding: 0; }
body {
    font-family: system-ui, -apple-system, BlinkMacSystemFont, "Segoe UI", sans-serif;
    background: #f5f6f8; color: #1c1c1c; line-height: 1.5;
    padding: 20px; max-width: 1200px; margin: 0 auto;
}
a { color: #0d6efd; text-decoration: none; }
a:hover { text-decoration: underline; }
h1 { color: #1c1c1c; margin-bottom: 16px; font-size: 1.5em; }
h2 { color: #1c1c1c; margin-bottom: 12px; font-size: 1.2em; }
.breadcrumb { margin-bottom: 16px; color: #495057; font-size: 0.9em; }
.breadcrumb a { color: #0d6efd; }

/* Project / subproject listing */
.listing { list-style: none; }
.listing li {
    background: white; border-radius: 8px; margin-bottom: 8px;
    padding: 12px 16px; border-left: 4px solid #6c757d;
    box-shadow: 0 1px 3px rgba(0,0,0,0.08);
}
.listing li:hover { background: #f8f9fa; }
.listing .meta { color: #495057; font-size: 0.85em; margin-left: 12px; }

/* Summary bar */
.summary {
    background: white; border-radius: 8px; padding: 12px 16px;
    margin-bottom: 16px; display: flex; flex-wrap: wrap; gap: 16px;
    border-left: 4px solid #6f42c1;
    box-shadow: 0 1px 3px rgba(0,0,0,0.08);
}
.summary .stat { font-size: 0.9em; }
.summary .stat .label { color: #495057; }
.summary .stat .value { color: #1c1c1c; font-weight: bold; }

/* Event cards */
.entry {
    background: white; border-radius: 8px; margin-bottom: 1rem;
    padding: 1rem; border-left: 4px solid #6c757d;
    box-shadow: 0 1px 3px rgba(0,0,0,0.08);
}
.entry header {
    display: flex; justify-content: space-between; align-items: baseline;
    margin-bottom: 0.5rem; gap: 0.5rem; flex-wrap: wrap;
}
.entry header div { font-weight: 600; }
.entry header small { color: #495057; font-size: 0.8em; }
.entry .body { font-size: 0.9em; }
.entry .body pre {
    background: #1e1e1e; color: #f8f8f2; padding: 0.75rem;
    border-radius: 6px; overflow-x: auto; font-size: 0.85em;
    max-height: 400px; overflow-y: auto; white-space: pre-wrap;
    word-break: break-word;
}
.entry details { margin-top: 0.5rem; }
.entry summary {
    cursor: pointer; color: #0d6efd; font-size: 0.85em;
    user-select: none;
}
.entry summary:hover { text-decoration: underline; }

/* Entry type colors — matches example-viewer exactly */
.entry-system       { border-left-color: #6c757d; }
.entry-user         { border-left-color: #007bff; }
.entry-assistant    { border-left-color: #6f42c1; }
.entry-tool         { border-left-color: #e36209; }
.entry-metric       { border-left-color: #198754; }
.entry-token-usage  { border-left-color: #0d9488; }
.entry-error        { border-left-color: #dc3545; }

/* Prompt text */
.prompt-text {
    background: #f8f9fa; padding: 0.75rem 1rem; border-radius: 6px;
    border-left: 3px solid #007bff; margin-top: 0.5rem;
    white-space: pre-wrap; word-break: break-word;
}

/* Key-value display */
.kv-table {
    width: 100%; border-collapse: collapse; margin: 0.5rem 0;
}
.kv-table th, .kv-table td {
    padding: 0.35rem 0.5rem; border-bottom: 1px solid #e5e5e5;
    vertical-align: top; text-align: left;
}
.kv-table th {
    width: 180px; color: #495057; background: #f8f9fa;
}

/* Status chips */
.status-chip {
    display: inline-block; padding: 0.1rem 0.6rem; border-radius: 999px;
    font-size: 0.8rem; margin-right: 0.5rem; text-transform: capitalize;
}
.status-chip.success { background: #d1e7dd; color: #0f5132; }
.status-chip.failure { background: #f8d7da; color: #842029; }

/* Artifacts table */
table.artifacts {
    width: 100%; border-collapse: collapse; margin-top: 8px;
}
table.artifacts th {
    text-align: left; padding: 0.35rem 0.5rem;
    border-bottom: 2px solid #e5e5e5;
    color: #495057; font-size: 0.85em; background: #f8f9fa;
}
table.artifacts td {
    padding: 0.35rem 0.5rem; border-bottom: 1px solid #e5e5e5;
}
table.artifacts tr:hover { background: #f8f9fa; }

/* Filter controls */
.controls {
    margin-bottom: 12px; display: flex; flex-wrap: wrap; gap: 8px;
    align-items: center;
}
.controls label {
    font-size: 0.85em; color: #495057; cursor: pointer;
    display: flex; align-items: center; gap: 4px;
}
.controls input[type=checkbox] { cursor: pointer; }

/* Buttons */
.nav-button {
    border: none; background: #0d6efd; color: white;
    padding: 0.3rem 0.7rem; border-radius: 999px; cursor: pointer;
    font-weight: 600; font-size: 0.75rem;
    transition: background 0.2s ease;
    display: inline-flex; align-items: center;
}
.nav-button:hover { background: #0b5ed7; }
.nav-button.secondary { background: #6c757d; }
.nav-button.secondary:hover { background: #5c636a; }
.nav-button.small { padding: 0.2rem 0.5rem; font-size: 0.7rem; }
.meta-toggle { background: #ffc107; color: #1c1c1c; }
.meta-toggle:hover { background: #ffca2c; }

/* Sub-project view links */
.view-links { display: flex; gap: 12px; margin-bottom: 16px; }
.view-links a {
    background: white; padding: 10px 20px; border-radius: 8px;
    border: 1px solid #e5e5e5; font-weight: bold;
    box-shadow: 0 1px 3px rgba(0,0,0,0.08);
}
.view-links a:hover { background: #f8f9fa; border-color: #0d6efd; }

/* Code blocks */
.code-block { margin: 0.5rem 0; }
.code-lang {
    font-size: 0.78rem; color: #6c757d; text-transform: uppercase;
    letter-spacing: 0.05em; margin-bottom: 0.25rem;
}

/* Collapsible meta toggle */
body.meta-hidden .entry.collapsible-meta { display: none; }

/* Source header */
.source-header {
    color: #495057; font-size: 0.9em; margin-bottom: 1rem;
}

/* Timeline panel — fixed at top */
.timeline-panel {
    position: fixed; top: 0; left: 0; right: 0; width: 100%;
    background: white; border-radius: 0 0 12px 12px;
    box-shadow: 0 8px 24px rgba(0,0,0,0.12);
    padding: 0.85rem 1.25rem 1rem; z-index: 30;
    border-bottom: 1px solid #e5e7eb;
}
.timeline-panel.hidden { display: none; }
.timeline-header {
    display: flex; align-items: flex-start; justify-content: flex-start;
    gap: 0.5rem 0.75rem; flex-wrap: wrap; width: 100%;
}
.timeline-range { color: #6c757d; font-size: 0.85rem; margin-top: 0.1rem; }
.timeline-scroll {
    position: relative; overflow-x: auto; overflow-y: hidden;
    padding-bottom: 0.25rem; margin-top: 0.65rem;
}
.timeline-scroll::-webkit-scrollbar { height: 10px; }
.timeline-scroll::-webkit-scrollbar-thumb { background: #cbd5e1; border-radius: 999px; }
.timeline-scroll::-webkit-scrollbar-track { background: #edf2f7; }
.timeline-track {
    position: relative; min-height: 64px; background: #f3f4f6;
    border: 1px solid #e5e7eb; border-radius: 10px;
    transition: height 0.15s ease;
}
.timeline-event {
    position: absolute; width: 16px; height: 16px; border-radius: 50%;
    border: 2px solid #ffffff; box-shadow: 0 2px 6px rgba(0,0,0,0.2);
    cursor: pointer; transform: translateX(-50%);
    transition: transform 0.12s ease, box-shadow 0.12s ease;
}
.timeline-event:hover {
    transform: translateX(-50%) scale(1.08);
    box-shadow: 0 4px 10px rgba(0,0,0,0.28);
}
.timeline-event.highlight {
    box-shadow: 0 0 0 4px #ffd166, 0 3px 10px rgba(0,0,0,0.32);
    transform: translateX(-50%) scale(1.1);
}
.timeline-axis {
    position: relative; margin-top: 0.35rem; height: 22px;
}
.timeline-tick {
    position: absolute; top: 0; transform: translateX(-50%);
    color: #6c757d; font-size: 0.75rem; text-align: center; white-space: nowrap;
}
.timeline-tick::before {
    content: ""; display: block; width: 1px; height: 8px;
    background: #ced4da; margin: 0 auto 2px;
}
.timeline-legend {
    display: flex; flex-wrap: wrap; gap: 0.35rem 0.75rem;
    margin-top: 0.6rem; font-size: 0.85rem; color: #495057;
}
.timeline-legend .legend-item {
    display: inline-flex; align-items: center; gap: 0.35rem;
    padding: 0.1rem 0.35rem; border-radius: 6px;
    background: #f8f9fb; border: 1px solid #e3e7ed;
    cursor: pointer; user-select: none;
}
.timeline-legend input[type="checkbox"] { margin: 0; cursor: pointer; }
.timeline-legend .swatch {
    display: inline-block; width: 12px; height: 12px;
    border-radius: 3px; border: 1px solid rgba(0,0,0,0.08);
}
.legend-actions { display: flex; gap: 0.5rem; flex-wrap: wrap; margin-top: 0.5rem; }
.timeline-toggle-btn {
    position: fixed; top: 0.75rem; right: 1rem;
    background: #0d6efd; color: white; border: none; border-radius: 999px;
    padding: 0.35rem 0.7rem; font-size: 0.75rem; font-weight: 700; cursor: pointer;
    box-shadow: 0 8px 18px rgba(13,110,253,0.25); z-index: 25;
}
.timeline-toggle-btn.hidden { display: none; }
.entry-highlight {
    box-shadow: 0 0 0 3px #ffd166 inset, 0 6px 16px rgba(0,0,0,0.16);
    transition: box-shadow 0.3s ease;
}
@media (max-width: 640px) {
    .timeline-panel { padding: 0.75rem 1rem 0.85rem; border-radius: 0 0 10px 10px; }
    .timeline-toggle-btn { right: 0.75rem; top: 0.65rem; }
}
"""

def _page(title, body, breadcrumbs=None):
    # type: (str, str, list[tuple[str, str]] | None) -> str
    bc = ""
    if breadcrumbs:
        parts = []
        for label, href in breadcrumbs:
            if href:
                parts.append('<a href="{}">{}</a>'.format(href, html.escape(label)))
            else:
                parts.append(html.escape(label))
        bc = '<div class="breadcrumb">{}</div>'.format(" / ".join(parts))
    return (
        "<!DOCTYPE html><html><head>"
        "<meta charset='utf-8'>"
        "<meta name='viewport' content='width=device-width,initial-scale=1'>"
        "<title>{title}</title>"
        "<style>{css}</style>"
        "</head><body>"
        "{bc}"
        "<h1>{title}</h1>"
        "{body}"
        "</body></html>"
    ).format(title=html.escape(title), css=_CSS, bc=bc, body=body)


# ---------------------------------------------------------------------------
# Page renderers
# ---------------------------------------------------------------------------

def _render_project_list(data_dir):
    # type: (str) -> str
    projects = _scan_projects(data_dir)
    if not projects:
        return _page("Telemetry Viewer", "<p>No projects found in data directory.</p>")
    if len(projects) == 1:
        # Auto-redirect
        return None  # Signal to redirect
    items = []
    for p in projects:
        pdir = os.path.join(data_dir, p)
        subs = _scan_subprojects(pdir)
        items.append(
            '<li><a href="/p/{href}">{name}</a>'
            '<span class="meta">{n} subproject{s}</span></li>'.format(
                href=urllib.parse.quote(p, safe=""),
                name=html.escape(p),
                n=len(subs),
                s="" if len(subs) == 1 else "s",
            )
        )
    return _page(
        "Projects",
        '<ul class="listing">{}</ul>'.format("\n".join(items)),
    )


def _render_subproject_list(data_dir, project):
    # type: (str, str) -> str
    pdir = os.path.join(data_dir, project)
    subs = _scan_subprojects(pdir)
    if not subs:
        return _page(
            project,
            "<p>No subprojects found.</p>",
            breadcrumbs=[("Projects", "/"), (project, "")],
        )
    items = []
    for s in subs:
        sdir = os.path.join(pdir, s)
        has_tel, has_art = _classify_subproject(sdir)
        meta_parts = []
        if has_tel:
            meta_parts.append("telemetry")
        if has_art:
            meta_parts.append("artifacts")
        file_count = len([f for f in os.listdir(sdir) if not f.startswith(".")])
        items.append(
            '<li><a href="/p/{proj}/{sub}">{name}</a>'
            '<span class="meta">{count} file{s} &middot; {types}</span></li>'.format(
                proj=urllib.parse.quote(project, safe=""),
                sub=urllib.parse.quote(s, safe=""),
                name=html.escape(s),
                count=file_count,
                s="" if file_count == 1 else "s",
                types=", ".join(meta_parts) if meta_parts else "empty",
            )
        )
    body = '<ul class="listing">{}</ul>'.format("\n".join(items))

    # Check for files at the project root (no subproject)
    root_files = []
    for name in sorted(os.listdir(pdir)):
        fpath = os.path.join(pdir, name)
        if os.path.isfile(fpath) and not name.startswith("."):
            root_files.append((name, os.path.getsize(fpath)))
    if root_files:
        rows = []
        for name, size in root_files:
            raw_href = "/raw/{}/{}".format(
                urllib.parse.quote(project, safe=""),
                urllib.parse.quote(name, safe=""),
            )
            rows.append(
                '<tr><td><a href="{href}">{name}</a></td><td>{size}</td></tr>'.format(
                    href=raw_href,
                    name=html.escape(name),
                    size=_format_size(size),
                )
            )
        body += (
            "<h2>Project Files</h2>"
            '<table class="artifacts">'
            "<thead><tr><th>Name</th><th>Size</th></tr></thead>"
            "<tbody>{}</tbody></table>"
        ).format("\n".join(rows))

    return _page(
        project,
        body,
        breadcrumbs=[("Projects", "/"), (project, "")],
    )


def _render_artifact_listing(data_dir, project, sub, rel_path=""):
    # type: (str, str, str, str) -> str
    """Render an artifact file/directory listing as an HTML table.

    rel_path is the path relative to the subproject dir (empty = root,
    "build-logs" = a subdirectory).
    """
    sdir = os.path.join(data_dir, project, sub)
    target = os.path.join(sdir, rel_path) if rel_path else sdir

    entries = []
    for name in sorted(os.listdir(target)):
        if name.startswith("."):
            continue
        # At the subproject root, skip telemetry files
        if not rel_path and _is_telemetry(name):
            continue
        fpath = os.path.join(target, name)
        if os.path.isdir(fpath):
            count = len([f for f in os.listdir(fpath) if not f.startswith(".")])
            entries.append((name, True, 0, count))
        else:
            size = os.path.getsize(fpath)
            entries.append((name, False, size, 0))

    if not entries:
        return "<p>No artifacts.</p>"

    rows = []
    for name, is_dir, size, child_count in entries:
        entry_rel = "{}/{}".format(rel_path, name) if rel_path else name
        if is_dir:
            dir_href = "/p/{}/{}/artifacts/{}".format(
                urllib.parse.quote(project, safe=""),
                urllib.parse.quote(sub, safe=""),
                urllib.parse.quote(entry_rel, safe="/"),
            )
            rows.append(
                '<tr><td><a href="{href}">{name}/</a></td>'
                '<td>{count} entries</td></tr>'.format(
                    href=dir_href,
                    name=html.escape(name),
                    count=child_count,
                )
            )
        else:
            raw_href = "/raw/{}/{}/{}".format(
                urllib.parse.quote(project, safe=""),
                urllib.parse.quote(sub, safe=""),
                urllib.parse.quote(entry_rel, safe="/"),
            )
            rows.append(
                '<tr><td><a href="{href}">{name}</a></td>'
                '<td>{size}</td></tr>'.format(
                    href=raw_href,
                    name=html.escape(name),
                    size=_format_size(size),
                )
            )

    return (
        '<table class="artifacts">'
        "<thead><tr><th>Name</th><th>Size</th></tr></thead>"
        "<tbody>{}</tbody></table>"
    ).format("\n".join(rows))


def _render_subproject_view(data_dir, project, sub):
    # type: (str, str, str) -> str | None
    """Render the subproject page.

    - No artifacts → returns None (signal to redirect to /log)
    - Has artifacts → shows "View Conversation" link (if telemetry exists)
      plus inline artifact listing
    """
    sdir = os.path.join(data_dir, project, sub)
    if not os.path.isdir(sdir):
        return None
    has_tel, has_art = _classify_subproject(sdir)

    # No artifacts — redirect straight to conversation log
    if not has_art:
        return None

    proj_href = "/p/{}".format(urllib.parse.quote(project, safe=""))
    base = "/p/{}/{}".format(
        urllib.parse.quote(project, safe=""),
        urllib.parse.quote(sub, safe=""),
    )
    bc = [("Projects", "/"), (project, proj_href), (sub, "")]

    parts = []
    if has_tel:
        parts.append(
            '<div class="view-links">'
            '<a href="{}/log">View Conversation</a>'
            '</div>'.format(base)
        )

    parts.append("<h2>Artifacts</h2>")
    # Show artifacts from artifacts/ subfolder if it exists, otherwise root
    art_subdir = os.path.join(sdir, "artifacts")
    if os.path.isdir(art_subdir) and any(
        not f.startswith(".") for f in os.listdir(art_subdir)
    ):
        parts.append(_render_artifact_listing(data_dir, project, sub, "artifacts"))
    else:
        parts.append(_render_artifact_listing(data_dir, project, sub))

    return _page("{} / {}".format(project, sub), "\n".join(parts), breadcrumbs=bc)


def _is_token_usage(ev):
    # type: (dict) -> bool
    """Detect token usage events from either Claude or Codex."""
    signal = ev.get("signal", "")
    event_name = (ev.get("event_name", "") or "").lower()
    attrs = ev.get("attributes", {}) or {}
    # Codex metric: codex.turn.token_usage / Claude metric: claude_code.token.usage
    metric_name = (ev.get("metric_name", "") or "").lower()
    if signal == "metric" and ("token_usage" in metric_name or "token.usage" in metric_name):
        return True
    # Codex SSE: response.completed with token counts
    if "sse_event" in event_name and attrs.get("event.kind") == "response.completed":
        return "input_token_count" in attrs or "output_token_count" in attrs
    return False


def _event_css_class(ev):
    # type: (dict) -> str
    """Map event to example-viewer entry-* CSS class."""
    signal = ev.get("signal", "")
    event_name = ev.get("event_name", "") or ""

    if _is_token_usage(ev):
        return "entry-token-usage"
    if signal == "metric":
        return "entry-metric"
    if signal == "trace":
        return "entry-system"

    en = event_name.lower()
    if "user_prompt" in en:
        return "entry-user"
    if "api_request" in en or "api_error" in en:
        return "entry-assistant"
    if "tool_decision" in en or "tool_result" in en:
        return "entry-tool"
    if "websocket" in en or "sse_event" in en or "conversation_starts" in en:
        return "entry-system"
    return "entry-system"


def _extract_mcp_server(attrs):
    # type: (dict) -> str
    """Extract MCP server name from attributes or tool_parameters JSON."""
    server = attrs.get("mcp_server_name", "")
    if server:
        return server
    params_str = attrs.get("tool_parameters", "")
    if params_str:
        try:
            params = json.loads(str(params_str))
            return params.get("mcp_server_name", "")
        except (ValueError, TypeError):
            pass
    return ""


def _event_label(ev):
    # type: (dict) -> str
    """Build the header label like the example viewer."""
    signal = ev.get("signal", "")
    event_name = ev.get("event_name", "") or ""
    attrs = ev.get("attributes", {}) or {}

    if _is_token_usage(ev):
        model = attrs.get("model", "")
        if model:
            return "Token Usage \u00b7 {}".format(model)
        return "Token Usage"
    if signal == "metric":
        return "Metric \u00b7 {}".format(ev.get("metric_name", "metric"))
    if signal == "trace":
        return "Trace \u00b7 {}".format(ev.get("span_name", "span"))

    en = event_name.lower()
    if "user_prompt" in en:
        return "User Prompt"
    if "api_request" in en:
        model = attrs.get("model", "")
        if model:
            return "API Request \u00b7 {}".format(model)
        return "API Request"
    if "api_error" in en:
        return "API Error"
    if "tool_decision" in en:
        tool = attrs.get("tool_name", "")
        decision = attrs.get("decision", "")
        if tool:
            label = "Tool Decision \u00b7 {}".format(tool)
            mcp_server = _extract_mcp_server(attrs)
            if mcp_server:
                label += " \u00b7 {}".format(mcp_server)
            if decision:
                label += " \u00b7 {}".format(decision)
            return label
        return "Tool Decision"
    if "tool_result" in en:
        tool = attrs.get("tool_name", "")
        if tool:
            label = "Tool Result \u00b7 {}".format(tool)
            mcp_server = _extract_mcp_server(attrs)
            if mcp_server:
                label += " \u00b7 {}".format(mcp_server)
            return label
        return "Tool Result"
    if "websocket_connect" in en:
        return "WebSocket Connect"
    if "websocket_event" in en:
        kind = attrs.get("event.kind", "")
        if kind:
            return "WebSocket Event \u00b7 {}".format(kind)
        return "WebSocket Event"
    if "websocket_request" in en:
        return "WebSocket Request"
    if "sse_event" in en:
        return "SSE Event"
    if "conversation_starts" in en:
        return "Conversation Start"
    if event_name == "log" or not event_name:
        return "System Log"
    return event_name


def _format_token_count(val):
    # type: (object) -> str
    """Format a token count as a comma-separated integer."""
    try:
        n = int(float(val))
        return "{:,}".format(n)
    except (ValueError, TypeError):
        return str(val)


def _render_token_usage(ev):
    # type: (dict) -> str
    """Render a token usage table from either Claude or Codex events."""
    signal = ev.get("signal", "")
    attrs = ev.get("attributes", {}) or {}
    rows = []

    if signal == "metric":
        # Codex: token_type attr (input, output, cached_input, etc.)
        # Claude: type attr (input, output, cacheRead, cacheCreation)
        # Group by model if multiple models present
        models = set(
            dp.get("attributes", {}).get("model", "")
            for dp in ev.get("data_points", [])
        )
        models.discard("")
        multi_model = len(models) > 1

        for dp in ev.get("data_points", []):
            dp_attrs = dp.get("attributes", {})
            token_type = dp_attrs.get("token_type", "") or dp_attrs.get("type", "")
            if not token_type:
                continue
            val = dp.get("sum", dp.get("asDouble", dp.get("asInt", "")))
            if val != "" and str(val) != "0" and float(val) != 0:
                # Normalize label: camelCase -> Title Case
                label = token_type
                if label[0].islower():
                    # camelCase (Claude) or snake_case (Codex)
                    import re as _re
                    label = _re.sub(r'([A-Z])', r' \1', label)  # camelCase
                    label = label.replace("_", " ").strip().title()
                if multi_model:
                    model = dp_attrs.get("model", "")
                    if model:
                        label = "{} ({})".format(label, model)
                rows.append((label, _format_token_count(val)))
    else:
        # Codex SSE response.completed: token counts as attributes
        token_keys = [
            ("input_token_count", "Input"),
            ("output_token_count", "Output"),
            ("cached_token_count", "Cached Input"),
            ("reasoning_token_count", "Reasoning Output"),
            ("tool_token_count", "Tool Input"),
        ]
        for key, label in token_keys:
            val = attrs.get(key)
            if val is not None and str(val) != "0":
                rows.append((label, _format_token_count(val)))

    if not rows:
        return ""

    table = '<table class="kv-table"><tbody>'
    for label, val in rows:
        table += "<tr><th>{}</th><td>{}</td></tr>".format(
            html.escape(label), html.escape(val)
        )
    table += "</tbody></table>"
    return table


def _render_event_body(ev):
    # type: (dict) -> str
    signal = ev.get("signal", "")
    attrs = ev.get("attributes", {}) or {}
    parts = []

    # Token usage gets its own renderer
    if _is_token_usage(ev):
        return _render_token_usage(ev)

    if signal == "log":
        event_name = (ev.get("event_name", "") or "").lower()

        # User prompt — show the prompt text
        if "user_prompt" in event_name:
            prompt = attrs.get("prompt", "")
            if prompt:
                parts.append('<div class="prompt-text">{}</div>'.format(
                    html.escape(str(prompt))
                ))

        # API request — show model, tokens, cost, duration
        elif "api_request" in event_name:
            info = []
            model = attrs.get("model", "")
            if model:
                info.append("<b>Model:</b> {}".format(html.escape(str(model))))
            for key in ("input_tokens", "output_tokens", "cache_read_tokens", "cache_creation_tokens"):
                val = attrs.get(key, "")
                if val and str(val) != "0":
                    label = key.replace("_", " ").title()
                    info.append("<b>{}:</b> {}".format(label, html.escape(str(val))))
            cost = attrs.get("cost_usd", "")
            if cost:
                try:
                    info.append("<b>Cost:</b> ${:.6f}".format(float(cost)))
                except (ValueError, TypeError):
                    info.append("<b>Cost:</b> {}".format(html.escape(str(cost))))
            dur = attrs.get("duration_ms", "")
            if dur:
                info.append("<b>Duration:</b> {}ms".format(html.escape(str(dur))))
            if info:
                parts.append(" &middot; ".join(info))

        # Tool decision — show tool name and decision
        elif "tool_decision" in event_name:
            tool = attrs.get("tool_name", "")
            decision = attrs.get("decision", "")
            source = attrs.get("source", "")
            mcp_server = _extract_mcp_server(attrs)
            line = []
            if tool:
                tool_label = html.escape(str(tool))
                if mcp_server:
                    tool_label += " &middot; {}".format(html.escape(str(mcp_server)))
                line.append("<b>{}</b>".format(tool_label))
            if decision:
                line.append(html.escape(str(decision)))
            if source:
                line.append("(source: {})".format(html.escape(str(source))))
            if line:
                parts.append(" &mdash; ".join(line))

        # Tool result — show tool name, success, duration, error, params, output
        elif "tool_result" in event_name:
            tool = attrs.get("tool_name", "")
            mcp_server = _extract_mcp_server(attrs)
            success = attrs.get("success", "")
            dur = attrs.get("duration_ms", "")
            error = attrs.get("error", "")
            info = []
            if tool:
                tool_label = html.escape(str(tool))
                if mcp_server:
                    tool_label += " &middot; {}".format(html.escape(str(mcp_server)))
                info.append("<b>{}</b>".format(tool_label))
            if success:
                color = "#4caf50" if str(success).lower() == "true" else "#f44336"
                info.append('<span style="color:{}">success={}</span>'.format(
                    color, html.escape(str(success))
                ))
            if dur:
                info.append("{}ms".format(html.escape(str(dur))))
            result_size = attrs.get("tool_result_size_bytes", "")
            if result_size:
                info.append("{} bytes".format(html.escape(str(result_size))))
            if info:
                parts.append(" &middot; ".join(info))

            # Error message (shown inline, highlighted)
            if error:
                parts.append(
                    '<div style="color:#d32f2f;background:#ffebee;padding:0.4rem 0.6rem;'
                    'border-radius:4px;margin:0.3rem 0;font-size:0.85rem;">'
                    '<b>Tool error:</b> {}</div>'.format(html.escape(str(error)))
                )

            # Tool parameters and output — expanded by default as "Results"
            results_parts = []
            params = attrs.get("tool_parameters", "") or attrs.get("arguments", "")
            if params:
                parsed = _try_parse_json(str(params))
                if parsed is not None and isinstance(parsed, dict):
                    # Render each key-value pair, expanding embedded
                    # newlines in long string values (e.g. full_command)
                    param_lines = []
                    for pk, pv in parsed.items():
                        if isinstance(pv, str) and ("\\n" in pv or len(pv) > 80):
                            param_lines.append("<b>{}</b>:".format(html.escape(pk)))
                            param_lines.append(html.escape(pv))
                        else:
                            param_lines.append("<b>{}</b>: {}".format(
                                html.escape(pk), html.escape(str(pv))
                            ))
                    results_parts.append(
                        "<b>Parameters</b><pre>{}</pre>".format("\n".join(param_lines))
                    )
                elif parsed is not None:
                    results_parts.append(
                        "<b>Parameters</b><pre>{}</pre>".format(
                            html.escape(json.dumps(parsed, indent=2))
                        )
                    )
                else:
                    results_parts.append(
                        "<b>Parameters</b><pre>{}</pre>".format(html.escape(str(params)))
                    )

            # Output (Codex puts it in attrs)
            output = attrs.get("output", "")
            if output:
                out_str = str(output)
                results_parts.append(
                    "<b>Output</b> ({} bytes)<pre>{}</pre>".format(
                        len(out_str), html.escape(out_str)
                    )
                )

            if results_parts:
                parts.append(
                    "<details open><summary>Results</summary>{}</details>".format(
                        "".join(results_parts)
                    )
                )

        # Websocket events — event.kind is already in the header, show
        # duration and model instead of repeating it
        elif "websocket" in event_name:
            dur = attrs.get("duration_ms", "")
            model = attrs.get("model", "")
            info = []
            if model:
                info.append(html.escape(str(model)))
            if dur and str(dur) != "0":
                info.append("{}ms".format(html.escape(str(dur))))
            if info:
                parts.append(" &middot; ".join(info))

    elif signal == "metric":
        metric_name = ev.get("metric_name", "")
        data_points = ev.get("data_points", [])

        # Find which attributes actually vary between data points —
        # those are the labels worth showing.  Everything else is
        # boilerplate that's the same on every data point.
        _METRIC_BOILERPLATE = {
            "app.version", "auth_mode", "originator",
            "session_source", "tmp_mem_enabled",
        }
        all_attr_keys = set()
        for dp in data_points:
            all_attr_keys.update(dp.get("attributes", {}).keys())
        varying_keys = []
        for k in sorted(all_attr_keys - _METRIC_BOILERPLATE):
            vals = set(
                str(dp.get("attributes", {}).get(k, ""))
                for dp in data_points
            )
            if len(vals) > 1:
                varying_keys.append(k)

        # Build context line: model (shown once) + any single-value
        # interesting attributes (e.g. tool=exec_command when all DPs
        # share the same tool)
        context_parts = []
        for k in ("model", "tool"):
            vals = set(
                str(dp.get("attributes", {}).get(k, ""))
                for dp in data_points
            )
            vals.discard("")
            if len(vals) == 1:
                context_parts.append("{}={}".format(k, vals.pop()))
        context = " ({})".format(", ".join(context_parts)) if context_parts else ""

        info = ["<b>{}</b>{}".format(
            html.escape(str(metric_name)), html.escape(context)
        )]
        for dp in data_points[:8]:
            dp_attrs = dp.get("attributes", {})
            # Extract value — check all common OTLP value fields
            val = ""
            for val_key in ("asDouble", "asInt", "sum", "count"):
                v = dp.get(val_key)
                if v is not None and str(v) != "":
                    val = v
                    break
            # Label from varying attributes only
            label = ", ".join(
                str(dp_attrs.get(k, "")) for k in varying_keys
                if dp_attrs.get(k, "")
            )
            val_str = html.escape(str(val)) if val != "" else ""
            if label and val_str:
                info.append("{}: {}".format(html.escape(label), val_str))
            elif val_str:
                info.append(val_str)
        if len(data_points) > 8:
            info.append("... +{} more".format(len(data_points) - 8))
        parts.append(" &middot; ".join(info))

    elif signal == "trace":
        span = ev.get("span_name", "")
        tid = ev.get("trace_id", "")[:12]
        parts.append("<b>{}</b> trace={}...".format(
            html.escape(str(span)), html.escape(tid)
        ))

    return "\n".join(parts)


def _compute_summary(events):
    # type: (list[dict]) -> dict
    """Extract summary stats from events."""
    total_cost = 0.0
    total_input = 0
    total_output = 0
    total_cache_read = 0
    models = set()
    api_count = 0
    tool_count = 0
    user_prompts = 0
    first_ts = ""
    last_ts = ""

    for ev in events:
        ts = ev.get("timestamp", "")
        if ts:
            if not first_ts or ts < first_ts:
                first_ts = ts
            if not last_ts or ts > last_ts:
                last_ts = ts

        signal = ev.get("signal", "")
        event_name = (ev.get("event_name", "") or "").lower()
        attrs = ev.get("attributes", {}) or {}

        if signal == "log":
            if "user_prompt" in event_name:
                user_prompts += 1
            elif "api_request" in event_name:
                api_count += 1
                model = attrs.get("model", "")
                if model:
                    models.add(str(model))
                try:
                    total_cost += float(attrs.get("cost_usd", 0))
                except (ValueError, TypeError):
                    pass
                try:
                    total_input += int(attrs.get("input_tokens", 0))
                except (ValueError, TypeError):
                    pass
                try:
                    total_output += int(attrs.get("output_tokens", 0))
                except (ValueError, TypeError):
                    pass
                try:
                    total_cache_read += int(attrs.get("cache_read_tokens", 0))
                except (ValueError, TypeError):
                    pass
            elif "tool_result" in event_name:
                tool_count += 1

            # Codex SSE response.completed — extract model and per-turn tokens
            elif "sse_event" in event_name and attrs.get("event.kind") == "response.completed":
                model = attrs.get("model", "")
                if model:
                    models.add(str(model))

        # Codex/Claude token usage metrics — use the LAST one as session total
        if _is_token_usage(ev) and signal == "metric":
            for dp in ev.get("data_points", []):
                dp_attrs = dp.get("attributes", {})
                model = dp_attrs.get("model", "")
                if model:
                    models.add(str(model))
                token_type = (dp_attrs.get("token_type", "") or dp_attrs.get("type", "")).lower()
                val = dp.get("sum", dp.get("asDouble", dp.get("asInt", 0)))
                try:
                    val = int(float(val))
                except (ValueError, TypeError):
                    continue
                if token_type in ("input",):
                    total_input = max(total_input, val)
                elif token_type in ("output",):
                    total_output = max(total_output, val)
                elif token_type in ("cached_input", "cacheread", "cache_read"):
                    total_cache_read = max(total_cache_read, val)

    return {
        "total_cost": total_cost,
        "total_input": total_input,
        "total_output": total_output,
        "total_cache_read": total_cache_read,
        "models": sorted(models),
        "api_count": api_count,
        "tool_count": tool_count,
        "user_prompts": user_prompts,
        "event_count": len(events),
        "first_ts": first_ts,
        "last_ts": last_ts,
    }


def _render_summary_bar(summary):
    # type: (dict) -> str
    stats = []
    if summary["models"]:
        stats.append(('<span class="label">Model:</span> '
                      '<span class="value">{}</span>').format(
            html.escape(", ".join(summary["models"]))
        ))
    if summary["total_cost"] > 0:
        stats.append(('<span class="label">Cost:</span> '
                      '<span class="value">${:.4f}</span>').format(
            summary["total_cost"]
        ))
    if summary["total_input"] or summary["total_output"]:
        stats.append(('<span class="label">Tokens:</span> '
                      '<span class="value">{:,} in / {:,} out / {:,} cached</span>').format(
            summary["total_input"], summary["total_output"], summary["total_cache_read"]
        ))
    if summary["user_prompts"]:
        stats.append(('<span class="label">Prompts:</span> '
                      '<span class="value">{}</span>').format(summary["user_prompts"]))
    if summary["api_count"]:
        stats.append(('<span class="label">API calls:</span> '
                      '<span class="value">{}</span>').format(summary["api_count"]))
    if summary["tool_count"]:
        stats.append(('<span class="label">Tool calls:</span> '
                      '<span class="value">{}</span>').format(summary["tool_count"]))
    stats.append(('<span class="label">Events:</span> '
                  '<span class="value">{}</span>').format(summary["event_count"]))

    return '<div class="summary">{}</div>'.format(
        "".join('<div class="stat">{}</div>'.format(s) for s in stats)
    )


def _get_tool_name(ev):
    # type: (dict) -> str | None
    """Extract tool name from event if it's a tool-related event.

    For MCP tools, includes the server name (e.g. 'mcp_tool · ghidra')
    so different MCP servers get distinct colors on the timeline.
    """
    event_name = (ev.get("event_name", "") or "").lower()
    attrs = ev.get("attributes", {}) or {}
    if "tool_result" in event_name or "tool_decision" in event_name:
        tool = attrs.get("tool_name", "") or None
        if tool:
            mcp_server = _extract_mcp_server(attrs)
            if mcp_server:
                return "{} \u00b7 {}".format(tool, mcp_server)
        return tool
    return None


def _render_conversation_log(data_dir, project, sub):
    # type: (str, str, str) -> str
    sdir = os.path.join(data_dir, project, sub)
    abs_sdir = os.path.abspath(sdir)
    events = _load_events(sdir)

    proj_href = "/p/{}".format(urllib.parse.quote(project, safe=""))
    sub_href = "/p/{}/{}".format(
        urllib.parse.quote(project, safe=""),
        urllib.parse.quote(sub, safe=""),
    )
    bc = [("Projects", "/"), (project, proj_href), (sub, sub_href), ("Log", "")]

    if not events:
        return _page(
            "{} / {} \u2014 Log".format(project, sub),
            "<p>No telemetry events found.</p>",
            breadcrumbs=bc,
        )

    summary = _compute_summary(events)
    summary_html = _render_summary_bar(summary)

    # Source header
    source_html = '<p class="source-header">Source: {} \u00b7 {} entries</p>'.format(
        html.escape(abs_sdir), len(events)
    )

    # Build filter checkboxes from event types
    event_types = set()
    for ev in events:
        signal = ev.get("signal", "")
        if signal == "metric":
            event_types.add("metric")
        elif signal == "trace":
            event_types.add("trace")
        else:
            event_types.add(ev.get("event_name", "other") or "other")

    filter_html = '<div class="controls"><span style="color:#495057;font-size:0.85em">Filter:</span>'
    for et in sorted(event_types):
        filter_html += (
            '<label><input type="checkbox" checked '
            'onchange="toggleType(this, \'{}\')">{}</label>'
        ).format(html.escape(et), html.escape(et))
    filter_html += "</div>"

    # Meta toggle button — default to hidden so users see the important stuff first
    meta_btn = (
        '<button class="nav-button meta-toggle" '
        'onclick="document.body.classList.toggle(\'meta-hidden\')">'
        'Show meta blocks</button>'
    )
    filter_html = meta_btn + " " + filter_html

    # --- Build timeline data and "next tool" links ---
    first_ts = summary["first_ts"]
    last_ts = summary["last_ts"]
    total_minutes = _parse_iso_to_minutes(last_ts, first_ts) if first_ts and last_ts else 0.0
    tick_interval = _choose_tick_interval(total_minutes) if total_minutes > 0 else 1.0

    # Collect all tool names for color map
    tool_names = []
    for ev in events:
        tn = _get_tool_name(ev)
        if tn:
            tool_names.append(tn)
    color_map = _build_tool_color_map(tool_names)

    # Build per-tool chain for "Next this tool" links
    # tool_last_idx[tool] = index of last seen entry for that tool
    tool_last_idx = {}  # type: dict[str, int]
    # next_tool_anchor[i] = anchor id of next entry with same tool
    next_tool_anchor = {}  # type: dict[int, str]

    # First pass: record tool indices
    tool_indices = {}  # type: dict[str, list[int]]
    for i, ev in enumerate(events):
        tn = _get_tool_name(ev)
        if tn:
            tool_indices.setdefault(tn, []).append(i)

    # Second pass: build next-links
    for tn, indices in tool_indices.items():
        for j in range(len(indices) - 1):
            next_tool_anchor[indices[j]] = "entry-{}".format(indices[j + 1])

    # Build per-event-type chain for "Next" buttons on API requests and user prompts
    next_type_anchor = {}  # type: dict[int, str]
    type_indices = {}  # type: dict[str, list[int]]
    for i, ev in enumerate(events):
        en = (ev.get("event_name", "") or "").lower()
        if "api_request" in en:
            type_indices.setdefault("api_request", []).append(i)
        elif "user_prompt" in en:
            type_indices.setdefault("user_prompt", []).append(i)
    for _tkey, indices in type_indices.items():
        for j in range(len(indices) - 1):
            next_type_anchor[indices[j]] = "entry-{}".format(indices[j + 1])

    # Build timeline event config for JS
    timeline_events = []
    for i, ev in enumerate(events):
        tn = _get_tool_name(ev)
        if not tn:
            continue
        ts_str = ev.get("timestamp", "")
        minutes = _parse_iso_to_minutes(ts_str, first_ts) if first_ts else 0.0
        timeline_events.append({
            "id": "entry-{}".format(i),
            "tool": tn,
            "color": color_map.get(tn, "hsl(210,72%,48%)"),
            "minutes": round(minutes, 4),
            "timestamp": _format_ts(ts_str),
        })

    timeline_config = json.dumps({
        "events": timeline_events,
        "totalMinutes": round(total_minutes, 4),
        "tickInterval": round(tick_interval, 4),
    })

    # Timeline HTML
    start_label = _format_ts(first_ts)
    end_label = _format_ts(last_ts)
    if total_minutes >= 60:
        dur_h = int(total_minutes // 60)
        dur_m = int(total_minutes % 60)
        duration_label = "{}h {}m".format(dur_h, dur_m) if dur_m else "{}h".format(dur_h)
    elif total_minutes >= 1:
        duration_label = "{}m".format(int(round(total_minutes)))
    else:
        duration_label = "{}s".format(int(round(total_minutes * 60)))

    timeline_html = (
        '<section class="timeline-panel" id="tool-timeline">'
        '<div class="timeline-header">'
        '<div><strong>Tool timeline</strong>'
        '<div class="timeline-range">{start} \u2192 {end} \u00b7 {dur}</div>'
        '</div>'
        '<button type="button" class="nav-button secondary" id="timeline-hide-btn">Hide</button>'
        '</div>'
        '<div class="timeline-scroll" id="timeline-scroll">'
        '<div class="timeline-track" id="timeline-track"></div>'
        '<div class="timeline-axis" id="timeline-axis"></div>'
        '</div>'
        '<div class="legend-actions">'
        '<button type="button" class="nav-button secondary small" id="legend-select-all">Select all</button>'
        '<button type="button" class="nav-button secondary small" id="legend-clear-all">Unselect all</button>'
        '</div>'
        '<div class="timeline-legend" id="timeline-legend"></div>'
        '</section>'
        '<button class="timeline-toggle-btn hidden" id="timeline-toggle-btn" type="button">'
        'Show tool timeline</button>'
    ).format(start=html.escape(start_label), end=html.escape(end_label), dur=html.escape(duration_label))

    # Build event cards
    cards = []
    for i, ev in enumerate(events):
        signal = ev.get("signal", "")
        css = _event_css_class(ev)
        label = _event_label(ev)
        ts_raw = ev.get("timestamp", "")
        ts = _format_ts(ts_raw)
        attrs = ev.get("attributes", {}) or {}
        seq = attrs.get("event.sequence", "")
        event_name = ev.get("event_name", "") or ""
        filename = ev.get("_filename", "")

        body_html = _render_event_body(ev)

        # Determine the data-type for filtering
        if signal == "metric":
            dtype = "metric"
        elif signal == "trace":
            dtype = "trace"
        else:
            dtype = event_name or "other"

        # Is this a "meta" event (system-level, collapsible)?
        # Auto-approved tool decisions (decision=approved, source=Config) are
        # noise in Codex sessions where everything is pre-approved — hide them
        is_auto_approved = (
            "tool_decision" in event_name.lower()
            and attrs.get("decision", "").lower() == "approved"
        )
        is_meta = css in ("entry-system", "entry-metric") or is_auto_approved
        meta_cls = " collapsible-meta" if is_meta else ""

        # Status chip for tool results
        status_html = ""
        if "tool_result" in event_name.lower():
            success = attrs.get("success", "")
            if str(success).lower() == "true":
                status_html = '<span class="status-chip success">success</span>'
            elif success:
                status_html = '<span class="status-chip failure">failed</span>'

        seq_str = ""
        if seq != "":
            seq_str = " \u00b7 #{}".format(seq)

        # "On timeline" and "Next this tool" buttons
        tn = _get_tool_name(ev)
        nav_buttons = ""
        if tn:
            nav_buttons += (
                ' <button type="button" class="nav-button secondary small jump-to-timeline"'
                ' data-target="entry-{idx}">On timeline</button>'
            ).format(idx=i)
            nxt = next_tool_anchor.get(i)
            if nxt:
                nav_buttons += (
                    ' <button type="button" class="nav-button secondary small jump-to-next"'
                    ' data-target="{nxt}">Next this tool</button>'
                ).format(nxt=nxt)

        # "Next API request" / "Next user prompt" buttons
        nxt_type = next_type_anchor.get(i)
        if nxt_type:
            en_lower = (event_name or "").lower()
            if "api_request" in en_lower:
                nav_buttons += (
                    ' <button type="button" class="nav-button secondary small jump-to-next"'
                    ' data-target="{nxt}">Next API request</button>'
                ).format(nxt=nxt_type)
            elif "user_prompt" in en_lower:
                nav_buttons += (
                    ' <button type="button" class="nav-button secondary small jump-to-next"'
                    ' data-target="{nxt}">Next user prompt</button>'
                ).format(nxt=nxt_type)

        card = (
            '<article class="entry {css}{meta}" data-type="{dtype}" id="entry-{idx}">'
            '<header>'
            '<div>{status} {label}{nav}</div>'
            '<small><span class="ev-ts" data-utc="{ts_raw}">{ts}</span>{seq} \u00b7 {fname}</small>'
            '</header>'
            '<div class="body">{body}</div>'
            '<details><summary>Full JSON</summary><pre>{json}</pre></details>'
            '</article>'
        ).format(
            css=css,
            meta=meta_cls,
            dtype=html.escape(dtype),
            idx=i,
            status=status_html,
            label=html.escape(label),
            nav=nav_buttons,
            ts=html.escape(ts),
            ts_raw=html.escape(ts_raw),
            seq=html.escape(seq_str),
            fname=html.escape(filename),
            body=body_html,
            json=html.escape(json.dumps(ev, indent=2, default=str)),
        )
        cards.append(card)

    js = """
<script>
// Default to hiding meta blocks (websocket, metrics, system events)
document.body.classList.add("meta-hidden");

function toggleType(cb, eventType) {{
    var cards = document.querySelectorAll('.entry[data-type="' + eventType + '"]');
    for (var i = 0; i < cards.length; i++) {{
        cards[i].style.display = cb.checked ? '' : 'none';
    }}
}}

(function() {{
    var cfg = {config};
    var panel = document.getElementById("tool-timeline");
    var showBtn = document.getElementById("timeline-toggle-btn");
    var hideBtn = document.getElementById("timeline-hide-btn");
    var scrollBox = document.getElementById("timeline-scroll");
    var track = document.getElementById("timeline-track");
    var axis = document.getElementById("timeline-axis");
    var legend = document.getElementById("timeline-legend");
    var selectAllBtn = document.getElementById("legend-select-all");
    var clearAllBtn = document.getElementById("legend-clear-all");

    if (!panel || !track || !axis || !legend || !scrollBox || !cfg || !cfg.events || !cfg.events.length) return;

    var total = Math.max(cfg.totalMinutes, 0.01);
    var laneHeight = 18, minGapPx = 18, pxPerMinute = 28, minWidth = 800;
    var totalWidth = minWidth;
    var bucketSize = 18, maxStackPerColumn = 5, columnOffset = 12;

    function scrollEntryIntoView(id) {{
        var target = document.getElementById(id);
        if (!target) return null;
        var gap = panel.classList.contains("hidden") ? 8 : (panel.offsetHeight + 8);
        var top = target.getBoundingClientRect().top + window.scrollY - gap;
        window.scrollTo({{ top: top, behavior: "smooth" }});
        return target;
    }}

    function highlightEntry(id) {{
        var target = scrollEntryIntoView(id);
        if (!target) return;
        target.classList.add("entry-highlight");
        setTimeout(function() {{ target.classList.remove("entry-highlight"); }}, 2200);
    }}

    function highlightMarker(id) {{
        var marker = track.querySelector('.timeline-event[data-id="' + id + '"]');
        if (!marker) return;
        marker.classList.add("highlight");
        setTimeout(function() {{ marker.classList.remove("highlight"); }}, 1200);
    }}

    function centerMarker(id) {{
        var marker = track.querySelector('.timeline-event[data-id="' + id + '"]');
        if (!marker) return;
        var center = marker.offsetLeft - scrollBox.clientWidth / 2 + marker.offsetWidth / 2;
        scrollBox.scrollTo({{ left: center, behavior: "smooth" }});
        highlightMarker(id);
    }}

    function applyFilters() {{
        var activeTools = new Set();
        legend.querySelectorAll('input[type="checkbox"]').forEach(function(cb) {{
            if (cb.checked) activeTools.add(cb.value);
        }});
        track.querySelectorAll(".timeline-event").forEach(function(node) {{
            node.style.display = activeTools.has(node.dataset.tool) ? "block" : "none";
        }});
    }}

    function formatTick(minutes) {{
        if (minutes >= 60) {{
            var h = Math.floor(minutes / 60);
            var m = Math.round(minutes - h * 60);
            return m ? h + "h " + m + "m" : h + "h";
        }}
        if (minutes < 1) return Math.round(minutes * 60) + "s";
        return Math.round(minutes) + "m";
    }}

    function placeEvents() {{
        var viewport = scrollBox.clientWidth || window.innerWidth || 1;
        var baseWidth = Math.max(minWidth, viewport, total * pxPerMinute);
        var inset = 14;
        var usable = Math.max(0, baseWidth - inset * 2);
        totalWidth = baseWidth + inset * 2;
        track.innerHTML = "";
        legend.innerHTML = "";
        axis.innerHTML = "";

        var buckets = new Map();
        var seenTools = new Map();
        var maxX = 0;

        cfg.events.forEach(function(ev) {{
            var rawX = inset + Math.max(0, Math.min(usable, (ev.minutes / total) * usable));
            var bucket = Math.floor(rawX / bucketSize);
            var count = buckets.get(bucket) || 0;
            var columnIdx = Math.floor(count / maxStackPerColumn);
            var laneIdx = count % maxStackPerColumn;
            var x = rawX + columnIdx * columnOffset;
            buckets.set(bucket, count + 1);
            maxX = Math.max(maxX, x + 16);

            var node = document.createElement("button");
            node.type = "button";
            node.className = "timeline-event";
            node.title = ev.tool + " \\u00b7 " + ev.timestamp;
            node.style.left = x + "px";
            node.style.top = (10 + laneIdx * laneHeight) + "px";
            node.style.backgroundColor = ev.color;
            node.dataset.tool = ev.tool;
            node.dataset.id = ev.id;
            node.addEventListener("click", function() {{
                highlightEntry(ev.id);
                highlightMarker(ev.id);
            }});
            track.appendChild(node);
            seenTools.set(ev.tool, ev.color);
        }});

        totalWidth = Math.max(totalWidth, maxX + inset + 10);
        track.style.width = totalWidth + "px";
        axis.style.width = totalWidth + "px";

        var maxCount = 0;
        buckets.forEach(function(v) {{ if (v > maxCount) maxCount = v; }});
        var height = Math.max(50, Math.min(maxStackPerColumn, maxCount || 1) * laneHeight + 24);
        track.style.height = height + "px";

        var usableTicks = Math.max(0, totalWidth - inset * 2);
        var interval = cfg.tickInterval || total;
        for (var m = 0; m <= cfg.totalMinutes + interval * 0.25; m += interval) {{
            var tick = document.createElement("div");
            tick.className = "timeline-tick";
            var px = inset + Math.min(usableTicks, Math.max(0, (m / total) * usableTicks));
            tick.style.left = px + "px";
            tick.textContent = formatTick(m);
            axis.appendChild(tick);
        }}

        for (var entry of seenTools.entries()) {{
            var toolName = entry[0], color = entry[1];
            var item = document.createElement("label");
            item.className = "legend-item";
            var cb = document.createElement("input");
            cb.type = "checkbox"; cb.value = toolName; cb.checked = true;
            var swatch = document.createElement("span");
            swatch.className = "swatch";
            swatch.style.backgroundColor = color;
            var lbl = document.createElement("span");
            lbl.textContent = toolName;
            item.appendChild(cb);
            item.appendChild(swatch);
            item.appendChild(lbl);
            legend.appendChild(item);
            cb.addEventListener("change", applyFilters);
        }}
        applyFilters();
    }}

    function updateLayout() {{
        placeEvents();
        var pad = panel.classList.contains("hidden") ? 12 : (panel.offsetHeight + 12);
        document.body.style.paddingTop = pad + "px";
    }}

    hideBtn.addEventListener("click", function() {{
        panel.classList.add("hidden");
        if (showBtn) showBtn.classList.remove("hidden");
        document.body.style.paddingTop = "12px";
    }});
    if (showBtn) showBtn.addEventListener("click", function() {{
        panel.classList.remove("hidden");
        showBtn.classList.add("hidden");
        updateLayout();
    }});
    if (selectAllBtn) selectAllBtn.addEventListener("click", function() {{
        legend.querySelectorAll('input[type="checkbox"]').forEach(function(cb) {{ cb.checked = true; }});
        applyFilters();
    }});
    if (clearAllBtn) clearAllBtn.addEventListener("click", function() {{
        legend.querySelectorAll('input[type="checkbox"]').forEach(function(cb) {{ cb.checked = false; }});
        applyFilters();
    }});

    scrollBox.addEventListener("wheel", function(e) {{
        if (Math.abs(e.deltaY) > Math.abs(e.deltaX)) {{
            scrollBox.scrollLeft += e.deltaY;
            e.preventDefault();
        }}
    }}, {{ passive: false }});

    // Wire up entry card buttons
    document.querySelectorAll(".jump-to-timeline").forEach(function(btn) {{
        btn.addEventListener("click", function() {{
            centerMarker(btn.dataset.target);
        }});
    }});
    document.querySelectorAll(".jump-to-next").forEach(function(btn) {{
        btn.addEventListener("click", function() {{
            var target = scrollEntryIntoView(btn.dataset.target);
            if (target) {{
                target.classList.add("entry-highlight");
                setTimeout(function() {{ target.classList.remove("entry-highlight"); }}, 2200);
            }}
            centerMarker(btn.dataset.target);
        }});
    }});

    updateLayout();
    window.addEventListener("resize", updateLayout);

    // Convert UTC timestamps to local time
    document.querySelectorAll(".ev-ts").forEach(function(el) {{
        var utc = el.getAttribute("data-utc");
        if (!utc) return;
        var d = new Date(utc);
        if (isNaN(d.getTime())) return;
        var local = d.toLocaleTimeString([], {{hour:"2-digit", minute:"2-digit", second:"2-digit", hour12: false}});
        el.textContent = el.textContent + " (" + local + " local)";
    }});
}})();
</script>
""".format(config=timeline_config)

    body = timeline_html + source_html + summary_html + filter_html + "\n".join(cards) + js
    return _page("{} / {} \u2014 Log".format(project, sub), body, breadcrumbs=bc)


def _render_artifacts(data_dir, project, sub, rel_path=""):
    # type: (str, str, str, str) -> str
    """Render an artifact directory listing page (for browsing into subdirs)."""
    proj_href = "/p/{}".format(urllib.parse.quote(project, safe=""))
    sub_href = "/p/{}/{}".format(
        urllib.parse.quote(project, safe=""),
        urllib.parse.quote(sub, safe=""),
    )
    bc = [("Projects", "/"), (project, proj_href), (sub, sub_href)]
    if rel_path:
        # Add breadcrumb segments for each path component
        parts = rel_path.split("/")
        for i, part in enumerate(parts):
            partial = "/".join(parts[:i + 1])
            if i < len(parts) - 1:
                href = "/p/{}/{}/artifacts/{}".format(
                    urllib.parse.quote(project, safe=""),
                    urllib.parse.quote(sub, safe=""),
                    urllib.parse.quote(partial, safe="/"),
                )
                bc.append((part, href))
            else:
                bc.append((part, ""))
    else:
        bc.append(("Artifacts", ""))

    title = "{} / {}".format(project, sub)
    if rel_path:
        title += " / {}".format(rel_path)

    listing = _render_artifact_listing(data_dir, project, sub, rel_path)
    return _page(title, listing, breadcrumbs=bc)


# ---------------------------------------------------------------------------
# HTTP Handler
# ---------------------------------------------------------------------------

def _make_handler(data_dir):
    # type: (str) -> type

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, format, *args):
            # Quieter logging
            pass

        def _send_html(self, code, body):
            # type: (int, str) -> None
            data = body.encode("utf-8")
            self.send_response(code)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)

        def _send_redirect(self, location):
            # type: (str) -> None
            self.send_response(302)
            self.send_header("Location", location)
            self.end_headers()

        def _send_404(self):
            # type: () -> None
            self._send_html(404, _page("Not Found", "<p>Page not found.</p>"))

        def _serve_raw_file(self, fpath):
            # type: (str) -> None
            mime, _ = mimetypes.guess_type(fpath)
            if mime is None:
                mime = "application/octet-stream"
            try:
                size = os.path.getsize(fpath)
                self.send_response(200)
                self.send_header("Content-Type", mime)
                self.send_header("Content-Length", str(size))
                self.end_headers()
                with open(fpath, "rb") as f:
                    while True:
                        chunk = f.read(65536)
                        if not chunk:
                            break
                        self.wfile.write(chunk)
            except OSError:
                self._send_404()

        def do_GET(self):
            # type: () -> None
            parsed = urllib.parse.urlparse(self.path)
            path = urllib.parse.unquote(parsed.path).rstrip("/") or "/"

            # / — project listing
            if path == "/":
                projects = _scan_projects(data_dir)
                if len(projects) == 1:
                    self._send_redirect("/p/{}".format(
                        urllib.parse.quote(projects[0], safe="")
                    ))
                    return
                result = _render_project_list(data_dir)
                self._send_html(200, result)
                return

            # /p/{project}
            m = re.match(r"^/p/([^/]+)$", path)
            if m:
                project = m.group(1)
                pdir = os.path.join(data_dir, project)
                if not os.path.isdir(pdir):
                    self._send_404()
                    return
                result = _render_subproject_list(data_dir, project)
                self._send_html(200, result)
                return

            # /p/{project}/{sub}
            m = re.match(r"^/p/([^/]+)/([^/]+)$", path)
            if m:
                project, sub = m.group(1), m.group(2)
                sdir = os.path.join(data_dir, project, sub)
                if not os.path.isdir(sdir):
                    self._send_404()
                    return
                result = _render_subproject_view(data_dir, project, sub)
                if result is None:
                    # No artifacts — go straight to conversation log
                    self._send_redirect("/p/{}/{}/log".format(
                        urllib.parse.quote(project, safe=""),
                        urllib.parse.quote(sub, safe=""),
                    ))
                else:
                    self._send_html(200, result)
                return

            # /p/{project}/{sub}/log
            m = re.match(r"^/p/([^/]+)/([^/]+)/log$", path)
            if m:
                project, sub = m.group(1), m.group(2)
                sdir = os.path.join(data_dir, project, sub)
                if not os.path.isdir(sdir):
                    self._send_404()
                    return
                result = _render_conversation_log(data_dir, project, sub)
                self._send_html(200, result)
                return

            # /p/{project}/{sub}/artifacts[/nested/path]
            m = re.match(r"^/p/([^/]+)/([^/]+)/artifacts(?:/(.+))?$", path)
            if m:
                project, sub = m.group(1), m.group(2)
                rel_path = m.group(3) or ""
                sdir = os.path.join(data_dir, project, sub)
                if not os.path.isdir(sdir):
                    self._send_404()
                    return
                # Prevent traversal
                if ".." in rel_path:
                    self._send_404()
                    return
                target = os.path.join(sdir, rel_path) if rel_path else sdir
                if not os.path.isdir(target):
                    self._send_404()
                    return
                result = _render_artifacts(data_dir, project, sub, rel_path)
                self._send_html(200, result)
                return

            # /raw/{project}/{filename} — serve project-root file
            m = re.match(r"^/raw/([^/]+)/([^/]+)$", path)
            if m:
                project, filename = m.group(1), m.group(2)
                if ".." in filename or filename.startswith("/"):
                    self._send_404()
                    return
                fpath = os.path.join(data_dir, project, filename)
                if not os.path.isfile(fpath):
                    # Not a project-root file — fall through to sub/filename
                    pass
                else:
                    self._serve_raw_file(fpath)
                    return

            # /raw/{project}/{sub}/{filename} — serve raw file
            m = re.match(r"^/raw/([^/]+)/([^/]+)/(.+)$", path)
            if m:
                project, sub, filename = m.group(1), m.group(2), m.group(3)
                if ".." in filename or filename.startswith("/"):
                    self._send_404()
                    return
                fpath = os.path.join(data_dir, project, sub, filename)
                if not os.path.isfile(fpath):
                    self._send_404()
                    return
                self._serve_raw_file(fpath)
                return

            self._send_404()

    return Handler


def serve(data_dir, port):
    # type: (str, int) -> None
    data_dir = os.path.abspath(data_dir)
    if not os.path.isdir(data_dir):
        print("Error: data directory '{}' does not exist.".format(data_dir))
        return

    handler = _make_handler(data_dir)
    server = HTTPServer(("127.0.0.1", port), handler)
    print("Telemetry viewer running at http://127.0.0.1:{}".format(port))
    print("Serving data from: {}".format(data_dir))
    print("Press Ctrl+C to stop.")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\nStopped.")
        server.server_close()
