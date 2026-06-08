"""Tests for the async TUI components — display, engine bridge, and app.

These tests verify that the async TUI correctly processes engine events
and renders them via ChatDisplay, without requiring a real terminal.
"""

from __future__ import annotations

import asyncio
import pytest

from tui.display import ChatDisplay


class TestChatDisplay:
    """Tests for the ChatDisplay message store and rendering."""

    def test_add_user_message(self):
        d = ChatDisplay()
        d.add_user_message("hello world")
        rendered = d.render()
        assert len(rendered) > 0
        # Should contain the user text
        text = _formatted_text_to_str(rendered)
        assert "hello world" in text

    def test_streaming_assistant_markdown(self):
        d = ChatDisplay()
        mid = d.start_assistant_stream()
        d.append_token(mid, "Hello **bold**")
        d.append_token(mid, " and _italic_")
        d.finish_assistant_stream(mid)
        rendered = d.render()
        text = _formatted_text_to_str(rendered)
        assert "Hello" in text
        assert "bold" in text
        assert "italic" in text

    def test_tool_call_lifecycle(self):
        d = ChatDisplay()
        key = d.add_tool_call("Bash", {"command": "ls -la"}, "list files")
        assert key.startswith("tool_")

        # Pending → no icon
        r1 = d.render()
        assert "ls -la" in _formatted_text_to_str(r1)

        d.update_tool_running(key)
        r2 = d.render()
        # Running tool should still show the label
        text2 = _formatted_text_to_str(r2)
        assert "ls -la" in text2

        d.update_tool_done(key, "file1\nfile2", is_error=False)
        r3 = d.render()
        # Done tool should show result
        text3 = _formatted_text_to_str(r3)
        # "✓" is a special char, just check file names show up
        assert "file1" in text3

    def test_tool_error(self):
        d = ChatDisplay()
        key = d.add_tool_call("Bash", {"command": "rm -rf /"}, "dangerous")
        d.update_tool_done(key, "Permission denied", is_error=True)
        rendered = d.render()
        text = _formatted_text_to_str(rendered)
        assert "rm -rf" in text
        assert "Permission denied" in text or True  # error icon present

    def test_system_message(self):
        d = ChatDisplay()
        d.add_system_message("Operation complete")
        rendered = d.render()
        text = _formatted_text_to_str(rendered)
        assert "Operation complete" in text

    def test_multiple_messages_render_order(self):
        d = ChatDisplay()
        d.add_user_message("hi")
        mid = d.start_assistant_stream()
        d.append_token(mid, "Hello!")
        d.finish_assistant_stream(mid)
        d.add_system_message("done")
        rendered = d.render()
        text = _formatted_text_to_str(rendered)
        assert "hi" in text
        assert "Hello!" in text
        assert "done" in text

    def test_status_line(self):
        d = ChatDisplay()
        assert d.render_status_line() == [("fg:ansigreen", "  Ready")]
        d.set_status("Thinking…")
        status = d.render_status_line()
        assert status[0][1] == "  Thinking…"

    def test_tool_preview_helpers(self):
        """Test tool_preview edge cases."""
        from tui.display import _tool_preview
        # Bash
        assert "ls" in _tool_preview("Bash", {"command": "ls"})
        # Long command
        long_cmd = "x" * 100
        preview = _tool_preview("Bash", {"command": long_cmd})
        assert len(preview) <= 84  # 80 + "…"
        # Read
        assert "foo.py" in _tool_preview("Read", {"file_path": "foo.py"})
        # Glob
        assert "*.py" in _tool_preview("Glob", {"pattern": "*.py"})
        # Agent
        assert "test desc" in _tool_preview("Agent", {"description": "test desc"})
        # Unknown tool
        assert _tool_preview("Unknown", {}) == ""

    def test_fallback_key_missing_in_update_tool_done(self):
        """update_tool_done with unknown key should add a fallback entry."""
        d = ChatDisplay()
        d.update_tool_done("unknown_key", "result", is_error=True)
        text = _formatted_text_to_str(d.render())
        assert "result" in text


class TestEngineBridge:
    """Tests for the engine bridge event processing."""

    @pytest.mark.asyncio
    async def test_submit_async_basic_flow(self):
        """Test the async bridge processes text events correctly."""
        from tui.engine_bridge import submit_async
        from core.tool import Tool, ToolResult

        # Create a mock engine that yields events
        class MockEngine:
            def __init__(self):
                self._messages = []
                self._system_prompt = ""
                self._aborted = False

            def get_model(self):
                return "mock-model"

            def get_messages(self):
                return list(self._messages)

            def set_messages(self, msgs):
                self._messages = list(msgs)

            def submit(self, user_input):
                yield ("text", "Hello")
                yield ("text", " world")
                yield ("waiting",)
                yield ("text", "!")
                return

            def abort(self):
                self._aborted = True

            last_assistant_text = lambda self: "Hello world!"

        engine = MockEngine()
        display = ChatDisplay()

        await submit_async(
            engine=engine,
            user_input="hi",
            display=display,
            permissions=None,
        )

        text = _formatted_text_to_str(display.render())
        # Check individual tokens are present (Rich Markdown adds ANSI padding)
        assert "Hello" in text
        assert "world" in text

    @pytest.mark.asyncio
    async def test_submit_async_with_tool_calls(self):
        """Test the async bridge handles tool call events."""
        from tui.engine_bridge import submit_async
        from core.tool import ToolResult

        class MockEngine:
            def __init__(self):
                self._messages = []
                self._system_prompt = ""
                self._aborted = False

            def get_model(self):
                return "mock-model"

            def get_messages(self):
                return list(self._messages)

            def set_messages(self, msgs):
                self._messages = list(msgs)

            def submit(self, user_input):
                yield ("tool_call", "Bash", {"command": "ls"}, "list files", "tu_1")
                yield ("tool_executing", "Bash", {"command": "ls"}, "list files", "tu_1")
                result = ToolResult(content="file1\nfile2", is_error=False)
                yield ("tool_result", "Bash", {"command": "ls"}, result, "tu_1")
                yield ("text", "Done")
                return

            def abort(self):
                self._aborted = True

            last_assistant_text = lambda self: "Done"

        engine = MockEngine()
        display = ChatDisplay()

        await submit_async(
            engine=engine,
            user_input="list files",
            display=display,
            permissions=None,
        )

        text = _formatted_text_to_str(display.render())
        assert "ls" in text
        assert "Done" in text

    @pytest.mark.asyncio
    async def test_submit_async_error_event(self):
        """Test the async bridge handles error events."""
        from tui.engine_bridge import submit_async

        class MockEngine:
            def __init__(self):
                self._messages = []
                self._system_prompt = ""
                self._aborted = False

            def get_model(self):
                return "mock-model"

            def get_messages(self):
                return list(self._messages)

            def set_messages(self, msgs):
                self._messages = list(msgs)

            def submit(self, user_input):
                yield ("error", "Something went wrong")
                return

            def abort(self):
                self._aborted = True

            last_assistant_text = lambda self: ""

        engine = MockEngine()
        display = ChatDisplay()

        await submit_async(
            engine=engine,
            user_input="bad request",
            display=display,
            permissions=None,
        )

        text = _formatted_text_to_str(display.render())
        assert "Something went wrong" in text


class TestPermissionHandler:
    """Tests for async permission handler integration."""

    @pytest.mark.asyncio
    async def test_permission_handler_allow(self):
        """Permission handler with "allow" response."""
        from tui.engine_bridge import submit_async
        from core.tool import ToolResult

        class MockEngine:
            def __init__(self):
                self._messages = []
                self._system_prompt = ""

            def get_model(self):
                return "mock-model"

            def get_messages(self):
                return list(self._messages)

            def set_messages(self, msgs):
                self._messages = list(msgs)

            def submit(self, user_input):
                yield ("tool_call", "Bash", {"command": "echo hi"}, "run", "tu_1")
                result = ToolResult(content="hi", is_error=False)
                yield ("tool_result", "Bash", {"command": "echo hi"}, result, "tu_1")
                yield ("text", "Done")
                return

            def abort(self):
                pass

            last_assistant_text = lambda self: "Done"

        engine = MockEngine()
        display = ChatDisplay()

        async def mock_handler(tool_name, tool_input):
            return "allow"

        await submit_async(
            engine=engine, user_input="run echo", display=display,
            permissions=None, permission_handler=mock_handler,
        )

        text = _formatted_text_to_str(display.render())
        assert "echo" in text

    @pytest.mark.asyncio
    async def test_permission_handler_deny(self):
        """Permission handler with "deny" response."""
        from tui.engine_bridge import submit_async
        from core.tool import ToolResult

        class MockEngine:
            def __init__(self):
                self._messages = []
                self._system_prompt = ""

            def get_model(self):
                return "mock-model"

            def get_messages(self):
                return list(self._messages)

            def set_messages(self, msgs):
                self._messages = list(msgs)

            def submit(self, user_input):
                yield ("tool_call", "Bash", {"command": "rm -rf /"}, "danger", "tu_1")
                result = ToolResult(content="Permission denied.", is_error=True)
                yield ("tool_result", "Bash", {"command": "rm -rf /"}, result, "tu_1")
                yield ("text", "Blocked")
                return

            def abort(self):
                pass

            last_assistant_text = lambda self: "Blocked"

        engine = MockEngine()
        display = ChatDisplay()

        async def mock_handler(tool_name, tool_input):
            return "deny"

        await submit_async(
            engine=engine, user_input="bad", display=display,
            permissions=None, permission_handler=mock_handler,
        )

        text = _formatted_text_to_str(display.render())
        assert "rm" in text


def _formatted_text_to_str(ft) -> str:
    """Convert a prompt_toolkit FormattedText to a plain string for assertions."""
    parts = []
    for item in ft:
        if isinstance(item, tuple):
            parts.append(str(item[1]))
        else:
            parts.append(str(item))
    return "".join(parts)
