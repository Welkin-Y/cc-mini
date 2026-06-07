"""Tests for ToolExecutor — lifecycle tracking and event emission."""
from __future__ import annotations

import pytest

from session.models import ToolCallBlock, ToolResult, ToolStatus
from session.session import Session
from session.tool_executor import ToolExecutor


class TestToolExecutor:
    def test_execute_known_tool(self):
        s = Session()
        s.add_user_message("run echo")
        tc = ToolCallBlock(tool_use_id="tu_1", name="Echo", input={"message": "hello"})
        s.add_assistant_message([tc])

        def echo_fn(**kwargs):
            return ToolResult(tool_use_id=kwargs.get("tool_use_id", ""),
                              content=f"echo: {kwargs.get('message', '')}")

        executor = ToolExecutor({"Echo": echo_fn}, s)
        result = executor.execute(tc)

        assert result.content == "echo: hello"
        assert not result.is_error
        assert tc.status == ToolStatus.COMPLETED
        assert tc.started_at is not None
        assert tc.completed_at is not None

    def test_execute_unknown_tool(self):
        s = Session()
        s.add_user_message("run unknown")
        tc = ToolCallBlock(tool_use_id="tu_1", name="NoSuchTool", input={})
        s.add_assistant_message([tc])

        executor = ToolExecutor({}, s)
        result = executor.execute(tc)
        assert result.is_error
        assert "Unknown tool" in result.content
        assert tc.status == ToolStatus.ERRORED

    def test_execute_tool_that_raises(self):
        s = Session()
        s.add_user_message("run")
        tc = ToolCallBlock(tool_use_id="tu_1", name="Crash", input={})
        s.add_assistant_message([tc])

        def crash_fn(**kwargs):
            raise ValueError("something went wrong")

        executor = ToolExecutor({"Crash": crash_fn}, s)
        result = executor.execute(tc)
        assert result.is_error
        assert "something went wrong" in result.content
        assert tc.status == ToolStatus.ERRORED

    def test_execute_events_emitted(self):
        s = Session()
        s.add_user_message("run echo")
        tc = ToolCallBlock(tool_use_id="tu_1", name="Echo", input={"message": "x"})
        s.add_assistant_message([tc])

        started = []
        completed = []
        s.events.on("tool_call_started", lambda sess, block: started.append(block))
        s.events.on("tool_call_completed", lambda sess, block: completed.append(block))

        executor = ToolExecutor({"Echo": lambda **kw: ToolResult(tool_use_id="tu_1", content="ok")}, s)
        executor.execute(tc)

        assert len(started) == 1
        assert len(completed) == 1
        assert started[0].tool_use_id == "tu_1"

    def test_execute_batch_sequential(self):
        s = Session()
        s.add_user_message("run two")
        tc1 = ToolCallBlock(tool_use_id="tu_1", name="Echo", input={"message": "a"})
        tc2 = ToolCallBlock(tool_use_id="tu_2", name="Echo", input={"message": "b"})
        s.add_assistant_message([tc1, tc2])

        executor = ToolExecutor(
            {"Echo": lambda **kw: ToolResult(tool_use_id=kw.get("tool_use_id", ""),
                                              content=f"echo: {kw.get('message', '')}")},
            s,
        )
        results = executor.execute_batch([tc1, tc2])
        assert len(results) == 2
        assert results[0].content == "echo: a"
        assert results[1].content == "echo: b"

    def test_execute_batch_parallel(self):
        s = Session()
        s.add_user_message("run parallel")
        tcs = [
            ToolCallBlock(tool_use_id=f"tu_{i}", name="Slow", input={"delay": 0.01})
            for i in range(3)
        ]
        s.add_assistant_message(tcs)

        def slow_fn(**kw):
            import time
            time.sleep(kw.get("delay", 0.01))
            return ToolResult(tool_use_id=kw.get("tool_use_id", ""), content="done")

        executor = ToolExecutor({"Slow": slow_fn}, s)
        results = executor.execute_batch(tcs, parallel=True)
        assert len(results) == 3
        for tc in tcs:
            assert tc.status == ToolStatus.COMPLETED

    def test_register_tool(self):
        s = Session()
        executor = ToolExecutor({}, s)
        executor.register("NewTool", lambda **kw: ToolResult(tool_use_id="x", content="new"))
        assert "NewTool" in executor._tools
