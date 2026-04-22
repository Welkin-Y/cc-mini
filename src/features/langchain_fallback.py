from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Any, Callable, Optional


@dataclass
class LangChainToolSpec:
    name: str
    description: str
    input_schema: dict[str, Any]
    invoke: Callable[[dict[str, Any]], str]


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
) -> str:
    try:
        from langchain.agents import AgentType, initialize_agent
        from langchain.tools import Tool as LangChainTool
        from langchain_community.chat_models import ChatOpenAI
    except Exception as exc:
        raise LangChainFallbackUnavailable(
            "LangChain fallback requires `langchain` and `langchain-community`."
        ) from exc

    llm = ChatOpenAI(
        model_name=model,
        openai_api_key=api_key or "lm-studio",
        openai_api_base=base_url,
        temperature=0,
    )

    agent_tools = [
        LangChainTool(
            name=spec.name,
            description=(
                f"{spec.description}\n"
                f"Pass a JSON object matching this schema: {json.dumps(spec.input_schema, ensure_ascii=False)}"
            ),
            func=_wrap_tool_invocation(spec),
        )
        for spec in tool_specs
    ]

    agent = initialize_agent(
        agent_tools,
        llm,
        agent=AgentType.STRUCTURED_CHAT_ZERO_SHOT_REACT_DESCRIPTION,
        verbose=False,
        handle_parsing_errors=True,
    )
    prompt = _build_agent_prompt(system_prompt, messages)
    result = agent.invoke({"input": prompt})
    if isinstance(result, dict):
        output = result.get("output", "")
        return output if isinstance(output, str) else str(output)
    return str(result)


def _build_agent_prompt(system_prompt: str, messages: list[dict[str, Any]]) -> str:
    lines = [system_prompt.strip(), "", "Conversation so far:"]
    for message in messages:
        role = str(message.get("role", "user")).capitalize()
        content = _message_to_text(message.get("content", ""))
        if content:
            lines.append(f"{role}: {content}")
    lines.append("")
    lines.append("Answer the latest user request. Use tools when needed.")
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


def _wrap_tool_invocation(spec: LangChainToolSpec) -> Callable[[str], str]:
    def _invoke(raw_input: str) -> str:
        parsed = _parse_tool_input(raw_input, spec.input_schema)
        return spec.invoke(parsed)
    return _invoke


def _parse_tool_input(raw_input: str, input_schema: dict[str, Any]) -> dict[str, Any]:
    raw_input = (raw_input or "").strip()
    if not raw_input:
        return {}
    try:
        parsed = json.loads(raw_input)
        if isinstance(parsed, dict):
            return parsed
    except json.JSONDecodeError:
        pass

    properties = input_schema.get("properties", {})
    if len(properties) == 1:
        key = next(iter(properties))
        return {key: raw_input}
    return {}
