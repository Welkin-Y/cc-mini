from types import SimpleNamespace
from unittest.mock import MagicMock

from rich.console import Console

from commands import CommandContext, _cmd_cost, _cmd_model


def _ctx(provider: str = "lmstudio") -> CommandContext:
    engine = MagicMock()
    engine.get_model.return_value = "local-model"
    engine.set_model = MagicMock()
    engine.list_available_models.return_value = []
    tracker = MagicMock()
    tracker.format_cost.return_value = "No API usage recorded."
    compact_service = MagicMock()
    on_model_change = MagicMock()
    session_store = MagicMock()
    return CommandContext(
        engine=engine,
        session_store=session_store,
        compact_service=compact_service,
        console=Console(record=True),
        app_config=SimpleNamespace(provider=provider, model_list=()),
        cost_tracker=tracker,
        on_model_change=on_model_change,
    )


def test_cost_command_mentions_lmstudio_pricing_note():
    ctx = _ctx("lmstudio")
    _cmd_cost(ctx, "")
    output = ctx.console.export_text()
    assert "LM Studio backend" in output
    assert "No API usage recorded." in output


def test_model_command_guides_lmstudio_switching(monkeypatch):
    ctx = _ctx("lmstudio")
    monkeypatch.setattr("commands._pick_simple_model", lambda *args, **kwargs: None)
    _cmd_model(ctx, "")
    output = ctx.console.export_text()
    assert "Kept model as local-model" in output


def test_model_command_updates_compact_and_session_state():
    ctx = _ctx("lmstudio")
    ctx.engine.get_model.return_value = "qwen-local"
    _cmd_model(ctx, "qwen-local")
    ctx.compact_service.set_model.assert_called_once_with("qwen-local")
    assert ctx.session_store.model == "qwen-local"
    ctx.on_model_change.assert_called_once_with("qwen-local")


def test_model_command_uses_discovered_lmstudio_models(monkeypatch):
    ctx = _ctx("lmstudio")
    ctx.engine.list_available_models.return_value = ["qwen-local", "llama-local"]
    ctx.engine.get_model.side_effect = ["local-model", "qwen-local"]

    monkeypatch.setattr("commands._pick_simple_model", lambda *args, **kwargs: "qwen-local")

    _cmd_model(ctx, "")

    ctx.engine.list_available_models.assert_called_once_with()
    ctx.engine.set_model.assert_called_once_with("qwen-local")
    ctx.compact_service.set_model.assert_called_once_with("qwen-local")


def test_model_command_falls_back_to_configured_models(monkeypatch):
    ctx = _ctx("lmstudio")
    ctx.app_config.model_list = ("qwen-local", "llama-local")
    ctx.engine.get_model.side_effect = ["local-model", "llama-local"]

    monkeypatch.setattr("commands._pick_simple_model", lambda *args, **kwargs: "llama-local")

    _cmd_model(ctx, "")

    ctx.engine.set_model.assert_called_once_with("llama-local")
