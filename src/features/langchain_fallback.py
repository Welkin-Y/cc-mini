from __future__ import annotations

import json
import sys
from dataclasses import dataclass
from typing import Any, Callable, Optional

import httpx

from core.tool import Tool


@dataclass
class LangChainToolSpec:
    tool: Tool
    invoke: Callable[[dict[str, Any]], str]

    @property
    def name(self) -> str:
        return self.tool.name

    @property
    def description(self) -> str:
        return self.tool.description

    @property
    def input_schema(self) -> dict[str, Any]:
        return self.tool.input_schema


class LangChainFallbackUnavailable(RuntimeError):
    pass


_TOOL_CALL_UNSUPPORTED_PATTERNS = (
    "tool_calls",
    "tools is not supported",
    "tool use is not supported",
    "function calling is not supported",
    "tools are not supported",
    "functions are not supported",
    "does not support tools",
    "does not support function",
)

_REACT_PROMPT_TEMPLATE = """{system_prompt}

You are running in fallback mode because the base model does not support native tool calling.
You must use ReAct format exactly.

You have access to the following tools:

{tools}

When you call a tool, `Action Input` must be a JSON object string matching the tool arguments.
If a tool has exactly one required argument, you may also pass a plain string and it will map to that argument.

Use the following format:

Question: the input question you must answer
Thought: think about the next best step
Action: the action to take, should be one of [{tool_names}]
Action Input: a JSON object string with the tool arguments
Observation: the result of the action
... (this Thought/Action/Action Input/Observation can repeat as needed)
Thought: I now know the final answer
Final Answer: the final answer to the original input question

Important termination rule:
- After you have seen any Observation, your next reply must be either another valid `Action:` block or a `Final Answer:`.
- Do not emit `Summary:`, `Result:`, `Explanation:`, or plain prose after a tool result.
- If you are done, emit `Final Answer:` immediately.

Question: {input}
Thought:{agent_scratchpad}"""

_REACT_PARSE_REPAIR_MESSAGE = (
    "Your previous reply could not be parsed. "
    "Respond in exact ReAct format only. "
    "If using a tool, emit `Action:` and `Action Input:`. "
    "If done, emit `Final Answer:`. "
    "Do not emit `Summary:` or plain prose after a tool result."
)
_DEFAULT_MAX_ITERATIONS = 20


def should_fallback_from_error_message(message: str) -> bool:
    lowered = message.lower()
    return any(pattern in lowered for pattern in _TOOL_CALL_UNSUPPORTED_PATTERNS)


def run_langchain_agent(
    *,
    model: str,
    api_key: Optional[str],
    base_url: Optional[str],
    system_prompt: str,
    messages: list[dict[str, Any]],
    tool_specs: list[LangChainToolSpec],
    debug: bool = False,
) -> str:
    if not base_url:
        raise LangChainFallbackUnavailable(
            "Fallback requires a reachable OpenAI-compatible base URL."
        )

    try:
        from langchain.agents import AgentExecutor, create_react_agent
        from langchain.agents.output_parsers import ReActSingleInputOutputParser
        from langchain.tools import Tool as LangChainTool
        from langchain_core.agents import AgentFinish
        from langchain_core.exceptions import OutputParserException
        from langchain_core.language_models.llms import LLM
        from langchain_core.prompts import PromptTemplate
    except Exception as exc:
        raise LangChainFallbackUnavailable(
            "LangChain fallback requires the `langchain` package."
        ) from exc

    step_counter = {"value": 0}
    tool_call_counter = {"value": 0}
    retry_state = {"last": None, "count": 0}

    class _LMStudioRestLLM(LLM):
        model_name: str
        base_url: str
        api_key: str

        @property
        def _llm_type(self) -> str:
            return "lmstudio-rest"

        @property
        def _identifying_params(self) -> dict[str, Any]:
            return {"model_name": self.model_name, "base_url": self.base_url}

        def _call(
            self,
            prompt: str,
            stop: Optional[list[str]] = None,
            run_manager: Any = None,
            **kwargs: Any,
        ) -> str:
            payload: dict[str, Any] = {
                "model": self.model_name,
                "messages": [{"role": "user", "content": prompt}],
                "temperature": 0,
            }
            if stop:
                payload["stop"] = stop

            with httpx.Client(timeout=httpx.Timeout(120.0, connect=10.0)) as client:
                response = client.post(
                    _chat_completions_url(self.base_url),
                    headers={
                        "Content-Type": "application/json",
                        "Authorization": f"Bearer {self.api_key}",
                    },
                    json=payload,
                )
                response.raise_for_status()
                text = _extract_response_text(response.json())
                step_counter["value"] += 1
                _debug_log(
                    debug,
                    f"step={step_counter['value']} model_output={_truncate_debug(text)}",
                )
                return text

    llm = _LMStudioRestLLM(
        model_name=model,
        base_url=base_url,
        api_key=api_key or "lm-studio",
    )

    class _TolerantReActOutputParser(ReActSingleInputOutputParser):
        def parse(self, text: str):  # type: ignore[override]
            stripped = text.strip()
            try:
                retry_state["last"] = None
                retry_state["count"] = 0
                return super().parse(stripped)
            except OutputParserException:
                final_text = _extract_fallback_final_answer(
                    stripped,
                    allow_plain_text=(tool_call_counter["value"] == 0),
                )
                if final_text is not None:
                    _debug_log(
                        debug,
                        f"step={step_counter['value']} accepted_final={_truncate_debug(final_text)}",
                    )
                    return AgentFinish({"output": final_text}, stripped)
                repeated_final = _accept_repeated_non_action_summary(
                    stripped,
                    retry_state=retry_state,
                    allow_repeat_accept=(tool_call_counter["value"] > 0),
                )
                if repeated_final is not None:
                    _debug_log(
                        debug,
                        f"step={step_counter['value']} accepted_repeated_final={_truncate_debug(repeated_final)}",
                    )
                    return AgentFinish({"output": repeated_final}, stripped)
                _debug_log(
                    debug,
                    f"step={step_counter['value']} parser_retry={_truncate_debug(stripped)}",
                )
                raise

    prompt = PromptTemplate.from_template(_REACT_PROMPT_TEMPLATE)
    agent_tools = [
        LangChainTool.from_function(
            func=_wrap_react_tool(spec, tool_call_counter=tool_call_counter, debug=debug),
            name=spec.name,
            description=spec.tool.to_langchain_description(),
        )
        for spec in tool_specs
    ]
    agent = create_react_agent(
        llm,
        agent_tools,
        prompt,
        output_parser=_TolerantReActOutputParser(),
    )
    executor = AgentExecutor(
        agent=agent,
        tools=agent_tools,
        verbose=False,
        handle_parsing_errors=_REACT_PARSE_REPAIR_MESSAGE,
        max_iterations=_DEFAULT_MAX_ITERATIONS,
    )
    result = executor.invoke({
        "input": _build_agent_input(messages),
        "system_prompt": system_prompt.strip(),
    })
    if isinstance(result, dict):
        output = result.get("output", "")
        return output if isinstance(output, str) else str(output)
    return str(result)


def _chat_completions_url(base_url: str) -> str:
    trimmed = base_url.rstrip("/")
    if trimmed.endswith("/chat/completions"):
        return trimmed
    if trimmed.endswith("/v1"):
        return f"{trimmed}/chat/completions"
    return f"{trimmed}/chat/completions"


def _extract_response_text(payload: dict[str, Any]) -> str:
    choices = payload.get("choices")
    if not isinstance(choices, list) or not choices:
        raise LangChainFallbackUnavailable("Fallback response did not include any choices.")
    message = choices[0].get("message", {})
    content = message.get("content", "")
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts: list[str] = []
        for item in content:
            if isinstance(item, dict) and item.get("type") == "text":
                parts.append(str(item.get("text", "")))
        return "\n".join(part for part in parts if part)
    return str(content)


def _build_agent_input(messages: list[dict[str, Any]]) -> str:
    lines = ["Conversation so far:"]
    for message in messages:
        role = str(message.get("role", "user")).capitalize()
        content = _message_to_text(message.get("content", ""))
        if content:
            lines.append(f"{role}: {content}")
    lines.append("")
    lines.append("Answer the latest user request.")
    return "\n".join(lines).strip()


def _message_to_text(content: Any) -> str:
    if isinstance(content, str):
        return content
    if not isinstance(content, list):
        return str(content)
    parts: list[str] = []
    for block in content:
        if not isinstance(block, dict):
            continue
        block_type = block.get("type")
        if block_type == "text":
            parts.append(str(block.get("text", "")))
        elif block_type == "tool_result":
            parts.append(str(block.get("content", "")))
    return "\n".join(part for part in parts if part)


def _wrap_react_tool(
    spec: LangChainToolSpec,
    *,
    tool_call_counter: Optional[dict[str, int]] = None,
    debug: bool = False,
) -> Callable[[str], str]:
    def _invoke(tool_input: str) -> str:
        parsed_input = _parse_tool_input(spec.tool, tool_input)
        if tool_call_counter is not None:
            tool_call_counter["value"] += 1
        _debug_log(
            debug,
            f"tool={spec.name} input={_truncate_debug(json.dumps(parsed_input, ensure_ascii=False))}",
        )
        return spec.invoke(parsed_input)

    return _invoke


def _parse_tool_input(tool: Tool, raw_input: Any) -> dict[str, Any]:
    if isinstance(raw_input, dict):
        return raw_input

    text = str(raw_input or "").strip()
    if not text:
        return {}

    try:
        parsed = json.loads(text)
    except json.JSONDecodeError:
        parsed = None

    if isinstance(parsed, dict):
        return parsed
    if parsed is not None:
        text = str(parsed)

    properties = tool.input_schema.get("properties", {})
    required = list(tool.input_schema.get("required", []))
    if len(required) == 1 and required[0] in properties:
        return {required[0]: text}

    raise LangChainFallbackUnavailable(
        f"Tool {tool.name} requires JSON object input, got: {raw_input}"
    )


def _extract_fallback_final_answer(text: str, *, allow_plain_text: bool = True) -> Optional[str]:
    if not text:
        return None

    try:
        payload = json.loads(text)
    except json.JSONDecodeError:
        payload = None

    if isinstance(payload, dict):
        if payload.get("type") in {"assistant", "final"}:
            content = payload.get("content", "")
            return str(content).strip() if str(content).strip() else None
        for key in ("final_answer", "answer", "output"):
            value = payload.get(key)
            if value is not None and str(value).strip():
                return str(value).strip()

    lowered = text.lower()
    if "action:" in lowered:
        return None
    if not allow_plain_text:
        return None
    return text.strip()


def _accept_repeated_non_action_summary(
    text: str,
    *,
    retry_state: dict[str, Any],
    allow_repeat_accept: bool,
) -> Optional[str]:
    if not allow_repeat_accept:
        return None

    if _looks_like_terminal_summary(text):
        return text.strip()

    normalized = _normalize_retry_text(text)
    if not normalized or len(normalized) < 20:
        retry_state["last"] = normalized
        retry_state["count"] = 1
        return None

    if retry_state.get("last") == normalized:
        retry_state["count"] = int(retry_state.get("count", 0)) + 1
    else:
        retry_state["last"] = normalized
        retry_state["count"] = 1

    if int(retry_state["count"]) >= 2:
        return text.strip()
    return None


def _normalize_retry_text(text: str) -> str:
    lowered = text.lower()
    if "action:" in lowered or "final answer:" in lowered:
        return ""
    normalized = lowered.replace("*", "").replace("`", "")
    return " ".join(normalized.split())


def _looks_like_terminal_summary(text: str) -> bool:
    normalized = _normalize_retry_text(text)
    if not normalized:
        return False
    if len(normalized) < 40:
        return False
    summary_markers = (
        "summary:",
        "test results summary",
        "final summary",
        "overall result",
        "conclusion:",
        "in summary",
    )
    return any(marker in normalized for marker in summary_markers)


def _debug_log(enabled: bool, message: str) -> None:
    if enabled:
        print(f"[langchain-fallback] {message}", file=sys.stderr, flush=True)


def _truncate_debug(text: str, limit: int = 400) -> str:
    compact = " ".join(text.split())
    if len(compact) <= limit:
        return compact
    return compact[:limit] + "..."


def _wrap_tool_invocation(spec: LangChainToolSpec) -> Callable[..., str]:
    def _invoke(**kwargs: Any) -> str:
        return spec.invoke(kwargs)

    return _invoke
