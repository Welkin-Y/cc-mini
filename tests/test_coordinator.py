import time

from features.coordinator import (
    current_session_mode,
    get_coordinator_system_prompt,
    get_coordinator_user_context,
    is_coordinator_mode,
    match_session_mode,
)
from types import SimpleNamespace
from unittest.mock import MagicMock


def test_is_coordinator_mode_reads_env(monkeypatch):
    monkeypatch.delenv("CC_MINI_COORDINATOR", raising=False)
    assert is_coordinator_mode() is False

    monkeypatch.setenv("CC_MINI_COORDINATOR", "1")
    assert is_coordinator_mode() is True
    assert current_session_mode() == "coordinator"


def test_match_session_mode_switches_env(monkeypatch):
    monkeypatch.delenv("CC_MINI_COORDINATOR", raising=False)

    warning = match_session_mode("coordinator")

    assert warning == "Entered coordinator mode to match resumed session."
    assert is_coordinator_mode() is True


def test_get_coordinator_user_context_hidden_when_disabled(monkeypatch):
    monkeypatch.delenv("CC_MINI_COORDINATOR", raising=False)
    assert get_coordinator_user_context(["Read", "Bash"]) == {}


def test_get_coordinator_user_context_lists_worker_tools(monkeypatch):
    monkeypatch.setenv("CC_MINI_COORDINATOR", "1")

    context = get_coordinator_user_context(["Read", "Bash"])

    assert "workerToolsContext" in context
    assert "Bash, Read" in context["workerToolsContext"]


def test_coordinator_system_prompt_mentions_task_notifications():
    prompt = get_coordinator_system_prompt()
    assert "task-notification" in prompt
    assert "Agent" in prompt
    assert "SendMessage" in prompt


def test_coordinator_worker_engine_inherits_langchain_fallback(monkeypatch):
    from tui import app as tui_app

    captured = {}
    engine_calls = []

    class _FakeEngine:
        def __init__(self, *args, **kwargs):
            engine_calls.append(kwargs)
            self._kwargs = kwargs
            self._client = MagicMock()

        def get_model(self):
            return self._kwargs.get("model", "local-model")

        def set_tools(self, _tools):
            pass

        @property
        def system_prompt(self):
            return self._kwargs.get("system_prompt", "")

        @system_prompt.setter
        def system_prompt(self, _value):
            pass

    class _FakeWorkerManager:
        def __init__(self, build_worker_engine):
            captured["worker_engine"] = build_worker_engine()

        def has_running_tasks(self):
            return False

        def drain_notifications(self):
            return []

        def get_running_status(self):
            return []

    monkeypatch.setattr(
        tui_app,
        "load_app_config",
        lambda _args: SimpleNamespace(
            provider="lmstudio",
            api_key="lm-studio",
            base_url="http://localhost:1234/v1",
            model="local-model",
            max_tokens=2048,
            effort=None,
            memory_dir=None,
            config_paths=(),
            auto_dream=False,
            dream_interval_hours=24.0,
            dream_min_sessions=5,
        ),
    )
    monkeypatch.setattr(tui_app, "Engine", _FakeEngine)
    monkeypatch.setattr(tui_app, "WorkerManager", _FakeWorkerManager)
    monkeypatch.setattr(tui_app, "run_query", lambda *args, **kwargs: None)
    monkeypatch.setattr(tui_app, "parse_input", lambda text: text)
    monkeypatch.setattr(tui_app, "ensure_memory_dir", lambda _path: None)
    monkeypatch.setattr(tui_app, "register_bundled_skills", lambda: None)
    monkeypatch.setattr(tui_app, "discover_skills", lambda _cwd: None)
    monkeypatch.setattr(tui_app, "build_skills_prompt_section", lambda: "")
    monkeypatch.setattr(tui_app, "build_system_prompt", lambda **_kwargs: "prompt")
    monkeypatch.setattr(tui_app, "load_sandbox_config", lambda _paths: {})
    monkeypatch.setattr(tui_app, "SandboxManager", lambda config=None: MagicMock())
    monkeypatch.setattr(tui_app, "LLMClient", MagicMock())
    monkeypatch.setattr(tui_app, "set_coordinator_mode", lambda enabled: None)
    monkeypatch.setattr(tui_app, "is_coordinator_mode", lambda: True)
    monkeypatch.setattr(tui_app, "get_coordinator_user_context", lambda _tools: {})
    monkeypatch.setattr(tui_app, "get_coordinator_system_prompt", lambda: "coord")
    monkeypatch.setattr(tui_app, "get_worker_system_prompt", lambda: "worker")
    monkeypatch.setattr(tui_app, "CostTracker", lambda: SimpleNamespace(total_cost_usd=0, format_cost=lambda: ""))
    monkeypatch.setattr(tui_app, "CompactService", lambda **_kwargs: MagicMock())
    monkeypatch.setattr(tui_app, "SessionStore", MagicMock())
    monkeypatch.setattr(tui_app.sys, "argv", ["cc-mini", "--print", "--coordinator", "--langchain-fallback", "hello"])

    tui_app.main()

    worker_call = engine_calls[0]
    main_call = engine_calls[1]

    assert worker_call["allow_langchain_fallback"] is True
    assert worker_call["debug_langchain_fallback"] is False
    assert worker_call["coordinator_mode"] is True
    assert main_call["allow_langchain_fallback"] is True
    assert main_call["coordinator_mode"] is True


def test_coordinator_worker_failure_notification_uses_failed_status():
    from features.worker_manager import WorkerManager
    from features.langchain_fallback import LangChainFallbackUnavailable

    class _FailingEngine:
        def submit(self, _prompt):
            raise LangChainFallbackUnavailable(
                "LangChain fallback exhausted in coordinator mode after hitting the iteration limit twice."
            )

        def abort(self):
            pass

    manager = WorkerManager(build_worker_engine=lambda: _FailingEngine())
    manager.spawn(description="Inspect", prompt="do the task")

    deadline = time.time() + 1.0
    notification = ""
    while time.time() < deadline:
        notifications = manager.drain_notifications()
        if notifications:
            notification = notifications[0]
            break
        time.sleep(0.01)

    assert notification
    assert "<status>failed</status>" in notification
    assert "LangChain fallback exhausted in coordinator mode" in notification
    assert 'Agent "Inspect" failed:' in notification
