# llm-session-whitebox

White-box conversation session manager for LLM agents. Zero runtime dependencies — stdlib only.

## Install

```bash
pip install -e /path/to/session          # dev install
pip install -e /path/to/session[viz]     # with visualization server
```

## Quick start

```python
from session import Session
from session.models import TextBlock, ToolCallBlock, ToolResult

# Create a session
s = Session()

# Add messages
s.add_user_message("Read engine.py")
s.add_assistant_message([
    TextBlock(text="Let me read that file."),
    ToolCallBlock(tool_use_id="tu_1", name="Read",
                   input={"file_path": "engine.py"}),
])

# Track tool execution
s.start_tool_call("tu_1")
s.complete_tool_call("tu_1", ToolResult(
    tool_use_id="tu_1",
    content="class Engine:\n    def submit(self, ...):\n        ...",
))

# Query the session
for turn in s.turns:
    print(turn.user_message.text, "→", turn.final_text)

print(s.stats())
```

## Visualization

```bash
pip install -e /path/to/session[viz]
session-viz                              # starts on port 8080
# or: python -m session._viz.server
```

Open http://localhost:8080 — interactive chat-bubble session inspector.

## Features

- **Typed models** — `UserMessage`, `AssistantMessage`, `TextBlock`, `ToolCallBlock`, `ToolResult`, `Turn`
- **Tool lifecycle** — `pending → executing → completed | errored` with timing
- **Event system** — `message_added`, `tool_call_started/completed`, `turn_completed`
- **Serialization** — JSON round-trip
- **Versionable history** — checkpoints with git workspace tie-in, branching
- **White-box editing** — edit/delete messages, retry tools, rollback, inject corrections

## Run tests

```bash
pip install -e /path/to/session[dev]
pytest tests/ -v
```
