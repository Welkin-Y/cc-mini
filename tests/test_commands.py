from io import StringIO
from unittest.mock import MagicMock

from rich.console import Console

from core.commands import CommandContext, _cmd_model
from core.engine import Engine
from core.llm import LLMModel
from core.permissions import PermissionChecker
from core.tools.base import Tool, ToolResult


class DummyTool(Tool):
    name = "Dummy"
    description = "A dummy tool for testing"
    input_schema = {
        "type": "object",
        "properties": {"msg": {"type": "string"}},
        "required": ["msg"],
    }

    def execute(self, msg: str) -> ToolResult:
        return ToolResult(content=f"got: {msg}")


def _make_engine(provider: str = "lmstudio", model: str = "model-b"):
    return Engine(
        tools=[DummyTool()],
        system_prompt="test",
        permission_checker=PermissionChecker(auto_approve=True),
        provider=provider,
        model=model,
    )


def _make_ctx(engine: Engine, console: Console):
    app_config = MagicMock()
    app_config.provider = engine._provider
    return CommandContext(
        engine=engine,
        session_store=None,
        compact_service=MagicMock(),
        console=console,
        app_config=app_config,
    )


def test_model_lists_lmstudio_models():
    engine = _make_engine()
    engine._client.list_models = MagicMock(return_value=[
        LLMModel(id="model-a"),
        LLMModel(id="model-b"),
    ])
    output = StringIO()
    console = Console(file=output, force_terminal=False, color_system=None)

    _cmd_model(_make_ctx(engine, console), "")

    rendered = output.getvalue()
    assert "LM Studio Models" in rendered
    assert "model-a" in rendered
    assert "model-b" in rendered
    assert "active" in rendered
    assert "/model <number> or /model <id>" in rendered


def test_model_switches_lmstudio_model_by_index():
    engine = _make_engine(model="model-a")
    engine._client.list_models = MagicMock(return_value=[
        LLMModel(id="model-a"),
        LLMModel(id="model-b"),
    ])
    output = StringIO()
    console = Console(file=output, force_terminal=False, color_system=None)

    _cmd_model(_make_ctx(engine, console), "2")

    assert engine.get_model() == "model-b"
    assert "Set model to model-b" in output.getvalue()


def test_model_rejects_invalid_lmstudio_index():
    engine = _make_engine(model="model-a")
    engine._client.list_models = MagicMock(return_value=[
        LLMModel(id="model-a"),
    ])
    output = StringIO()
    console = Console(file=output, force_terminal=False, color_system=None)

    _cmd_model(_make_ctx(engine, console), "9")

    assert engine.get_model() == "model-a"
    assert "Invalid model index 9" in output.getvalue()
