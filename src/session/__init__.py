"""White-box session module — typed conversation history with observable tool execution.

Provides::

    from session import Session, SessionStats
    from session.models import (
        UserMessage, AssistantMessage, TextBlock, ToolCallBlock, ToolResult,
        Turn, ToolStatus,
    )
    from session.events import EventBus
    from session.tool_executor import ToolExecutor
    from session.serializer import session_to_json, session_from_json
"""

from session.session import Session, SessionStats
from session.models import (
    AssistantMessage,
    TextBlock,
    ToolCallBlock,
    ToolResult,
    ToolStatus,
    Turn,
    UserMessage,
)
from session.events import EventBus
from session.tool_executor import ToolExecutor
from session.serializer import session_to_json, session_from_json

__all__ = [
    "AssistantMessage",
    "EventBus",
    "Session",
    "SessionStats",
    "TextBlock",
    "ToolCallBlock",
    "ToolExecutor",
    "ToolResult",
    "ToolStatus",
    "Turn",
    "UserMessage",
    "session_from_json",
    "session_to_json",
]
