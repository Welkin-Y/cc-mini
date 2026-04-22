from types import SimpleNamespace
from unittest.mock import patch

from tui.app import _resolve_initial_model


def test_resolve_initial_model_prefers_discovered_lmstudio_model():
    app_config = SimpleNamespace(
        provider="lmstudio",
        api_key="lm-studio",
        base_url="http://localhost:1234/v1",
        model="local-model",
        model_list=(),
    )

    with patch("tui.app.LLMClient") as mock_client:
        mock_client.return_value.list_models.return_value = ["qwen-local", "llama-local"]
        assert _resolve_initial_model(app_config) == "qwen-local"


def test_resolve_initial_model_keeps_configured_model_when_active():
    app_config = SimpleNamespace(
        provider="lmstudio",
        api_key="lm-studio",
        base_url="http://localhost:1234/v1",
        model="llama-local",
        model_list=(),
    )

    with patch("tui.app.LLMClient") as mock_client:
        mock_client.return_value.list_models.return_value = ["qwen-local", "llama-local"]
        assert _resolve_initial_model(app_config) == "llama-local"


def test_resolve_initial_model_falls_back_to_configured_model_list():
    app_config = SimpleNamespace(
        provider="lmstudio",
        api_key="lm-studio",
        base_url="http://localhost:1234/v1",
        model="local-model",
        model_list=("qwen-local", "llama-local"),
    )

    with patch("tui.app.LLMClient") as mock_client:
        mock_client.return_value.list_models.return_value = []
        assert _resolve_initial_model(app_config) == "qwen-local"
