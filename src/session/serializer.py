"""Bidirectional JSON serialization for Session objects.

Converts Session → JSON-compatible dict (for API responses / file persistence)
and JSON → Session (for resuming / loading).
"""

from __future__ import annotations

import json
from typing import Any

from session.models import (
    AssistantMessage,
    TextBlock,
    ToolCallBlock,
    ToolResult,
    ToolStatus,
    Turn,
    UserMessage,
    _now_ms,
)
from session.session import Session, SessionStats


# ---------------------------------------------------------------------------
# Serialize: Session → JSON-compatible dict
# ---------------------------------------------------------------------------

def session_to_dict(session: Session) -> dict[str, Any]:
    """Convert a Session to a JSON-serializable dict."""
    return {
        "session_id": session.session_id,
        "created_at": session.created_at,
        "turns": [_turn_to_dict(t) for t in session.turns],
        "stats": _stats_to_dict(session.stats()),
    }


def session_to_json(session: Session, indent: int = 2) -> str:
    """Convert a Session to a JSON string."""
    return json.dumps(session_to_dict(session), indent=indent, ensure_ascii=False)


def _turn_to_dict(turn: Turn) -> dict[str, Any]:
    return {
        "user_message": {
            "id": turn.user_message.id,
            "timestamp": turn.user_message.timestamp,
            "text": turn.user_message.text,
            "content": turn.user_message.content if not isinstance(turn.user_message.content, str) else None,
            "meta": turn.user_message.meta,
        },
        "assistant_messages": [
            _assistant_message_to_dict(m) for m in turn.assistant_messages
        ],
        "tool_results": [
            _tool_result_to_dict(r) for r in turn.tool_results
        ],
        "elapsed_ms": turn.elapsed_ms,
        "start_time": turn.start_time,
        "end_time": turn.end_time,
    }


def _assistant_message_to_dict(msg: AssistantMessage) -> dict[str, Any]:
    blocks: list[dict[str, Any]] = []
    for block in msg.content:
        if isinstance(block, TextBlock):
            blocks.append({"type": "text", "text": block.text, "id": block.id})
        elif isinstance(block, ToolCallBlock):
            d: dict[str, Any] = {
                "type": "tool_call",
                "tool_use_id": block.tool_use_id,
                "name": block.name,
                "input": block.input,
                "status": block.status,
                "id": block.id,
                "timestamp": block.timestamp,
            }
            if block.started_at:
                d["started_at"] = block.started_at
            if block.completed_at:
                d["completed_at"] = block.completed_at
            if block.elapsed_ms is not None:
                d["elapsed_ms"] = block.elapsed_ms
            if block.result is not None:
                d["result"] = _tool_result_to_dict(block.result)
            blocks.append(d)
    return {
        "id": msg.id,
        "timestamp": msg.timestamp,
        "role": msg.role,
        "text": msg.text,
        "blocks": blocks,
        "usage": msg.usage,
    }


def _tool_result_to_dict(result: ToolResult) -> dict[str, Any]:
    return {
        "tool_use_id": result.tool_use_id,
        "content": result.content,
        "is_error": result.is_error,
        "id": result.id,
        "timestamp": result.timestamp,
    }


def _stats_to_dict(stats: SessionStats) -> dict[str, Any]:
    return {
        "total_turns": stats.total_turns,
        "total_messages": stats.total_messages,
        "total_tool_calls": stats.total_tool_calls,
        "tool_calls_completed": stats.tool_calls_completed,
        "tool_calls_errored": stats.tool_calls_errored,
        "total_input_tokens": stats.total_input_tokens,
        "total_output_tokens": stats.total_output_tokens,
        "total_duration_ms": stats.total_duration_ms,
        "avg_tool_latency_ms": round(stats.avg_tool_latency_ms, 1),
    }


# ---------------------------------------------------------------------------
# Deserialize: JSON → Session
# ---------------------------------------------------------------------------

def session_from_json(data: str | dict[str, Any]) -> Session:
    """Build a Session from a JSON string or dict (as produced by session_to_dict)."""
    if isinstance(data, str):
        obj = json.loads(data)
    else:
        obj = data

    session = Session(session_id=obj.get("session_id"))

    for turn_data in obj.get("turns", []):
        # User message — preserve original content shape (text or list)
        um_data = turn_data["user_message"]
        raw_text = um_data.get("text", "")
        raw_content = um_data.get("content")
        if raw_content is not None:
            session.add_user_message(raw_content)
        else:
            session.add_user_message(raw_text)

        # Assistant messages
        for am_data in turn_data.get("assistant_messages", []):
            blocks: list[TextBlock | ToolCallBlock] = []
            for b in am_data.get("blocks", []):
                btype = b.get("type", "")
                if btype == "text":
                    blocks.append(TextBlock(text=b.get("text", ""), id=b.get("id", "")))
                elif btype == "tool_call":
                    tc = ToolCallBlock(
                        tool_use_id=b.get("tool_use_id", ""),
                        name=b.get("name", ""),
                        input=b.get("input", {}),
                        status=b.get("status", ToolStatus.PENDING),
                        id=b.get("id", ""),
                        timestamp=b.get("timestamp", _now_ms()),
                    )
                    tc.started_at = b.get("started_at")
                    tc.completed_at = b.get("completed_at")
                    if "result" in b:
                        r = b["result"]
                        tc.result = ToolResult(
                            tool_use_id=r.get("tool_use_id", ""),
                            content=r.get("content", ""),
                            is_error=r.get("is_error", False),
                            id=r.get("id", ""),
                            timestamp=r.get("timestamp", _now_ms()),
                        )
                    blocks.append(tc)
            session.add_assistant_message(blocks, usage=am_data.get("usage", {}))

    return session
