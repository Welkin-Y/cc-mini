"""Flask visualization server for session inspection.

Starts an HTTP server that serves:
  GET  /              — interactive HTML session inspector
  GET  /api/session   — full session state as JSON
  POST /api/session/message — add a message to the session
  POST /api/session/clear   — reset the session

Usage::

    python -m session._viz.server          # starts on port 8080
    SESSION_PORT=9090 python -m session._viz.server  # custom port

Or via Docker::

    docker-compose up
"""

from __future__ import annotations

import json
import os
import sys
from pathlib import Path

from flask import Flask, jsonify, render_template, request

# Allow running from repo root or from within the session package
_VIZ_DIR = Path(__file__).resolve().parent
_TEMPLATE_DIR = _VIZ_DIR / "templates"
_STATIC_DIR = _VIZ_DIR / "static"

app = Flask(
    __name__,
    template_folder=str(_TEMPLATE_DIR),
    static_folder=str(_STATIC_DIR),
)

# ---------------------------------------------------------------------------
# In-memory session store for the viz server
# Each key is a session_id → Session object
# ---------------------------------------------------------------------------
_sessions: dict[str, object] = {}  # str → Session

# Default demo session (lazily created on first access)
_DEMO_ID = "__demo__"


def _get_session(session_id: str | None = None) -> object:
    """Get or create a Session for the given id."""
    sid = session_id or _DEMO_ID
    if sid not in _sessions:
        from session import Session
        from session.models import TextBlock, ToolCallBlock, ToolResult

        s = Session(session_id=sid)
        _sessions[sid] = s
    return _sessions[sid]


# ---------------------------------------------------------------------------
# API routes
# ---------------------------------------------------------------------------

@app.route("/api/session")
@app.route("/api/session/<session_id>")
def api_get_session(session_id: str | None = None):
    """Return full session state as JSON."""
    from session.serializer import session_to_dict

    s = _get_session(session_id)
    return jsonify(session_to_dict(s))


@app.route("/api/session/message", methods=["POST"])
@app.route("/api/session/<session_id>/message", methods=["POST"])
def api_add_message(session_id: str | None = None):
    """Add a message to the session.

    Expects JSON body with one of:
      {"role": "user", "content": "..."}
      {"role": "assistant", "content": [{"type": "text", "text": "..."}, ...]}
    """
    from session.models import TextBlock, ToolCallBlock

    s = _get_session(session_id)
    data = request.get_json(force=True)
    role = data.get("role", "user")
    content = data.get("content", "")
    usage = data.get("usage", {})

    if role == "user":
        s.add_user_message(content)
    elif role == "assistant":
        blocks = []
        if isinstance(content, list):
            for block in content:
                btype = block.get("type", "")
                if btype == "text":
                    blocks.append(TextBlock(text=block.get("text", "")))
                elif btype in ("tool_call", "tool_use"):
                    blocks.append(ToolCallBlock(
                        tool_use_id=block.get("tool_use_id") or block.get("id", ""),
                        name=block.get("name", ""),
                        input=block.get("input", {}),
                    ))
        s.add_assistant_message(blocks, usage=usage)

    return jsonify({"ok": True, "session_id": s.session_id})


@app.route("/api/session/message/<message_id>/edit", methods=["POST"])
@app.route("/api/session/<session_id>/message/<message_id>/edit", methods=["POST"])
def api_edit_message(session_id: str | None = None, message_id: str = ""):
    """Edit a message's content."""
    s = _get_session(session_id)
    data = request.get_json(force=True)
    new_content = data.get("content", "")
    ok = s.edit_message(message_id, new_content)
    return jsonify({"ok": ok})


@app.route("/api/session/message/<message_id>/delete", methods=["POST"])
@app.route("/api/session/<session_id>/message/<message_id>/delete", methods=["POST"])
def api_delete_message(session_id: str | None = None, message_id: str = ""):
    """Delete a message and its tool calls/results."""
    s = _get_session(session_id)
    n = s.delete_message(message_id)
    return jsonify({"ok": n > 0, "removed": n})


@app.route("/api/session/tool/<tool_use_id>/retry", methods=["POST"])
@app.route("/api/session/<session_id>/tool/<tool_use_id>/retry", methods=["POST"])
def api_retry_tool(session_id: str | None = None, tool_use_id: str = ""):
    """Reset a tool call to pending for re-execution."""
    s = _get_session(session_id)
    ok = s.retry_tool_call(tool_use_id)
    return jsonify({"ok": ok})


@app.route("/api/session/rollback/<message_id>", methods=["POST"])
@app.route("/api/session/<session_id>/rollback/<message_id>", methods=["POST"])
def api_rollback(session_id: str | None = None, message_id: str = ""):
    """Roll back the session to before the given message."""
    s = _get_session(session_id)
    n = s.rollback_to(message_id)
    return jsonify({"ok": n > 0, "removed": n})


@app.route("/api/session/inject", methods=["POST"])
@app.route("/api/session/<session_id>/inject", methods=["POST"])
def api_inject(session_id: str | None = None):
    """Inject a correction message into the session."""
    s = _get_session(session_id)
    data = request.get_json(force=True)
    text = data.get("text", "")
    msg = s.inject_correction(text)
    return jsonify({"ok": True, "message_id": msg.id})


# ---- Checkpoint API ----

@app.route("/api/session/checkpoint", methods=["POST"])
@app.route("/api/session/<session_id>/checkpoint", methods=["POST"])
def api_create_checkpoint(session_id: str | None = None):
    """Create a named checkpoint of the current session + workspace state."""
    from session.checkpoint import get_store

    s = _get_session(session_id)
    data = request.get_json(force=True) or {}
    label = data.get("label", "checkpoint")
    store = get_store(s.session_id, cwd=os.getcwd())
    cp = store.save(s, label)
    return jsonify({"ok": True, "checkpoint": {
        "id": cp.id, "label": cp.label, "branch": cp.branch,
        "git_sha": cp.git_sha, "git_dirty": cp.git_dirty,
        "message_count": cp.message_count,
    }})


@app.route("/api/session/checkpoints")
@app.route("/api/session/<session_id>/checkpoints")
def api_list_checkpoints(session_id: str | None = None):
    """List all checkpoints for this session."""
    from session.checkpoint import get_store

    s = _get_session(session_id)
    store = get_store(s.session_id, cwd=os.getcwd())
    cps = store.list()
    return jsonify({"checkpoints": [{
        "id": cp.id, "label": cp.label, "branch": cp.branch,
        "timestamp": cp.timestamp, "message_index": cp.message_index,
        "message_count": cp.message_count,
        "parent_checkpoint_id": cp.parent_checkpoint_id,
        "git_sha": cp.git_sha, "git_dirty": cp.git_dirty,
        "git_files_changed": cp.git_files_changed,
    } for cp in cps], "branches": store.branches()})


@app.route("/api/session/checkpoint/<checkpoint_id>/restore", methods=["POST"])
@app.route("/api/session/<session_id>/checkpoint/<checkpoint_id>/restore", methods=["POST"])
def api_restore_checkpoint(session_id: str | None = None, checkpoint_id: str = ""):
    """Restore session to a checkpoint."""
    from session.checkpoint import get_store

    s = _get_session(session_id)
    store = get_store(s.session_id, cwd=os.getcwd())
    n = store.restore(s, checkpoint_id)
    return jsonify({"ok": n > 0, "removed": n})


@app.route("/api/session/checkpoint/<checkpoint_id>/branch", methods=["POST"])
@app.route("/api/session/<session_id>/checkpoint/<checkpoint_id>/branch", methods=["POST"])
def api_branch_checkpoint(session_id: str | None = None, checkpoint_id: str = ""):
    """Create a new branch from a checkpoint."""
    from session.checkpoint import get_store

    s = _get_session(session_id)
    data = request.get_json(force=True) or {}
    branch_name = data.get("name", "branch")
    store = get_store(s.session_id, cwd=os.getcwd())
    try:
        cp = store.create_branch(s, checkpoint_id, branch_name)
        return jsonify({"ok": True, "checkpoint_id": cp.id, "branch": branch_name})
    except ValueError as e:
        return jsonify({"ok": False, "error": str(e)}), 400


@app.route("/api/session/checkpoint/<checkpoint_id>", methods=["DELETE"])
@app.route("/api/session/<session_id>/checkpoint/<checkpoint_id>", methods=["DELETE"])
def api_delete_checkpoint(session_id: str | None = None, checkpoint_id: str = ""):
    """Delete a checkpoint."""
    from session.checkpoint import get_store

    s = _get_session(session_id)
    store = get_store(s.session_id, cwd=os.getcwd())
    ok = store.delete(checkpoint_id)
    return jsonify({"ok": ok})


@app.route("/api/session/branch/<branch_name>", methods=["POST"])
@app.route("/api/session/<session_id>/branch/<branch_name>", methods=["POST"])
def api_switch_branch(session_id: str | None = None, branch_name: str = ""):
    """Switch session to the tip of a branch."""
    from session.checkpoint import get_store

    s = _get_session(session_id)
    store = get_store(s.session_id, cwd=os.getcwd())
    try:
        n = store.switch_branch(s, branch_name)
        return jsonify({"ok": True, "removed": n, "branch": branch_name})
    except ValueError as e:
        return jsonify({"ok": False, "error": str(e)}), 400


@app.route("/api/session/tool-result", methods=["POST"])
@app.route("/api/session/<session_id>/tool-result", methods=["POST"])
def api_add_tool_result(session_id: str | None = None):
    """Record a tool execution result.

    Expects JSON body:
      {"tool_use_id": "...", "content": "...", "is_error": false}
    """
    from session.models import ToolResult

    s = _get_session(session_id)
    data = request.get_json(force=True)
    tool_use_id = data.get("tool_use_id", "")
    if not tool_use_id:
        return jsonify({"ok": False, "error": "tool_use_id is required"}), 400
    content = data.get("content", "")
    is_error = data.get("is_error", False)

    result = ToolResult(tool_use_id=tool_use_id, content=content, is_error=is_error)
    s.complete_tool_call(tool_use_id, result)

    return jsonify({"ok": True})


@app.route("/api/session/clear", methods=["POST"])
@app.route("/api/session/<session_id>/clear", methods=["POST"])
def api_clear_session(session_id: str | None = None):
    """Reset the session."""
    s = _get_session(session_id)
    s.clear()
    return jsonify({"ok": True})


@app.route("/api/session/demo", methods=["POST"])
@app.route("/api/session/<session_id>/demo", methods=["POST"])
def api_load_demo(session_id: str | None = None):
    """Load demo data into the session for quick visualization testing."""
    from session import Session
    from session.models import TextBlock, ToolCallBlock, ToolResult

    sid = session_id or _DEMO_ID
    s = Session(session_id=sid)
    _sessions[sid] = s

    # Turn 1: simple Q&A
    s.add_user_message("What Python files are in this project?")
    s.add_assistant_message(
        [TextBlock(text="Let me search for Python files."),
         ToolCallBlock(tool_use_id="tu_1", name="Glob",
                       input={"pattern": "**/*.py"})],
        usage={"input_tokens": 1200, "output_tokens": 80},
    )
    s.start_tool_call("tu_1")
    s.complete_tool_call("tu_1", ToolResult(
        tool_use_id="tu_1",
        content="src/core/engine.py\nsrc/core/llm.py\nsrc/core/config.py\nsrc/tools/bash.py\nsrc/tools/file_read.py",
    ))
    s.add_assistant_message(
        [TextBlock(text="Found 5 Python files:\n\n"
                        "- `src/core/engine.py`\n"
                        "- `src/core/llm.py`\n"
                        "- `src/core/config.py`\n"
                        "- `src/tools/bash.py`\n"
                        "- `src/tools/file_read.py`")],
        usage={"input_tokens": 500, "output_tokens": 90},
    )

    # Turn 2: read a file (simulated)
    s.add_user_message("Read engine.py and explain the tool loop")
    s.add_assistant_message(
        [TextBlock(text="Let me read that file."),
         ToolCallBlock(tool_use_id="tu_2", name="Read",
                       input={"file_path": "src/core/engine.py"})],
        usage={"input_tokens": 1400, "output_tokens": 60},
    )
    s.start_tool_call("tu_2")
    s.complete_tool_call("tu_2", ToolResult(
        tool_use_id="tu_2",
        content="class Engine:\n    def submit(self, user_input):\n        ...",
    ))
    s.add_assistant_message(
        [TextBlock(text="The `submit()` method implements an agentic loop — it sends "
                        "messages to the LLM, processes tool calls in batches, and "
                        "feeds results back until the model returns `end_turn`.")],
        usage={"input_tokens": 800, "output_tokens": 120},
    )

    # Turn 3: with an errored tool
    s.add_user_message("Run a command that fails")
    s.add_assistant_message(
        [TextBlock(text="Running that command."),
         ToolCallBlock(tool_use_id="tu_3", name="Bash",
                       input={"command": "nonexistent-command --flag"})],
        usage={"input_tokens": 1100, "output_tokens": 50},
    )
    s.start_tool_call("tu_3")
    s.complete_tool_call("tu_3", ToolResult(
        tool_use_id="tu_3",
        content="bash: nonexistent-command: command not found",
        is_error=True,
    ))
    s.add_assistant_message(
        [TextBlock(text="The command failed with 'command not found'. "
                        "Let me try a different approach."),
         ToolCallBlock(tool_use_id="tu_4", name="Bash",
                       input={"command": "which python3"})],
        usage={"input_tokens": 900, "output_tokens": 70},
    )
    s.start_tool_call("tu_4")
    s.complete_tool_call("tu_4", ToolResult(
        tool_use_id="tu_4",
        content="/usr/bin/python3",
    ))
    s.add_assistant_message(
        [TextBlock(text="Python is at `/usr/bin/python3`. Try using that instead.")],
        usage={"input_tokens": 400, "output_tokens": 50},
    )

    return jsonify({"ok": True, "session_id": sid})


# ---------------------------------------------------------------------------
# HTML viewer
# ---------------------------------------------------------------------------

@app.route("/")
def index():
    """Serve the interactive session inspector HTML page."""
    return render_template("session.html")


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

def main():
    port = int(os.environ.get("SESSION_PORT", "8080"))
    host = os.environ.get("SESSION_HOST", "0.0.0.0")
    debug = os.environ.get("SESSION_DEBUG", "0") == "1"

    print(f" Session viz server → http://{host}:{port}")
    print(f"   Demo data: curl -X POST http://{host}:{port}/api/session/demo")
    app.run(host=host, port=port, debug=debug)


if __name__ == "__main__":
    main()
