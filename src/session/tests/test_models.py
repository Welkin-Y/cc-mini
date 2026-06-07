"""Tests for session models — typed dataclasses."""
from __future__ import annotations

import pytest

from session.models import (
    AssistantMessage,
    SessionStats,
    TextBlock,
    ToolCallBlock,
    ToolResult,
    ToolStatus,
    Turn,
    UserMessage,
)


class TestUserMessage:
    def test_text_content(self):
        msg = UserMessage(content="hello world")
        assert msg.text == "hello world"
        assert msg.role == "user"
        assert msg.id

    def test_list_content(self):
        msg = UserMessage(content=[{"type": "text", "text": "hello"}])
        assert msg.text == "hello"

    def test_mixed_list_content(self):
        msg = UserMessage(content=[
            {"type": "text", "text": "part1"},
            {"type": "text", "text": "part2"},
        ])
        assert msg.text == "part1part2"

    def test_tool_result_content_is_not_text(self):
        """tool_result blocks are not extracted by .text (only .text blocks are)."""
        msg = UserMessage(content=[
            {"type": "tool_result", "tool_use_id": "x", "content": "result"},
        ])
        assert msg.text == ""


class TestAssistantMessage:
    def test_text_only(self):
        msg = AssistantMessage(content=[TextBlock(text="hello")])
        assert msg.text == "hello"
        assert not msg.has_tool_calls
        assert len(msg.text_blocks) == 1
        assert len(msg.tool_calls) == 0

    def test_tool_call_only(self):
        tc = ToolCallBlock(tool_use_id="tu_1", name="Read", input={"file_path": "x.py"})
        msg = AssistantMessage(content=[tc])
        assert msg.text == ""
        assert msg.has_tool_calls
        assert len(msg.text_blocks) == 0
        assert len(msg.tool_calls) == 1

    def test_mixed(self):
        msg = AssistantMessage(content=[
            TextBlock(text="Let me read that."),
            ToolCallBlock(tool_use_id="tu_1", name="Read", input={"file_path": "x.py"}),
        ])
        assert msg.text == "Let me read that."
        assert msg.has_tool_calls


class TestToolCallBlock:
    def test_defaults(self):
        tc = ToolCallBlock(tool_use_id="tu_1", name="Read")
        assert tc.status == ToolStatus.PENDING
        assert tc.started_at is None
        assert tc.completed_at is None
        assert tc.result is None
        assert not tc.is_terminal

    def test_lifecycle(self):
        tc = ToolCallBlock(tool_use_id="tu_1", name="Read")
        tc.status = ToolStatus.EXECUTING
        tc.started_at = 1000
        assert not tc.is_terminal

        tc.status = ToolStatus.COMPLETED
        tc.completed_at = 2000
        assert tc.is_terminal
        assert tc.elapsed_ms == 1000

    def test_errored_is_terminal(self):
        tc = ToolCallBlock(tool_use_id="tu_1", name="Read", status=ToolStatus.ERRORED)
        assert tc.is_terminal

    def test_elapsed_without_start(self):
        tc = ToolCallBlock(tool_use_id="tu_1", name="Read")
        assert tc.elapsed_ms is None


class TestTurn:
    def test_basic(self):
        um = UserMessage(content="read file")
        am = AssistantMessage(content=[TextBlock(text="done")])
        turn = Turn(user_message=um, assistant_messages=[am])
        assert turn.final_text == "done"
        assert turn.all_tool_calls == []

    def test_tool_calls_aggregation(self):
        um = UserMessage(content="edit file")
        tc1 = ToolCallBlock(tool_use_id="tu_1", name="Read", input={"file_path": "a.py"})
        tc2 = ToolCallBlock(tool_use_id="tu_2", name="Edit", input={"file_path": "a.py"})
        am = AssistantMessage(content=[tc1, tc2])
        turn = Turn(user_message=um, assistant_messages=[am])
        assert len(turn.all_tool_calls) == 2

    def test_final_text_skips_tool_call_messages(self):
        um = UserMessage(content="do it")
        am1 = AssistantMessage(content=[ToolCallBlock(tool_use_id="tu_1", name="Read")])
        am2 = AssistantMessage(content=[TextBlock(text="all done")])
        turn = Turn(user_message=um, assistant_messages=[am1, am2])
        assert turn.final_text == "all done"
