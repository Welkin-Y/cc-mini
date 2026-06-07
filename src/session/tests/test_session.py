"""Tests for the Session class — message management, turns, stats, events."""
from __future__ import annotations

import pytest

from session.models import (
    AssistantMessage,
    TextBlock,
    ToolCallBlock,
    ToolResult,
    ToolStatus,
    UserMessage,
)
from session.session import Session


class TestMessageManagement:
    def test_add_user_message(self):
        s = Session()
        msg = s.add_user_message("hello")
        assert isinstance(msg, UserMessage)
        assert msg.text == "hello"
        assert len(s._messages) == 1

    def test_add_assistant_message_text(self):
        s = Session()
        s.add_user_message("hi")
        msg = s.add_assistant_message([TextBlock(text="hello back")])
        assert isinstance(msg, AssistantMessage)
        assert msg.text == "hello back"
        assert len(s._messages) == 2

    def test_add_assistant_message_with_tool_calls(self):
        s = Session()
        s.add_user_message("read file")
        tc = ToolCallBlock(tool_use_id="tu_1", name="Read", input={"file_path": "x.py"})
        msg = s.add_assistant_message([TextBlock(text="ok"), tc])
        assert msg.has_tool_calls
        # Tool call should be indexed
        assert s.get_tool_call("tu_1") is tc

    def test_add_tool_result_links_to_block(self):
        s = Session()
        s.add_user_message("read file")
        tc = ToolCallBlock(tool_use_id="tu_1", name="Read")
        s.add_assistant_message([tc])
        result = s.add_tool_result("tu_1", "file contents", is_error=False)
        assert result.content == "file contents"
        assert tc.result is result

    def test_update_tool_call_lifecycle(self):
        s = Session()
        s.add_user_message("read file")
        tc = ToolCallBlock(tool_use_id="tu_1", name="Read")
        s.add_assistant_message([tc])

        # Start
        block = s.start_tool_call("tu_1")
        assert block.status == ToolStatus.EXECUTING
        assert block.started_at is not None

        # Complete
        result = ToolResult(tool_use_id="tu_1", content="done")
        block = s.complete_tool_call("tu_1", result)
        assert block.status == ToolStatus.COMPLETED
        assert block.completed_at is not None
        assert block.result is result
        assert block.elapsed_ms is not None
        assert block.elapsed_ms >= 0

    def test_complete_with_error(self):
        s = Session()
        s.add_user_message("bad cmd")
        tc = ToolCallBlock(tool_use_id="tu_1", name="Bash")
        s.add_assistant_message([tc])
        result = ToolResult(tool_use_id="tu_1", content="command not found", is_error=True)
        block = s.complete_tool_call("tu_1", result)
        assert block.status == ToolStatus.ERRORED


class TestTurns:
    def test_single_turn(self):
        s = Session()
        s.add_user_message("hello")
        s.add_assistant_message([TextBlock(text="hi")])
        assert len(s.turns) == 1
        turn = s.turns[0]
        assert turn.user_message.text == "hello"
        assert turn.final_text == "hi"

    def test_multi_turn(self):
        s = Session()
        s.add_user_message("q1")
        s.add_assistant_message([TextBlock(text="a1")])
        s.add_user_message("q2")
        s.add_assistant_message([TextBlock(text="a2")])
        assert len(s.turns) == 2
        assert s.turns[0].final_text == "a1"
        assert s.turns[1].final_text == "a2"

    def test_turn_with_tool_loop(self):
        """A turn with multiple assistant messages (tool-call loop)."""
        s = Session()
        s.add_user_message("read and edit")
        tc = ToolCallBlock(tool_use_id="tu_1", name="Read", input={"file_path": "a.py"})
        s.add_assistant_message([tc])
        s.add_tool_result("tu_1", "content here")
        # Second assistant message (no tools)
        s.add_assistant_message([TextBlock(text="I read the file.")])
        assert len(s.turns) == 1
        turn = s.turns[0]
        assert len(turn.assistant_messages) == 2
        assert len(turn.all_tool_calls) == 1


class TestStats:
    def test_empty_session(self):
        s = Session()
        st = s.stats()
        assert st.total_turns == 0
        assert st.total_messages == 0
        assert st.total_tool_calls == 0

    def test_with_messages(self):
        s = Session()
        s.add_user_message("hi")
        s.add_assistant_message([TextBlock(text="hello")], usage={"input_tokens": 10, "output_tokens": 5})
        st = s.stats()
        assert st.total_turns == 1
        assert st.total_messages == 2
        assert st.total_input_tokens == 10
        assert st.total_output_tokens == 5

    def test_with_tool_calls(self):
        s = Session()
        s.add_user_message("read")
        tc = ToolCallBlock(tool_use_id="tu_1", name="Read")
        s.add_assistant_message([tc])
        s.start_tool_call("tu_1")
        s.complete_tool_call("tu_1", ToolResult(tool_use_id="tu_1", content="ok"))
        st = s.stats()
        assert st.total_tool_calls == 1
        assert st.tool_calls_completed == 1
        assert st.tool_calls_errored == 0

    def test_with_errored_tool(self):
        s = Session()
        s.add_user_message("bad")
        tc = ToolCallBlock(tool_use_id="tu_1", name="Bash")
        s.add_assistant_message([tc])
        s.complete_tool_call("tu_1", ToolResult(tool_use_id="tu_1", content="err", is_error=True))
        st = s.stats()
        assert st.tool_calls_errored == 1
        assert st.tool_calls_completed == 0


class TestEvents:
    def test_message_added_event(self):
        s = Session()
        events = []
        s.events.on("message_added", lambda session, msg: events.append(msg))
        s.add_user_message("hi")
        s.add_assistant_message([TextBlock(text="hello")])
        assert len(events) == 2
        assert isinstance(events[0], UserMessage)
        assert isinstance(events[1], AssistantMessage)

    def test_tool_call_events(self):
        s = Session()
        started = []
        completed = []
        s.events.on("tool_call_started", lambda session, tc: started.append(tc))
        s.events.on("tool_call_completed", lambda session, tc: completed.append(tc))

        s.add_user_message("read")
        tc = ToolCallBlock(tool_use_id="tu_1", name="Read")
        s.add_assistant_message([tc])
        s.start_tool_call("tu_1")
        s.complete_tool_call("tu_1", ToolResult(tool_use_id="tu_1", content="ok"))

        assert len(started) == 1
        assert started[0].tool_use_id == "tu_1"
        assert len(completed) == 1

    def test_turn_completed_event(self):
        s = Session()
        turns_done = []
        s.events.on("turn_completed", lambda session, turn: turns_done.append(turn))

        s.add_user_message("hi")
        s.add_assistant_message([TextBlock(text="hello")])  # no tools → turn done

        assert len(turns_done) == 1

    def test_event_error_isolation(self):
        """A broken callback should not prevent others from running."""
        s = Session()
        good = []
        def broken(session, msg):
            raise RuntimeError("boom")
        s.events.on("message_added", broken)
        s.events.on("message_added", lambda session, msg: good.append(msg))
        s.add_user_message("hi")
        assert len(good) == 1


class TestToDicts:
    def test_round_trip(self):
        """to_dicts() produces output compatible with Engine._messages format."""
        s = Session()
        s.add_user_message("hi")
        tc = ToolCallBlock(tool_use_id="tu_1", name="Read", input={"file_path": "x.py"})
        s.add_assistant_message([TextBlock(text="let me read"), tc])

        dicts = s.to_dicts()
        assert len(dicts) == 2
        assert dicts[0] == {"role": "user", "content": "hi"}
        assert dicts[1]["role"] == "assistant"
        blocks = dicts[1]["content"]
        assert blocks[0] == {"type": "text", "text": "let me read"}
        assert blocks[1] == {"type": "tool_use", "id": "tu_1", "name": "Read", "input": {"file_path": "x.py"}}

    def test_from_dicts(self):
        dicts = [
            {"role": "user", "content": "read x.py"},
            {"role": "assistant", "content": [
                {"type": "text", "text": "ok"},
                {"type": "tool_use", "id": "tu_1", "name": "Read", "input": {"file_path": "x.py"}},
            ]},
        ]
        s = Session.from_dicts(dicts)
        assert len(s.turns) == 1
        turn = s.turns[0]
        assert turn.user_message.text == "read x.py"
        assert len(turn.all_tool_calls) == 1
        assert turn.all_tool_calls[0].name == "Read"


class TestLifecycle:
    def test_clear(self):
        s = Session()
        s.add_user_message("hi")
        s.add_assistant_message([TextBlock(text="hello")])
        assert len(s._messages) == 2

        cleared = []
        s.events.on("session_cleared", lambda session: cleared.append(True))
        s.clear()
        assert len(s._messages) == 0
        assert len(s.turns) == 0
        assert len(cleared) == 1

    def test_last_assistant_text(self):
        s = Session()
        assert s.last_assistant_text == ""

        s.add_user_message("hi")
        s.add_assistant_message([TextBlock(text="hello world")])
        assert s.last_assistant_text == "hello world"

    def test_last_message(self):
        s = Session()
        assert s.last_message is None
        s.add_user_message("hi")
        assert isinstance(s.last_message, UserMessage)


class TestWhiteboxEditing:
    """Tests for edit_message, delete_message, retry_tool_call, rollback_to, inject_correction."""

    def test_edit_user_message(self):
        s = Session()
        msg = s.add_user_message("original")
        assert msg.text == "original"
        ok = s.edit_message(msg.id, "corrected")
        assert ok
        assert s._messages[0].text == "corrected"

    def test_edit_assistant_message(self):
        s = Session()
        s.add_user_message("q")
        msg = s.add_assistant_message([TextBlock(text="wrong answer")])
        ok = s.edit_message(msg.id, "right answer")
        assert ok
        assert s.last_assistant_text == "right answer"

    def test_edit_nonexistent(self):
        s = Session()
        assert not s.edit_message("no-such-id", "whatever")

    def test_delete_user_message(self):
        s = Session()
        msg1 = s.add_user_message("q1")
        s.add_assistant_message([TextBlock(text="a1")])
        s.add_user_message("q2")
        s.add_assistant_message([TextBlock(text="a2")])
        assert len(s._messages) == 4
        n = s.delete_message(msg1.id)
        assert n == 1
        assert len(s._messages) == 3

    def test_delete_assistant_cleans_tool_calls(self):
        s = Session()
        s.add_user_message("run")
        tc = ToolCallBlock(tool_use_id="tu_1", name="Read")
        msg = s.add_assistant_message([tc])
        r = s.add_tool_result("tu_1", "result")
        assert s.get_tool_call("tu_1") is not None
        n = s.delete_message(msg.id)
        assert n == 1
        assert s.get_tool_call("tu_1") is None

    def test_retry_tool_call(self):
        s = Session()
        s.add_user_message("run")
        tc = ToolCallBlock(tool_use_id="tu_1", name="Read")
        s.add_assistant_message([tc])
        s.start_tool_call("tu_1")
        s.complete_tool_call("tu_1", ToolResult(tool_use_id="tu_1", content="bad", is_error=True))
        assert tc.status == ToolStatus.ERRORED

        ok = s.retry_tool_call("tu_1")
        assert ok
        assert tc.status == ToolStatus.PENDING
        assert tc.result is None
        assert tc.started_at is None
        assert tc.completed_at is None

    def test_retry_nonexistent(self):
        s = Session()
        assert not s.retry_tool_call("no-such")

    def test_rollback_to(self):
        s = Session()
        msg1 = s.add_user_message("q1")
        s.add_assistant_message([TextBlock(text="a1")])
        msg2 = s.add_user_message("q2")
        s.add_assistant_message([TextBlock(text="a2")])
        assert len(s._messages) == 4
        n = s.rollback_to(msg2.id)
        assert n == 2  # msg2 + its assistant response
        assert len(s._messages) == 2
        assert s._messages[0].id == msg1.id

    def test_rollback_nonexistent(self):
        s = Session()
        assert s.rollback_to("no-such") == 0

    def test_inject_correction(self):
        s = Session()
        s.add_user_message("q1")
        s.add_assistant_message([TextBlock(text="wrong")])
        msg = s.inject_correction("Actually, the answer is...")
        assert msg.text == "Actually, the answer is..."
        assert msg.meta.get("correction") is True
        assert len(s.turns) == 2  # new turn created
