from __future__ import annotations

from typing import Any, Optional
from abc import ABC, abstractmethod
from dataclasses import dataclass


@dataclass
class ToolResult:
    content: str
    is_error: bool = False


class Tool(ABC):
    @property
    @abstractmethod
    def name(self) -> str: ...

    @property
    @abstractmethod
    def description(self) -> str: ...

    @property
    @abstractmethod
    def input_schema(self) -> dict: ...

    @abstractmethod
    def execute(self, **kwargs) -> ToolResult: ...

    def get_activity_description(self, **kwargs) -> Optional[str]:
        """Return a human-readable description of what the tool is doing, shown in the spinner."""
        return None

    def is_read_only(self) -> bool:
        return False

    def to_api_schema(self) -> dict:
        return {
            "name": self.name,
            "description": self.description,
            "input_schema": self.input_schema,
        }

    def to_langchain_description(self) -> str:
        lines = [self.description.strip()]
        properties = self.input_schema.get("properties", {})
        required = set(self.input_schema.get("required", []))

        if properties:
            lines.append("Arguments:")
            for key, meta in properties.items():
                if not isinstance(meta, dict):
                    meta = {}
                type_name = str(meta.get("type", "value"))
                desc = str(meta.get("description", "")).strip()
                required_suffix = "required" if key in required else "optional"
                line = f"- {key}: {type_name} ({required_suffix})"
                if desc:
                    line += f" - {desc}"
                lines.append(line)
        else:
            lines.append("Arguments: none")

        lines.append("Pass arguments as a JSON object.")
        return "\n".join(_escape_prompt_template(line) for line in lines if line)

    def to_langchain_args_schema(self):
        try:
            from pydantic.v1 import Field, create_model
        except Exception:  # pragma: no cover - fallback for older pydantic installs
            from pydantic import Field, create_model

        fields: dict[str, tuple[Any, Any]] = {}
        properties = self.input_schema.get("properties", {})
        required = set(self.input_schema.get("required", []))

        for key, meta in properties.items():
            if not isinstance(meta, dict):
                meta = {}
            type_name = str(meta.get("type", "string"))
            py_type = _schema_type_to_python(type_name)
            description = str(meta.get("description", "")).strip() or None
            default = ... if key in required else None
            fields[key] = (py_type, Field(default=default, description=description))

        model_name = f"{self.name}Args"
        return create_model(model_name, **fields)


def _escape_prompt_template(text: str) -> str:
    return text.replace("{", "{{").replace("}", "}}")


def _schema_type_to_python(type_name: str) -> Any:
    return {
        "string": str,
        "integer": int,
        "number": float,
        "boolean": bool,
    }.get(type_name, str)
