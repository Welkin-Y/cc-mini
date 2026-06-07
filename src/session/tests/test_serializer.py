"""Tests for session serialization — JSON round-trip."""
from __future__ import annotations

import json
import pytest

from session.models import TextBlock, ToolCallBlock, ToolResult
from session.session import Session
from session.serializer import session_to_dict, session_to_json, session_from_json


class TestSerialize:
    def test_empty_session(self):
        s = Session()
        d = session_to_dict(s)
        assert d["session_id"] == s.session_id
        assert d["turns"] == []
        assert d["stats"]["total_turns"] == 0

    def test_session_with_text(self):
        s = Session()
        s.add_user_message("hello")
        s.add_assistant_message([TextBlock(text="world")])

        d = session_to_dict(s)
        assert len(d["turns"]) == 1
        turn = d["turns"][0]
        assert turn["user_message"]["text"] == "hello"
        assert len(turn["assistant_messages"]) == 1
        am = turn["assistant_messages"][0]
        assert am["text"] == "world"
        assert am["blocks"][0]["type"] == "text"

    def test_session_with_tool_calls(self):
        s = Session()
        s.add_user_message("read file")
        tc = ToolCallBlock(tool_use_id="tu_1", name="Read", input={"file_path": "x.py"})
        s.add_assistant_message([tc])
        s.start_tool_call("tu_1")
        s.complete_tool_call("tu_1", ToolResult(tool_use_id="tu_1", content="file content"))

        d = session_to_dict(s)
        turn = d["turns"][0]
        am = turn["assistant_messages"][0]
        tc_dict = am["blocks"][0]
        assert tc_dict["type"] == "tool_call"
        assert tc_dict["tool_use_id"] == "tu_1"
        assert tc_dict["status"] == "completed"
        assert tc_dict["result"]["content"] == "file content"

    def test_json_output(self):
        s = Session()
        s.add_user_message("hi")
        s.add_assistant_message([TextBlock(text="hey")])
        j = session_to_json(s)
        assert isinstance(j, str)
        parsed = json.loads(j)
        assert parsed["session_id"] == s.session_id


class TestDeserialize:
    def test_round_trip_basic(self):
        s = Session(session_id="test123")
        s.add_user_message("hello")
        s.add_assistant_message([TextBlock(text="world")])

        j = session_to_json(s)
        s2 = session_from_json(j)
        assert s2.session_id == "test123"
        assert len(s2.turns) == 1
        assert s2.turns[0].user_message.text == "hello"
        assert s2.turns[0].final_text == "world"

    def test_round_trip_with_tool_calls(self):
        s = Session()
        s.add_user_message("read file")
        tc = ToolCallBlock(tool_use_id="tu_1", name="Read", input={"file_path": "x.py"})
        s.add_assistant_message([tc])
        s.start_tool_call("tu_1")
        s.complete_tool_call("tu_1", ToolResult(tool_use_id="tu_1", content="content"))

        j = session_to_json(s)
        s2 = session_from_json(j)
        assert len(s2.turns) == 1
        tcs = s2.turns[0].all_tool_calls
        assert len(tcs) == 1
        assert tcs[0].name == "Read"
        assert tcs[0].status == "completed"
        assert tcs[0].result.content == "content"

    def test_round_trip_from_dict(self):
        """Deserialize from a plain dict (not JSON string)."""
        d = session_to_dict(Session())
        s = session_from_json(d)
        assert isinstance(s, Session)

    def test_stats_preserved(self):
        s = Session()
        s.add_user_message("hi")
        s.add_assistant_message([TextBlock(text="yo")], usage={"input_tokens": 5, "output_tokens": 3})

        j = session_to_json(s)
        s2 = session_from_json(j)
        st = s2.stats()
        assert st.total_input_tokens == 5
        assert st.total_output_tokens == 3
