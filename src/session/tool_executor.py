"""Observable tool execution with lifecycle tracking.

Decoupled from Engine — ToolExecutor takes a callable (the tool's execute
method) and a Session, then runs the tool with full lifecycle tracking:
status transitions, timing, and event emission.
"""

from __future__ import annotations

import time
from typing import Any, Callable, Protocol

from session.models import ToolCallBlock, ToolResult, ToolStatus, _now_ms
from session.session import Session


class ToolFn(Protocol):
    """Signature for a callable that executes a tool."""

    def __call__(self, **kwargs: Any) -> ToolResult: ...


class ToolExecutor:
    """Execute tool calls with lifecycle tracking against a Session.

    Parameters
    ----------
    tools : dict[str, ToolFn]
        Mapping of tool name → callable. The callable receives keyword
        arguments from the tool-call input and must return a ToolResult.
    session : Session
        The session to record results into and emit events from.
    """

    def __init__(self, tools: dict[str, ToolFn], session: Session) -> None:
        self._tools = tools
        self._session = session

    def register(self, name: str, fn: ToolFn) -> None:
        """Add or replace a tool."""
        self._tools[name] = fn

    def execute(self, tool_call: ToolCallBlock, **extra_input: Any) -> ToolResult:
        """Execute a single tool call with full lifecycle tracking.

        1. Mark the tool call as ``executing``, record start time
        2. Call the tool function
        3. Record result, completion time, status (completed / errored)
        4. Emit lifecycle events on the session

        Returns the ToolResult (never raises — errors are captured as results).
        """
        tool_use_id = tool_call.tool_use_id
        fn = self._tools.get(tool_call.name)

        # Mark executing
        self._session.update_tool_call(
            tool_use_id,
            status=ToolStatus.EXECUTING,
            started_at=_now_ms(),
        )

        if fn is None:
            result = ToolResult(
                tool_use_id=tool_use_id,
                content=f"Unknown tool: {tool_call.name}",
                is_error=True,
            )
        else:
            merged_input = {**tool_call.input, **extra_input}
            t0 = time.perf_counter()
            try:
                result = fn(**merged_input)
                if not isinstance(result, ToolResult):
                    result = ToolResult(
                        tool_use_id=tool_use_id,
                        content=str(result),
                    )
            except Exception as exc:
                elapsed = time.perf_counter() - t0
                result = ToolResult(
                    tool_use_id=tool_use_id,
                    content=f"Tool error: {exc}",
                    is_error=True,
                )
                result.meta["exception"] = str(exc)
                result.meta["elapsed_sec"] = round(elapsed, 4)

        # Ensure result is linked
        if not result.tool_use_id:
            result.tool_use_id = tool_use_id

        # Mark completed / errored
        self._session.update_tool_call(
            tool_use_id,
            status=ToolStatus.ERRORED if result.is_error else ToolStatus.COMPLETED,
            result=result,
            completed_at=_now_ms(),
        )

        return result

    def execute_batch(
        self,
        tool_calls: list[ToolCallBlock],
        parallel: bool = False,
    ) -> list[ToolResult]:
        """Execute multiple tool calls.

        Parameters
        ----------
        tool_calls : list[ToolCallBlock]
            The tool calls to execute.
        parallel : bool
            If True, run in a ThreadPoolExecutor (max 10 workers).
            If False, run sequentially.
        """
        if not parallel or len(tool_calls) <= 1:
            return [self.execute(tc) for tc in tool_calls]

        from concurrent.futures import ThreadPoolExecutor, as_completed

        results: dict[str, ToolResult] = {}
        with ThreadPoolExecutor(max_workers=min(len(tool_calls), 10)) as pool:
            futures = {pool.submit(self.execute, tc): tc for tc in tool_calls}
            for f in as_completed(futures):
                tc = futures[f]
                try:
                    results[tc.tool_use_id] = f.result()
                except Exception as exc:
                    results[tc.tool_use_id] = ToolResult(
                        tool_use_id=tc.tool_use_id,
                        content=f"Execution error: {exc}",
                        is_error=True,
                    )

        # Return in original order
        return [results[tc.tool_use_id] for tc in tool_calls]
