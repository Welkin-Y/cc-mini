"""cc-mini entry point — argparse, engine setup, and interactive REPL."""
from __future__ import annotations

from typing import Optional
import argparse
import os
import sys
import time
from datetime import datetime
from pathlib import Path

from prompt_toolkit.history import FileHistory
from rich.console import Console

from core.config import load_app_config
from core.context import build_system_prompt
from core.engine import AbortedError, Engine
from core.llm import LLMClient
from tools import AskUserQuestionTool
from tools import AgentTool, SendMessageTool, TaskStopTool
from tools import BashTool
from tools import FileEditTool
from tools import FileReadTool
from tools import FileWriteTool
from tools import GlobTool
from tools import GrepTool
from features.coordinator import (
    current_session_mode,
    get_coordinator_system_prompt,
    get_coordinator_user_context,
    get_worker_system_prompt,
    is_coordinator_mode,
    match_session_mode,
    set_coordinator_mode,
)
from features.cost_tracker import CostTracker
from core.session import SessionStore
from features.compact import CompactService, estimate_tokens, should_compact
from tui.keylistener import EscListener
from core.permissions import PermissionChecker
from features.worker_manager import WorkerManager
from features.sandbox.config import load_sandbox_config
from features.sandbox.manager import SandboxManager
from features.memory import (
    ensure_memory_dir,
    extract_memory_tags,
    append_to_daily_log,
    build_dream_prompt,
    should_auto_dream,
    try_acquire_lock,
    release_lock,
    record_consolidation,
    read_last_consolidated_at,
)
from features.skills import discover_skills, list_skills, build_skills_prompt_section
from features.skills_bundled import register_bundled_skills
from commands import parse_command, handle_command, CommandContext
from tui.prompt import bordered_prompt, slash_completer
from tui.query import run_query
from tui.input_parser import parse_input
from tui.shell import run_shell, handle_sandbox_command

console = Console()
_HISTORY_FILE = Path.home() / ".config" / "cc-mini" / "history"

# Match claude-code-main: useDoublePress DOUBLE_PRESS_TIMEOUT_MS = 800
_DOUBLE_PRESS_TIMEOUT_MS = 0.8


def _run_dream(engine: Engine, memory_dir: Path,
               permissions: PermissionChecker, quiet: bool = False,
               transcript_dir: str = "",
               session_ids: Optional[list[str]] = None) -> None:
    """Run dream consolidation: snapshot messages, submit dream prompt, restore.

    Mirrors TS autoDream.ts — auto-dream (quiet=True) gets permission isolation;
    manual /dream runs with normal permissions (matching TS behavior).
    """
    if not quiet:
        console.print("[dim]Starting dream consolidation…[/dim]")

    # Auto-dream gets permission isolation; manual /dream does not (matches TS)
    isolated = quiet
    if isolated:
        permissions.enter_dream_mode(str(memory_dir))

    saved_messages = engine.get_messages()
    engine.set_messages([])
    try:
        dream_prompt = build_dream_prompt(
            memory_dir,
            transcript_dir=transcript_dir,
            session_ids=session_ids,
        )
        run_query(engine, dream_prompt, print_mode=False, permissions=permissions, quiet=quiet)
    finally:
        engine.set_messages(saved_messages)
        if isolated:
            permissions.exit_dream_mode()

    # Rebuild system prompt to pick up updated MEMORY.md
    engine.system_prompt = build_system_prompt(model=engine.get_model(), memory_dir=memory_dir)
    record_consolidation(memory_dir)
    if not quiet:
        console.print("[dim]Dream consolidation complete. Memory index updated.[/dim]")


def _resolve_initial_model(app_config) -> str:
    model = app_config.model
    if app_config.provider != "lmstudio":
        return model
    try:
        client = LLMClient(
            provider=app_config.provider,
            api_key=app_config.api_key,
            base_url=app_config.base_url,
        )
        models = client.list_models()
    except Exception:
        models = []
    if models:
        if model in models:
            return model
        return models[0]
    if app_config.model_list:
        if model in app_config.model_list:
            return model
        return app_config.model_list[0]
    return model


def _run_async_repl(
    *,
    engine,
    permissions,
    app_config,
    memory_dir,
    session_store,
    compact_service,
    cost_tracker,
    sandbox_mgr,
    worker_manager,
    plan_manager,
    cwd: str,
    current_model: list,
    coordinator_enabled: bool,
    session_id: str,
    _apply_session_mode,
    _build_tools_for_mode,
    _build_system_prompt_for_mode,
    _run_dream_fn,
) -> None:
    """Run the async TUI REPL (replaces the legacy sync REPL loop)."""
    import asyncio
    from tui.async_app import AsyncApp

    app = AsyncApp(
        engine=engine,
        permissions=permissions,
        cost_tracker=cost_tracker,
        memory_dir=memory_dir,
        session_store=session_store,
        compact_service=compact_service,
        app_config=app_config,
        plan_manager=plan_manager,
        worker_manager=worker_manager,
        run_dream_fn=_run_dream_fn,
        sandbox_mgr=sandbox_mgr,
    )

    # Run the async TUI
    try:
        asyncio.run(app.run())
    except (KeyboardInterrupt, EOFError):
        pass

    # Print cost summary on exit
    if cost_tracker.total_cost_usd > 0:
        console.print(f"\n[dim]{cost_tracker.format_cost()}[/dim]")


def main() -> None:
    parser = argparse.ArgumentParser(prog="cc-mini",
                                     description="Minimal AI coding assistant")
    parser.add_argument("prompt", nargs="?", help="Prompt to send (optional)")
    parser.add_argument("-p", "--print", action="store_true",
                        help="Non-interactive: print response and exit")
    parser.add_argument("--auto-approve", action="store_true",
                        help="Auto-approve all tool permissions (dangerous)")
    parser.add_argument("--config", help="Path to a TOML config file")
    parser.add_argument("--provider", choices=("anthropic", "openai", "lmstudio"),
                        help="API provider / wire format")
    parser.add_argument("--api-key", help="API key for the selected provider")
    parser.add_argument("--base-url", help="Custom API base URL for the selected provider")
    parser.add_argument("--model", help="Model name, e.g. claude-sonnet-4")
    parser.add_argument("--max-tokens", type=int,
                        help="Maximum output tokens for each model response")
    parser.add_argument("--effort", choices=("low", "medium", "high"),
                        help="Optional reasoning effort for supported OpenAI models")
    parser.add_argument("--resume", metavar="SESSION",
                        help="Resume a previous session (id or index)")
    parser.add_argument("--memory-dir", help="Override memory directory path")
    parser.add_argument("--no-auto-dream", action="store_true",
                        help="Disable automatic dream consolidation")
    parser.add_argument("--dream-interval", type=float,
                        help="Hours between auto-dream runs (default: 24)")
    parser.add_argument("--dream-min-sessions", type=int,
                        help="Minimum new sessions before auto-dream triggers (default: 5)")
    parser.add_argument("--coordinator", action="store_true",
                        help="Enable coordinator mode with background workers")
    args = parser.parse_args()

    try:
        app_config = load_app_config(args)
    except ValueError as exc:
        parser.error(str(exc))

    # Sandbox initialization
    sandbox_config = load_sandbox_config(app_config.config_paths)
    sandbox_mgr = SandboxManager(config=sandbox_config)

    # Memory setup
    memory_dir = app_config.memory_dir
    ensure_memory_dir(memory_dir)
    session_id = datetime.now().strftime("%Y%m%d-%H%M%S")

    # Skill setup — register bundled + discover project/user skills
    register_bundled_skills()
    cwd = str(Path.cwd())
    discover_skills(cwd)
    skills_section = build_skills_prompt_section()

    if args.coordinator:
        set_coordinator_mode(True)

    current_model = [_resolve_initial_model(app_config)]

    def _current_model() -> str:
        return current_model[0]

    def _build_base_tools() -> list:
        return [
            FileReadTool(), GlobTool(), GrepTool(),
            FileEditTool(), FileWriteTool(),
            BashTool(sandbox_manager=sandbox_mgr),
        ]

    worker_tool_names = [tool.name for tool in _build_base_tools()]

    def _build_system_prompt_for_mode(coordinator_enabled: bool) -> str:
        prompt = build_system_prompt(cwd=cwd, model=_current_model(), memory_dir=memory_dir)
        if skills_section:
            prompt += "\n\n" + skills_section
        if coordinator_enabled:
            extra = get_coordinator_user_context(worker_tool_names)
            worker_context = extra.get("workerToolsContext")
            if worker_context:
                prompt += "\n\n# Coordinator Context\n" + worker_context
            prompt += "\n\n" + get_coordinator_system_prompt()
        return prompt

    permissions = PermissionChecker(
        auto_approve=args.auto_approve,
        sandbox_manager=sandbox_mgr,
    )

    def _build_worker_engine() -> Engine:
        worker_permissions = PermissionChecker(
            auto_approve=True,
            sandbox_manager=sandbox_mgr,
        )
        worker_prompt = build_system_prompt(cwd=cwd, model=_current_model(), memory_dir=memory_dir)
        if skills_section:
            worker_prompt += "\n\n" + skills_section
        worker_prompt += "\n\n" + get_worker_system_prompt()
        return Engine(
            tools=_build_base_tools(),
            system_prompt=worker_prompt,
            permission_checker=worker_permissions,
            provider=app_config.provider,
            api_key=app_config.api_key,
            base_url=app_config.base_url,
            model=_current_model(),
            max_tokens=app_config.max_tokens,
            effort=app_config.effort,
        )

    def _build_plan_worker_engine() -> Engine:
        """Build a read-only worker engine for plan-mode subagents."""
        worker_permissions = PermissionChecker(
            auto_approve=True,
            sandbox_manager=sandbox_mgr,
        )
        worker_prompt = build_system_prompt(cwd=cwd, model=_current_model(), memory_dir=memory_dir)
        worker_prompt += (
            "\n\nYou are a read-only exploration agent. "
            "Use Glob, Grep, Read, and Bash (read-only commands only) to research the codebase. "
            "Report your findings clearly and concisely."
        )
        return Engine(
            tools=[FileReadTool(), GlobTool(), GrepTool(), BashTool(sandbox_manager=sandbox_mgr)],
            system_prompt=worker_prompt,
            permission_checker=worker_permissions,
            provider=app_config.provider,
            api_key=app_config.api_key,
            base_url=app_config.base_url,
            model=_current_model(),
            max_tokens=app_config.max_tokens,
            effort=app_config.effort,
        )

    worker_manager = WorkerManager(build_worker_engine=_build_worker_engine)

    # Plan mode manager
    from features.plan import PlanModeManager
    from tools.plan_tools import EnterPlanModeTool, ExitPlanModeTool
    plan_manager = PlanModeManager()

    def _build_tools_for_mode(coordinator_enabled: bool) -> list:
        tools = _build_base_tools()
        tools.append(AskUserQuestionTool())
        tools.extend([
            EnterPlanModeTool(plan_manager),
            ExitPlanModeTool(plan_manager),
        ])
        if coordinator_enabled:
            tools.extend([
                AgentTool(worker_manager),
                SendMessageTool(worker_manager),
                TaskStopTool(worker_manager),
            ])
        return tools

    coordinator_enabled = is_coordinator_mode()

    # Session & compact services
    cost_tracker = CostTracker()
    session_store: Optional[SessionStore] = None
    if not args.print:
        session_store = SessionStore(
            cwd=cwd,
            model=_current_model(),
            mode=current_session_mode(),
        )

    engine = Engine(
        tools=_build_tools_for_mode(coordinator_enabled),
        system_prompt=_build_system_prompt_for_mode(coordinator_enabled),
        permission_checker=permissions,
        provider=app_config.provider,
        api_key=app_config.api_key,
        base_url=app_config.base_url,
        model=_current_model(),
        max_tokens=app_config.max_tokens,
        effort=app_config.effort,
        session_store=session_store,
        cost_tracker=cost_tracker,
    )
    current_model[0] = engine.get_model()
    plan_manager.bind_engine(engine, build_plan_worker_engine=_build_plan_worker_engine)
    plan_manager.set_permissions(permissions)
    permissions.set_plan_manager(plan_manager)
    compact_service = CompactService(
        client=engine._client,
        model=engine.get_model(),
        effort=app_config.effort,
    )

    def _apply_session_mode(session_mode: Optional[str]) -> Optional[str]:
        warning = match_session_mode(session_mode)
        enabled = is_coordinator_mode()
        engine.set_tools(_build_tools_for_mode(enabled))
        engine.system_prompt = _build_system_prompt_for_mode(enabled)
        if session_store is not None:
            session_store.mode = current_session_mode()
        return warning

    # Handle --resume
    if args.resume and session_store is not None:
        sessions = SessionStore.list_sessions(cwd)
        target = None
        try:
            idx = int(args.resume) - 1
            if 0 <= idx < len(sessions):
                target = sessions[idx]
        except ValueError:
            needle = args.resume.lower()
            for m in sessions:
                if m.session_id.lower().startswith(needle):
                    target = m
                    break
        if target:
            meta, msgs = SessionStore.load_session(target.session_id, cwd)
            if msgs:
                warning = _apply_session_mode(meta.mode if meta is not None else None)
                engine.set_messages(msgs)
                session_store = SessionStore(
                    cwd=cwd,
                    model=_current_model(),
                    session_id=target.session_id,
                    mode=current_session_mode(),
                )
                engine.set_session_store(session_store)
                console.print(f"[green]✓[/green] Resumed: {target.title[:50]}  "
                              f"({len(msgs)} messages)")
                if warning:
                    console.print(f"[yellow]{warning}[/yellow]")
        else:
            console.print(f"[red]Session not found: {args.resume}[/red]")

    # Non-interactive / piped
    if args.print or args.prompt:
        prompt_text = args.prompt or sys.stdin.read()
        run_query(engine, parse_input(prompt_text), print_mode=args.print, permissions=permissions)
        if worker_manager.has_running_tasks():
            console.print(
                "\n[dim]Background workers are still running. Use interactive mode "
                "to receive coordinator task notifications.[/dim]"
            )
        if cost_tracker.total_cost_usd > 0:
            console.print(f"\n[dim]{cost_tracker.format_cost()}[/dim]")
        return

    # Interactive REPL — async TUI
    _run_async_repl(
        engine=engine,
        permissions=permissions,
        app_config=app_config,
        memory_dir=memory_dir,
        session_store=session_store,
        compact_service=compact_service,
        cost_tracker=cost_tracker,
        sandbox_mgr=sandbox_mgr,
        worker_manager=worker_manager,
        plan_manager=plan_manager,
        cwd=cwd,
        current_model=current_model,
        coordinator_enabled=coordinator_enabled,
        session_id=session_id,
        _apply_session_mode=_apply_session_mode,
        _build_tools_for_mode=_build_tools_for_mode,
        _build_system_prompt_for_mode=_build_system_prompt_for_mode,
        _run_dream_fn=lambda quiet=True, transcript_dir="", session_ids=None: _run_dream(
            engine, memory_dir, permissions,
            quiet=quiet, transcript_dir=transcript_dir, session_ids=session_ids,
        ),
    )


if __name__ == "__main__":
    main()
