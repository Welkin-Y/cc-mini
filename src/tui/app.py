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
from commands.ui import RichCommandUI
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


def main() -> None:
    parser = argparse.ArgumentParser(prog="cc-mini",
                                     description="Minimal AI coding assistant")
    parser.add_argument("prompt", nargs="?", help="Prompt to send (optional)")
    parser.add_argument("-p", "--print", action="store_true",
                        help="Non-interactive: print response and exit")
    parser.add_argument("--auto-approve", action="store_true",
                        help="Auto-approve all tool permissions (dangerous)")
    parser.add_argument("--config", help="Path to a TOML config file")
    parser.add_argument("--provider", choices=("lmstudio",),
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
    parser.add_argument("--langchain-fallback", action="store_true",
                        help="Use LangChain tools for providers that do not support native tool calls")
    parser.add_argument("--langchain-debug", action="store_true",
                        help="Print per-step LangChain fallback debug logs to stderr")
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
            allow_langchain_fallback=args.langchain_fallback,
            debug_langchain_fallback=args.langchain_debug,
            coordinator_mode=is_coordinator_mode(),
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
            allow_langchain_fallback=args.langchain_fallback,
            debug_langchain_fallback=args.langchain_debug,
            coordinator_mode=is_coordinator_mode(),
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
        allow_langchain_fallback=args.langchain_fallback,
        debug_langchain_fallback=args.langchain_debug,
        coordinator_mode=coordinator_enabled,
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

    # Interactive REPL
    config_note = (
        f"[dim]{app_config.provider}:{_current_model()} · "
        f"max_tokens={app_config.max_tokens}[/dim]"
    )
    if is_coordinator_mode():
        config_note += " [dim yellow]· coordinator[/dim yellow]"
    session_note = f"[dim]session {session_store.session_id[:8]}[/dim]" if session_store else ""
    console.print("[bold cyan]cc-mini[/bold cyan]  "
                  f"{config_note}  {session_note}")


    _file_history = FileHistory(str(_HISTORY_FILE))

    # Track last Ctrl+C time for double-press exit (matches useDoublePress)
    last_ctrlc_time = 0.0

    # Terminal mode state — shared mutable ref toggled by "!" key binding
    _terminal_mode_ref = [False]

    animator = None

    _exiting = False

    def _drain_worker_notifications() -> None:
        if _exiting:
            return
        # Collect managers to drain: coordinator + plan-mode workers
        managers_to_drain = []
        if is_coordinator_mode():
            managers_to_drain.append(worker_manager)
        plan_wm = plan_manager.worker_manager
        if plan_wm is not None:
            managers_to_drain.append(plan_wm)
        if not managers_to_drain:
            return
        for mgr in managers_to_drain:
            while True:
                notifications = mgr.drain_notifications()
                if not notifications:
                    break
                for notification in notifications:
                    # Extract summary info from XML notification
                    import re as _re
                    _desc = _re.search(r"<summary>(.*?)</summary>", notification)
                    _uses = _re.search(r"<tool_uses>(\d+)</tool_uses>", notification)
                    _dur = _re.search(r"<duration_ms>(\d+)</duration_ms>", notification)
                    _status = _re.search(r"<status>(.*?)</status>", notification)
                    desc = _desc.group(1) if _desc else "Worker update"
                    uses = _uses.group(1) if _uses else "?"
                    dur_s = f"{int(_dur.group(1)) / 1000:.1f}" if _dur else "?"
                    status = _status.group(1) if _status else "completed"
                    icon = "[green]●[/green]" if status == "completed" else "[red]●[/red]"
                    console.print(f"\n{icon} [dim]{desc} ({uses} tool uses, {dur_s}s)[/dim]")
                    try:
                        run_query(engine, notification, print_mode=False, permissions=permissions)
                    except (KeyboardInterrupt, Exception):
                        return

    def _show_worker_status() -> None:
        """Show running worker status before prompt."""
        # Collect statuses from coordinator + plan-mode workers
        all_statuses = []
        if is_coordinator_mode():
            all_statuses.extend(worker_manager.get_running_status())
        plan_wm = plan_manager.worker_manager
        if plan_wm is not None:
            all_statuses.extend(plan_wm.get_running_status())
        for s in all_statuses:
            uses = s["tool_uses"]
            activity = s["activity"] or "working"
            console.print(
                f"[dim]  ● {s['description']} — "
                f"{uses} tool use{'s' if uses != 1 else ''} · {activity}[/dim]"
            )

    while True:
        _drain_worker_notifications()
        _show_worker_status()

        try:
            console.print()
            _terminal_mode_ref[0] = False  # always start in chat mode
            user_input = bordered_prompt(
                console,
                history=_file_history,
                completer=slash_completer,
                terminal_mode_ref=_terminal_mode_ref,
            ).strip()
        except KeyboardInterrupt:
            now = time.monotonic()
            if now - last_ctrlc_time <= _DOUBLE_PRESS_TIMEOUT_MS:
                _exiting = True
                console.print("\n[dim]Goodbye.[/dim]")
                break
            last_ctrlc_time = now
            console.print("\n[dim yellow]Press Ctrl+C again to exit[/dim yellow]")
            continue
        except EOFError:
            console.print("\n[dim]Goodbye.[/dim]")
            break

        # Reset double-press timer on any normal input
        last_ctrlc_time = 0.0

        if not user_input:
            continue

        # ---------------------------------------------------------------------------
        # Terminal mode — "!" key toggles mode in-place (no submit needed).
        # In terminal mode every submitted input is a shell command.
        # Outside terminal mode "!cmd" runs a one-off shell command.
        # ---------------------------------------------------------------------------
        if _terminal_mode_ref[0]:
            run_shell(user_input, console)
            continue

        if user_input.startswith("!") and len(user_input) > 1:
            run_shell(user_input[1:].lstrip(), console)
            continue
        if user_input.lower() in ("exit", "quit", "/exit", "/quit"):
            console.print("[dim]Goodbye.[/dim]")
            break
        if user_input.startswith("/sandbox"):
            handle_sandbox_command(user_input, sandbox_mgr, console)
            continue

        # Slash commands (session, compact, help, etc.)
        cmd = parse_command(user_input)
        if cmd is not None:
            cmd_name, cmd_args = cmd
            if cmd_name in ("exit", "quit"):
                console.print("[dim]Goodbye.[/dim]")
                break
            cmd_ctx = CommandContext(
                engine=engine,
                session_store=session_store,
                compact_service=compact_service,
                ui=RichCommandUI(console),
                app_config=app_config,
                memory_dir=memory_dir,
                permissions=permissions,
                run_dream=lambda: _run_dream(engine, memory_dir, permissions),
                cost_tracker=cost_tracker,
                new_session_store=lambda: SessionStore(
                    cwd=cwd,
                    model=_current_model(),
                    mode=current_session_mode(),
                ),
                reconfigure_mode=_apply_session_mode,
                plan_manager=plan_manager,
                on_model_change=lambda model: current_model.__setitem__(0, model),
            )
            handle_command(cmd_name, cmd_args, cmd_ctx)
            session_store = cmd_ctx.session_store
            # If the command set a pending query (e.g. /plan <description>),
            # submit it to the model instead of continuing to the next prompt.
            if cmd_ctx.pending_query:
                user_input = cmd_ctx.pending_query
                cmd_ctx.pending_query = None
                # Fall through to normal query processing below
            else:
                continue

        # Auto-compact when approaching token limits
        if should_compact(engine.get_messages(), model=_current_model(),
                          last_input_tokens=cost_tracker.last_input_tokens):
            console.print("[dim]Auto-compacting conversation…[/dim]")
            try:
                new_msgs, _ = compact_service.compact(
                    engine.get_messages(), engine.system_prompt)
                engine.set_messages(new_msgs)
                console.print(f"[dim]Context compressed to {estimate_tokens(new_msgs):,} tokens.[/dim]")
            except Exception as e:
                console.print(f"[dim red]Auto-compact failed: {e}[/dim red]")

        run_query(engine, parse_input(user_input), print_mode=False, permissions=permissions)
        _drain_worker_notifications()

        # Post-turn: extract <memory> tags
        text = engine.last_assistant_text()
        for mem in extract_memory_tags(text):
            append_to_daily_log(memory_dir, mem)

        # Auto-dream gate check
        current_sid = session_store.session_id if session_store else session_id
        sessions_path = session_store._dir if session_store else None
        if app_config.auto_dream and should_auto_dream(
            memory_dir,
            min_hours=app_config.dream_interval_hours,
            min_sessions=app_config.dream_min_sessions,
            current_session_id=current_sid,
            sessions_dir=sessions_path,
        ):
            prior_mtime = read_last_consolidated_at(memory_dir)
            if try_acquire_lock(memory_dir):
                # Gather session IDs for the dream prompt
                from features.memory import list_sessions_since
                sids = list_sessions_since(
                    prior_mtime,
                    sessions_dir=sessions_path,
                    current_session_id=current_sid,
                )
                transcript_dir = str(sessions_path) if sessions_path else ""
                try:
                    _run_dream(
                        engine, memory_dir, permissions, quiet=True,
                        transcript_dir=transcript_dir,
                        session_ids=sids,
                    )
                    release_lock(memory_dir)
                except Exception:
                    # Rollback: rewind lock mtime so dream retries next time
                    from features.memory import _lock_path
                    try:
                        lp = _lock_path(memory_dir)
                        if lp.exists():
                            os.utime(lp, (prior_mtime, prior_mtime))
                    except OSError:
                        pass

    # Print cost summary on exit
    if cost_tracker.total_cost_usd > 0:
        console.print(f"\n[dim]{cost_tracker.format_cost()}[/dim]")


if __name__ == "__main__":
    main()
