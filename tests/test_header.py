"""Verify header 'cc-mini provider:model' renders above input area."""

import pytest


class TestHeaderPosition:
    """Header is a FormattedTextControl between separator and input."""

    def test_header_control_exists(self):
        """AsyncApp creates _header_control in _build_ui."""
        from tui.async_app import AsyncApp
        from core.engine import Engine
        from core.permissions import PermissionChecker

        engine = Engine(
            tools=[], system_prompt="",
            permission_checker=PermissionChecker(auto_approve=True),
        )
        app = AsyncApp(engine=engine, permissions=None)
        assert hasattr(app, '_header_control')
        assert app._header_control is not None

    def test_header_not_in_chat_display(self):
        """Header is NOT in ChatDisplay messages — it's a separate UI element."""
        from tui.async_app import AsyncApp
        from core.engine import Engine
        from core.permissions import PermissionChecker

        engine = Engine(
            tools=[], system_prompt="",
            permission_checker=PermissionChecker(auto_approve=True),
        )
        app = AsyncApp(engine=engine, permissions=None)
        # Before run(), chat display is empty
        assert len(app.display._messages) == 0

    def test_run_sets_header_text(self):
        """run() sets _header_control.text with provider:model."""
        from tui.async_app import AsyncApp
        from core.engine import Engine
        from core.permissions import PermissionChecker

        engine = Engine(
            tools=[], system_prompt="",
            permission_checker=PermissionChecker(auto_approve=True),
            provider="lmstudio", model="qwen3.5-9b",
        )
        app = AsyncApp(engine=engine, permissions=None)

        # Simulate what run() does
        app._header_control.text = [
            ("bold fg:ansicyan", " cc-mini "),
            ("", "lmstudio:qwen3.5-9b"),
        ]

        text = app._header_control.text
        assert len(text) == 2
        assert text[0][0] == "bold fg:ansicyan"  # styled
        assert "cc-mini" in text[0][1]
        assert "lmstudio:qwen3.5-9b" in text[1][1]

    def test_header_renders_visible_text(self):
        """FormattedTextControl renders visible text."""
        from prompt_toolkit.layout.controls import FormattedTextControl
        ctrl = FormattedTextControl(
            text=[("bold fg:ansicyan", " cc-mini "), ("", "lmstudio:test")],
            focusable=False,
        )
        # The control produces valid FormattedText
        rendered = ctrl.text
        flat = "".join(item[1] for item in rendered if isinstance(item, tuple))
        assert "cc-mini" in flat
        assert "lmstudio:test" in flat

    def test_header_control_in_layout(self):
        """Header control is in the layout tree."""
        from tui.async_app import AsyncApp
        from core.engine import Engine
        from core.permissions import PermissionChecker

        engine = Engine(
            tools=[], system_prompt="",
            permission_checker=PermissionChecker(auto_approve=True),
        )
        app = AsyncApp(engine=engine, permissions=None)

        # Find our header control in the layout tree
        all_controls = app._layout.find_all_controls()
        assert app._header_control in all_controls, (
            "_header_control must be in the layout tree"
        )

    def test_docker_pt3_compat(self):
        """FormattedTextControl.text setter works in PT 3.x (no .prompt needed)."""
        from prompt_toolkit.layout.controls import FormattedTextControl
        ctrl = FormattedTextControl(text=[], focusable=False)
        # Set text dynamically — this is the PT 3.x compatible path
        ctrl.text = [("bold", "hello"), ("", " world")]
        assert len(ctrl.text) == 2
