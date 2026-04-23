import json
import importlib.util

import httpx
import pytest

from core.tool import Tool, ToolResult
from features.langchain_fallback import (
    LangChainFallbackUnavailable,
    LangChainToolSpec,
    _ITERATION_LIMIT_MESSAGE,
    _accept_repeated_non_action_summary,
    _extract_fallback_final_answer,
    _parse_tool_input,
    _wrap_react_tool,
    _wrap_tool_invocation,
    run_langchain_agent,
    should_fallback_from_error_message,
)


def _require_langchain():
    if importlib.util.find_spec("langchain") is None:
        pytest.skip("langchain is not installed in this test environment")


class _FakeTool(Tool):
    name = "Write"
    description = "Writes a file using {templated} text."
    input_schema = {
        "type": "object",
        "properties": {
            "file_path": {"type": "string", "description": "Absolute path"},
            "content": {"type": "string", "description": "File contents"},
        },
        "required": ["file_path", "content"],
    }

    def execute(self, **kwargs) -> ToolResult:
        return ToolResult(content=str(kwargs))


def test_format_tool_description_avoids_raw_json_braces():
    tool = _FakeTool()
    description = tool.to_langchain_description()

    assert "{{templated}}" in description
    assert '"properties"' not in description
    assert "- file_path: string (required) - Absolute path" in description
    assert "- content: string (required) - File contents" in description


def test_build_args_schema_and_wrapper_support_multi_argument_tools():
    calls = []

    class _GlobTool(Tool):
        name = "Glob"
        description = "Glob files"
        input_schema = {
            "type": "object",
            "properties": {
                "pattern": {"type": "string", "description": "Glob pattern"},
                "path": {"type": "string", "description": "Search root"},
            },
            "required": ["pattern"],
        }

        def execute(self, **kwargs) -> ToolResult:
            return ToolResult(content=str(kwargs))

    tool = _GlobTool()
    spec = LangChainToolSpec(tool, lambda payload: calls.append(payload) or "ok")

    schema = tool.to_langchain_args_schema()
    parsed = schema(pattern="*", path="/workspace")
    result = _wrap_tool_invocation(spec)(**parsed.dict())

    assert result == "ok"
    assert calls == [{"pattern": "*", "path": "/workspace"}]


def test_should_fallback_from_error_message_matches_tool_call_errors():
    assert should_fallback_from_error_message("tool_calls not supported") is True
    assert should_fallback_from_error_message("Functions are not supported by this model") is True
    assert should_fallback_from_error_message("authentication failed") is False


def test_parse_tool_input_supports_json_and_single_required_string():
    class _GlobTool(Tool):
        name = "Glob"
        description = "Glob files"
        input_schema = {
            "type": "object",
            "properties": {
                "pattern": {"type": "string", "description": "Glob pattern"},
                "path": {"type": "string", "description": "Search root"},
            },
            "required": ["pattern"],
        }

        def execute(self, **kwargs) -> ToolResult:
            return ToolResult(content=str(kwargs))

    assert _parse_tool_input(_GlobTool(), '{"pattern":"*","path":"/workspace"}') == {
        "pattern": "*",
        "path": "/workspace",
    }
    assert _parse_tool_input(_GlobTool(), "*") == {"pattern": "*"}


def test_parse_tool_input_rejects_non_json_for_multi_required_tool():
    tool = _FakeTool()

    with pytest.raises(LangChainFallbackUnavailable, match="requires JSON object input"):
        _parse_tool_input(tool, "not-json")


def test_parse_tool_input_accepts_json_object_with_trailing_narration():
    tool = _FakeTool()

    raw = (
        '{"file_path":"/workspace/notebooks/minesweeper/main.py","content":"print(\\"hi\\")"}'
        "<channel|>The file was created successfully."
    )

    assert _parse_tool_input(tool, raw) == {
        "file_path": "/workspace/notebooks/minesweeper/main.py",
        "content": 'print("hi")',
    }


def test_extract_fallback_final_answer_accepts_json_and_plain_text():
    assert _extract_fallback_final_answer('{"type":"assistant","content":"done"}') == "done"
    assert _extract_fallback_final_answer('{"final_answer":"done"}') == "done"
    assert _extract_fallback_final_answer("Plain prose final reply") == "Plain prose final reply"
    assert _extract_fallback_final_answer("Plain prose final reply", allow_plain_text=False) is None
    assert _extract_fallback_final_answer("Thought: hmm\nAction: Glob\nAction Input: *") is None


def test_accept_repeated_non_action_summary_after_tool_use():
    retry_state = {"last": None, "count": 0}
    accepted = _accept_repeated_non_action_summary(
        "**Execution Summary:** The requested checks completed successfully and the environment is ready.",
        retry_state=retry_state,
        allow_repeat_accept=True,
    )
    assert accepted.startswith("**Execution Summary:**")


def test_wrap_react_tool_parses_json_before_invoking():
    class _GlobTool(Tool):
        name = "Glob"
        description = "Glob files"
        input_schema = {
            "type": "object",
            "properties": {
                "pattern": {"type": "string", "description": "Glob pattern"},
                "path": {"type": "string", "description": "Search root"},
            },
            "required": ["pattern"],
        }

        def execute(self, **kwargs) -> ToolResult:
            return ToolResult(content=str(kwargs))

    calls = []
    wrapped = _wrap_react_tool(LangChainToolSpec(_GlobTool(), lambda payload: calls.append(payload) or "ok"))

    assert wrapped('{"pattern":"*","path":"/workspace"}') == "ok"
    assert calls == [{"pattern": "*", "path": "/workspace"}]


def test_run_langchain_agent_uses_react_agent_over_raw_http(monkeypatch):
    _require_langchain()
    requests = []
    client_cls = httpx.Client

    class _GlobTool(Tool):
        name = "Glob"
        description = "Glob files"
        input_schema = {
            "type": "object",
            "properties": {
                "pattern": {"type": "string", "description": "Glob pattern"},
                "path": {"type": "string", "description": "Search root"},
            },
            "required": ["pattern"],
        }

        def execute(self, **kwargs) -> ToolResult:
            return ToolResult(content=str(kwargs))

    def _handler(request: httpx.Request) -> httpx.Response:
        requests.append({
            "url": str(request.url),
            "headers": dict(request.headers),
            "json": json.loads(request.content.decode("utf-8")),
        })
        payload = requests[-1]["json"]
        prompt = payload["messages"][0]["content"]
        assert request.url.path.endswith("/v1/chat/completions")
        assert payload["model"] == "local-model"
        assert payload["temperature"] == 0
        assert payload["stop"] == ["\nObservation"]
        assert "You have access to the following tools:" in prompt
        assert "Glob files" in prompt
        assert "Action: Glob" in prompt or "should be one of [Glob]" in prompt
        assert "Do not emit `Summary:`" in prompt
        assert "If you are done, emit `Final Answer:` immediately." in prompt

        if len(requests) == 1:
            return httpx.Response(
                200,
                json={
                    "choices": [{
                        "message": {
                            "content": (
                                "Thought: I should inspect files\n"
                                "Action: Glob\n"
                                "Action Input: {\"pattern\":\"*\",\"path\":\"/workspace\"}"
                            )
                        }
                    }]
                },
            )

        assert "Observation: called:*:/workspace" in prompt
        return httpx.Response(
            200,
            json={
                "choices": [{
                    "message": {
                        "content": (
                            "Thought: I have enough information\n"
                            "Final Answer: ok"
                        )
                    }
                }]
            },
        )

    transport = httpx.MockTransport(_handler)

    def _make_client(*args, **kwargs):
        return client_cls(transport=transport, **kwargs)

    monkeypatch.setattr("features.langchain_fallback.httpx.Client", _make_client)

    tool = _GlobTool()
    spec = LangChainToolSpec(tool, lambda payload: f"called:{payload['pattern']}:{payload.get('path', '')}")
    result = run_langchain_agent(
        model="local-model",
        api_key="lm-studio",
        base_url="http://localhost:1234/v1",
        system_prompt="You are helpful.",
        messages=[{"role": "user", "content": "find files"}],
        tool_specs=[spec],
    )

    assert result == "ok"
    assert len(requests) == 2
    assert requests[0]["headers"]["authorization"] == "Bearer lm-studio"


def test_run_langchain_agent_treats_initial_non_react_reply_as_final_answer(monkeypatch):
    _require_langchain()
    requests = []
    client_cls = httpx.Client
    transport = httpx.MockTransport(
        lambda request: _react_retry_handler(requests, request)
    )

    def _make_client(*args, **kwargs):
        return client_cls(transport=transport, **kwargs)

    monkeypatch.setattr("features.langchain_fallback.httpx.Client", _make_client)

    result = run_langchain_agent(
        model="local-model",
        api_key="lm-studio",
        base_url="http://localhost:1234/v1",
        system_prompt="You are helpful.",
        messages=[{"role": "user", "content": "summarize the repo"}],
        tool_specs=[],
    )

    assert result == "This is cc-mini, an ultra-light Python harness."
    assert len(requests) == 1


def test_run_langchain_agent_accepts_plain_text_final_reply(monkeypatch):
    _require_langchain()
    client_cls = httpx.Client
    transport = httpx.MockTransport(
        lambda request: httpx.Response(
            200,
            json={
                "choices": [{
                    "message": {
                        "content": "This is the final answer in plain prose."
                    }
                }]
            },
        )
    )

    def _make_client(*args, **kwargs):
        return client_cls(transport=transport, **kwargs)

    monkeypatch.setattr("features.langchain_fallback.httpx.Client", _make_client)

    result = run_langchain_agent(
        model="local-model",
        api_key="lm-studio",
        base_url="http://localhost:1234/v1",
        system_prompt="You are helpful.",
        messages=[{"role": "user", "content": "summarize the repo"}],
        tool_specs=[],
    )

    assert result == "This is the final answer in plain prose."


def test_run_langchain_agent_accepts_json_final_reply(monkeypatch):
    _require_langchain()
    client_cls = httpx.Client
    transport = httpx.MockTransport(
        lambda request: httpx.Response(
            200,
            json={
                "choices": [{
                    "message": {
                        "content": '{"type":"assistant","content":"done"}'
                    }
                }]
            },
        )
    )

    def _make_client(*args, **kwargs):
        return client_cls(transport=transport, **kwargs)

    monkeypatch.setattr("features.langchain_fallback.httpx.Client", _make_client)

    result = run_langchain_agent(
        model="local-model",
        api_key="lm-studio",
        base_url="http://localhost:1234/v1",
        system_prompt="You are helpful.",
        messages=[{"role": "user", "content": "summarize the repo"}],
        tool_specs=[],
    )

    assert result == "done"


def test_run_langchain_agent_debug_logs_model_outputs_and_final_acceptance(monkeypatch, capsys):
    _require_langchain()
    client_cls = httpx.Client
    transport = httpx.MockTransport(
        lambda request: httpx.Response(
            200,
            json={
                "choices": [{
                    "message": {
                        "content": '{"type":"assistant","content":"done"}'
                    }
                }]
            },
        )
    )

    def _make_client(*args, **kwargs):
        return client_cls(transport=transport, **kwargs)

    monkeypatch.setattr("features.langchain_fallback.httpx.Client", _make_client)

    result = run_langchain_agent(
        model="local-model",
        api_key="lm-studio",
        base_url="http://localhost:1234/v1",
        system_prompt="You are helpful.",
        messages=[{"role": "user", "content": "summarize the repo"}],
        tool_specs=[],
        debug=True,
    )

    captured = capsys.readouterr()
    assert result == "done"
    assert "[langchain-fallback] step=1 model_output=" in captured.err
    assert "[langchain-fallback] step=1 accepted_final=done" in captured.err


def test_run_langchain_agent_does_not_accept_plain_text_after_tool_use(monkeypatch):
    _require_langchain()
    requests = []
    client_cls = httpx.Client

    class _GlobTool(Tool):
        name = "Glob"
        description = "Glob files"
        input_schema = {
            "type": "object",
            "properties": {
                "pattern": {"type": "string", "description": "Glob pattern"},
            },
            "required": ["pattern"],
        }

        def execute(self, **kwargs) -> ToolResult:
            return ToolResult(content=str(kwargs))

    def _handler(request: httpx.Request) -> httpx.Response:
        requests.append(json.loads(request.content.decode("utf-8")))
        if len(requests) == 1:
            return httpx.Response(
                200,
                json={
                    "choices": [{
                        "message": {
                            "content": "Thought: inspect files\nAction: Glob\nAction Input: {\"pattern\":\"*\"}"
                        }
                    }]
                },
            )
        if len(requests) == 2:
            return httpx.Response(
                200,
                json={
                    "choices": [{
                        "message": {
                            "content": "The files are now known."
                        }
                    }]
                },
            )
        return httpx.Response(
            200,
            json={
                "choices": [{
                    "message": {
                        "content": "Thought: I can answer now\nFinal Answer: done"
                    }
                }]
            },
        )

    transport = httpx.MockTransport(_handler)

    def _make_client(*args, **kwargs):
        return client_cls(transport=transport, **kwargs)

    monkeypatch.setattr("features.langchain_fallback.httpx.Client", _make_client)

    result = run_langchain_agent(
        model="local-model",
        api_key="lm-studio",
        base_url="http://localhost:1234/v1",
        system_prompt="You are helpful.",
        messages=[{"role": "user", "content": "find files"}],
        tool_specs=[LangChainToolSpec(_GlobTool(), lambda payload: f"glob:{payload['pattern']}")],
    )

    assert result == "done"
    assert len(requests) == 3


def test_run_langchain_agent_accepts_repeated_summary_after_tool_use(monkeypatch):
    _require_langchain()
    requests = []
    client_cls = httpx.Client

    class _GlobTool(Tool):
        name = "Glob"
        description = "Glob files"
        input_schema = {
            "type": "object",
            "properties": {
                "pattern": {"type": "string", "description": "Glob pattern"},
            },
            "required": ["pattern"],
        }

        def execute(self, **kwargs) -> ToolResult:
            return ToolResult(content=str(kwargs))

    def _handler(request: httpx.Request) -> httpx.Response:
        requests.append(json.loads(request.content.decode("utf-8")))
        if len(requests) == 1:
            return httpx.Response(
                200,
                json={
                    "choices": [{
                        "message": {
                            "content": "Thought: inspect files\nAction: Glob\nAction Input: {\"pattern\":\"*\"}"
                        }
                    }]
                },
            )
        summary = "**Execution Summary:** The requested checks completed successfully and the environment is ready."
        return httpx.Response(
            200,
            json={"choices": [{"message": {"content": summary}}]},
        )

    transport = httpx.MockTransport(_handler)

    def _make_client(*args, **kwargs):
        return client_cls(transport=transport, **kwargs)

    monkeypatch.setattr("features.langchain_fallback.httpx.Client", _make_client)

    result = run_langchain_agent(
        model="local-model",
        api_key="lm-studio",
        base_url="http://localhost:1234/v1",
        system_prompt="You are helpful.",
        messages=[{"role": "user", "content": "run tests"}],
        tool_specs=[LangChainToolSpec(_GlobTool(), lambda payload: f"glob:{payload['pattern']}")],
    )

    assert result.startswith("**Execution Summary:**")
    assert len(requests) == 2


def test_run_langchain_agent_rejects_iteration_limit_text_in_coordinator_mode():
    calls = []

    def _fake_once(**kwargs):
        calls.append(kwargs)
        return _ITERATION_LIMIT_MESSAGE

    with pytest.raises(
        LangChainFallbackUnavailable,
        match="exhausted in coordinator mode",
    ):
        with pytest.MonkeyPatch.context() as mp:
            mp.setattr("features.langchain_fallback._run_langchain_agent_once", _fake_once)
            run_langchain_agent(
                model="local-model",
                api_key="lm-studio",
                base_url="http://localhost:1234/v1",
                system_prompt="You are helpful.",
                messages=[{"role": "user", "content": "summarize the repo"}],
                tool_specs=[],
                coordinator_mode=True,
            )

    assert len(calls) == 2
    assert calls[0]["retrying"] is False
    assert calls[1]["retrying"] is True


def test_run_langchain_agent_retries_once_and_returns_second_answer_in_coordinator_mode():
    calls = []

    def _fake_once(**kwargs):
        calls.append(kwargs)
        if len(calls) == 1:
            return _ITERATION_LIMIT_MESSAGE
        return "fixed final answer"

    with pytest.MonkeyPatch.context() as mp:
        mp.setattr("features.langchain_fallback._run_langchain_agent_once", _fake_once)
        result = run_langchain_agent(
            model="local-model",
            api_key="lm-studio",
            base_url="http://localhost:1234/v1",
            system_prompt="You are helpful.",
            messages=[{"role": "user", "content": "summarize the repo"}],
            tool_specs=[],
            coordinator_mode=True,
        )

    assert result == "fixed final answer"
    assert len(calls) == 2
    assert calls[0]["retrying"] is False
    assert calls[1]["retrying"] is True


def test_run_langchain_agent_does_not_retry_iteration_limit_in_normal_mode():
    calls = []

    def _fake_once(**kwargs):
        calls.append(kwargs)
        return _ITERATION_LIMIT_MESSAGE

    with pytest.MonkeyPatch.context() as mp:
        mp.setattr("features.langchain_fallback._run_langchain_agent_once", _fake_once)
        result = run_langchain_agent(
            model="local-model",
            api_key="lm-studio",
            base_url="http://localhost:1234/v1",
            system_prompt="You are helpful.",
            messages=[{"role": "user", "content": "summarize the repo"}],
            tool_specs=[],
            coordinator_mode=False,
        )

    assert result == _ITERATION_LIMIT_MESSAGE
    assert len(calls) == 1


def _react_retry_handler(requests, request):
    requests.append(json.loads(request.content.decode("utf-8")))
    if len(requests) == 1:
        return httpx.Response(
            200,
            json={
                "choices": [{
                    "message": {
                        "content": "This is cc-mini, an ultra-light Python harness."
                    }
                }]
            },
        )
    return httpx.Response(
        200,
        json={
            "choices": [{
                "message": {
                    "content": "Thought: I can answer now\nFinal Answer: brief answer"
                }
            }]
        },
    )
