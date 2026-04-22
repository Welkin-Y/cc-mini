from __future__ import annotations

import threading
from argparse import Namespace
from pathlib import Path
from typing import Any, Callable, Optional

from rich.console import Console

from commands import CommandContext, _COMMAND_TABLE, handle_command, parse_command
from core.config import load_app_config
from core.context import build_system_prompt
from core.engine import AbortedError, Engine
from core.permissions import PermissionChecker
from core.session import SessionStore
from features.compact import CompactService
from features.coordinator import current_session_mode
from features.cost_tracker import CostTracker
from features.memory import build_dream_prompt, record_consolidation
from features.plan import PlanModeManager
from features.sandbox.config import load_sandbox_config
from features.sandbox.manager import SandboxManager
from features.skills import build_skills_prompt_section, discover_skills, list_skills
from features.skills_bundled import register_bundled_skills
from tools import AskUserQuestionTool, BashTool, FileEditTool, FileReadTool, FileWriteTool, GlobTool, GrepTool
from tools.plan_tools import EnterPlanModeTool, ExitPlanModeTool
from tui.query import run_query


class NotebookPermissionPrompt:
    def __init__(self, widgets_module: Any, display_fn: Callable[[Any], None]):
        self._widgets = widgets_module
        self._display = display_fn
        self._event = threading.Event()
        self._response: Optional[str] = None
        self._box: Optional[Any] = None

    def ask(self, tool: Any, inputs: dict[str, Any]) -> str:
        self._event.clear()
        self._response = None

        title = self._widgets.HTML(value=f"<b>Permission required:</b> <b>{tool.name}</b>")
        details = []
        for key, value in inputs.items():
            details.append(f"<b>{key}:</b> {value}")
        body = self._widgets.HTML(value="<br>".join(details) if details else "<em>No inputs</em>")

        yes = self._widgets.Button(description="Yes", button_style="success")
        no = self._widgets.Button(description="No", button_style="danger")
        always = self._widgets.Button(description="Always", button_style="warning")

        def _choose(value: str) -> None:
            self._response = value
            self._event.set()

        yes.on_click(lambda _btn: _choose("allow"))
        no.on_click(lambda _btn: _choose("deny"))
        always.on_click(lambda _btn: _choose("always"))

        self._box = self._widgets.VBox([
            title,
            body,
            self._widgets.HBox([yes, no, always]),
        ])
        self._display(self._box)
        try:
            self._event.wait()
        finally:
            self._display(None)
        return self._response or "deny"

    def clear(self) -> None:
        self._event.clear()
        self._response = None
        self._box = None


class NotebookApp:
    def __init__(
        self,
        engine: Engine,
        *,
        widgets_module: Any,
        display_fn: Callable[[Any], None],
        prompt_widget: Optional[Any] = None,
        command_runner: Optional[Callable[[str], Optional[str]]] = None,
        suggestion_provider: Optional[Callable[[str], list[tuple[str, str]]]] = None,
    ):
        self._engine = engine
        self._widgets = widgets_module
        self._display = display_fn
        self._command_runner = command_runner
        self._suggestion_provider = suggestion_provider
        self._prompt_widget = prompt_widget or widgets_module.VBox([])
        self._output = widgets_module.Output()
        self._status = widgets_module.HTML(value="")
        self._prompt = widgets_module.Textarea(
            placeholder="Ask cc-mini or type / for commands…",
            layout=widgets_module.Layout(width="100%", height="120px"),
        )
        self._suggestions = widgets_module.Select(
            options=[],
            rows=6,
            layout=widgets_module.Layout(width="100%"),
        )
        self._suggestion_help = widgets_module.HTML(value="")
        self._send = widgets_module.Button(description="Send", button_style="primary")
        self._clear = widgets_module.Button(description="Clear")
        self._send.on_click(self._on_send_clicked)
        self._clear.on_click(self._on_clear_clicked)
        self._prompt.observe(self._on_prompt_changed, names="value")
        self._suggestions.observe(self._on_suggestion_selected, names="value")
        self._root = widgets_module.VBox([
            self._prompt_widget,
            self._output,
            self._status,
            self._suggestion_help,
            self._suggestions,
            self._prompt,
            widgets_module.HBox([self._send, self._clear]),
        ])

    @property
    def widget(self) -> Any:
        return self._root

    def display(self) -> Any:
        self._display(self._root)
        return self._root

    def set_prompt_widget(self, widget: Optional[Any]) -> None:
        self._prompt_widget.children = () if widget is None else (widget,)

    def submit(self, text: str, wait: bool = False) -> Optional[threading.Thread]:
        prompt = text.strip()
        if not prompt:
            return None
        self._append_output(f"\n> {prompt}\n")
        self._status.value = "<em>Running…</em>"
        if wait:
            self._run_turn(prompt)
            return None
        thread = threading.Thread(target=self._run_turn, args=(prompt,), daemon=True)
        thread.start()
        return thread

    def _on_send_clicked(self, _button: Any) -> None:
        prompt = self._prompt.value
        self._prompt.value = ""
        self.submit(prompt)

    def _on_clear_clicked(self, _button: Any) -> None:
        self._engine.set_messages([])
        self._output.clear_output()
        self._status.value = ""
        self._suggestion_help.value = ""
        self._suggestions.options = []

    def _on_prompt_changed(self, change: dict[str, Any]) -> None:
        value = str(change.get("new", "") or "")
        if self._suggestion_provider is None:
            self._suggestion_help.value = ""
            self._suggestions.options = []
            return
        if not value.lstrip().startswith("/"):
            self._suggestion_help.value = ""
            self._suggestions.options = []
            return
        suggestions = self._suggestion_provider(value)
        self._suggestions.options = [
            (f"{command} — {desc}" if desc else command, command)
            for command, desc in suggestions
        ]
        if suggestions:
            self._suggestion_help.value = "<em>Slash command completion</em>"
        else:
            self._suggestion_help.value = "<em>No matching commands</em>"

    def _on_suggestion_selected(self, change: dict[str, Any]) -> None:
        selection = change.get("new")
        if not selection:
            return
        if isinstance(selection, tuple):
            selection = selection[-1]
        self._prompt.value = str(selection)

    def _set_status(self, text: str) -> None:
        self._status.value = text

    def _run_turn(self, prompt: str) -> None:
        try:
            command = parse_command(prompt)
            if command is not None and self._command_runner is not None:
                follow_up = self._command_runner(prompt)
                if follow_up:
                    self._run_turn(follow_up)
                return

            for event in self._engine.submit(prompt):
                self._handle_event(event)
        except AbortedError:
            self._append_output("\n[aborted]\n")
        finally:
            self._set_status("")
            if self._prompt.value.lstrip().startswith("/"):
                self._on_prompt_changed({"new": self._prompt.value})

    def _handle_event(self, event: tuple) -> None:
        kind = event[0]
        if kind == "waiting":
            self._set_status("<em>Waiting for model…</em>")
            return
        if kind == "text":
            self._append_output(event[1])
            self._set_status("<em>Thinking…</em>")
            return
        if kind == "error":
            self._append_output(f"\n[error] {event[1]}\n")
            self._set_status("<em>Error</em>")
            return
        if kind == "tool_call":
            _, tool_name, tool_input, _activity = event
            self._append_output(f"\n↳ {tool_name}({tool_input})\n")
            self._set_status(f"<em>Permission required: {tool_name}</em>")
            return
        if kind == "tool_result":
            _, _tool_name, _tool_input, result = event
            self._append_output(f"{result.content}\n")
            self._set_status("<em>Thinking…</em>")

    def _append_output(self, text: str) -> None:
        append_stdout = getattr(self._output, "append_stdout", None)
        if callable(append_stdout):
            append_stdout(text)
            return
        if hasattr(self._output, "parts"):
            self._output.parts.append(text)
            return
        with self._output:
            print(text, end="")


def _command_suggestions(text: str) -> list[tuple[str, str]]:
    prefix = text.lstrip()
    if not prefix.startswith("/"):
        return []
    query = prefix[1:].lower()
    suggestions: list[tuple[str, str]] = []
    seen: set[str] = set()

    for name, desc, _ in _COMMAND_TABLE:
        command = f"/{name}"
        if not query or name.startswith(query):
            suggestions.append((command, desc))
            seen.add(name)

    for skill in list_skills(user_invocable_only=True):
        if skill.name in seen:
            continue
        if not query or skill.name.startswith(query):
            suggestions.append((f"/{skill.name}", skill.description or "skill"))

    return suggestions


def create_notebook_app(engine: Engine) -> NotebookApp:
    try:
        import ipywidgets as widgets
        from IPython.display import display
    except Exception as exc:
        raise RuntimeError(
            "Notebook UI requires `ipywidgets` and an IPython notebook environment."
        ) from exc
    return NotebookApp(engine, widgets_module=widgets, display_fn=display)


def _resolve_initial_model(app_config) -> str:
    model = app_config.model
    if app_config.provider != "lmstudio":
        return model
    try:
        from core.llm import LLMClient

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


def create_default_notebook_app(
    *,
    provider: Optional[str] = None,
    api_key: Optional[str] = None,
    base_url: Optional[str] = None,
    model: Optional[str] = None,
    max_tokens: Optional[int] = None,
    effort: Optional[str] = None,
    memory_dir: Optional[str] = None,
    auto_approve: bool = False,
) -> NotebookApp:
    args = Namespace(
        prompt=None,
        print=False,
        auto_approve=auto_approve,
        config=None,
        provider=provider,
        api_key=api_key,
        base_url=base_url,
        model=model,
        max_tokens=max_tokens,
        effort=effort,
        memory_dir=memory_dir,
        no_auto_dream=False,
        dream_interval=None,
        dream_min_sessions=None,
    )
    app_config = load_app_config(args)
    sandbox_mgr = SandboxManager(config=load_sandbox_config(app_config.config_paths))
    cwd = str(Path.cwd())
    cost_tracker = CostTracker()
    plan_manager = PlanModeManager()

    register_bundled_skills()
    discover_skills(cwd)
    skills_section = build_skills_prompt_section()

    current_model = [_resolve_initial_model(app_config)]
    memory_path = app_config.memory_dir
    base_prompt = build_system_prompt(cwd=cwd, model=current_model[0], memory_dir=memory_path)
    prompt = base_prompt + ("\n\n" + skills_section if skills_section else "")

    try:
        import ipywidgets as widgets
        from IPython.display import display
    except Exception as exc:
        raise RuntimeError(
            "Notebook UI requires `ipywidgets` and an IPython notebook environment."
        ) from exc

    prompt_slot = widgets.VBox([])
    prompt_ui = NotebookPermissionPrompt(
        widgets,
        lambda box: setattr(prompt_slot, "children", () if box is None else (box,)),
    )

    permissions = PermissionChecker(
        auto_approve=auto_approve,
        sandbox_manager=sandbox_mgr,
        prompt_provider=prompt_ui.ask if prompt_ui is not None else None,
    )
    session_store_ref: list[Optional[SessionStore]] = [SessionStore(
        cwd=cwd,
        model=current_model[0],
        mode=current_session_mode(),
    )]

    engine = Engine(
        tools=[
            FileReadTool(),
            GlobTool(),
            GrepTool(),
            FileEditTool(),
            FileWriteTool(),
            BashTool(sandbox_manager=sandbox_mgr),
            AskUserQuestionTool(),
            EnterPlanModeTool(plan_manager),
            ExitPlanModeTool(plan_manager),
        ],
        system_prompt=prompt,
        permission_checker=permissions,
        provider=app_config.provider,
        model=current_model[0],
        max_tokens=app_config.max_tokens,
        api_key=app_config.api_key,
        base_url=app_config.base_url,
        effort=app_config.effort,
        session_store=session_store_ref[0],
        cost_tracker=cost_tracker,
    )
    plan_manager.bind_engine(engine)
    plan_manager.set_permissions(permissions)
    permissions.set_plan_manager(plan_manager)
    compact_service = CompactService(
        client=engine._client,
        model=engine.get_model(),
        effort=app_config.effort,
    )

    app = NotebookApp(
        engine,
        widgets_module=widgets,
        display_fn=display,
        prompt_widget=prompt_slot,
        command_runner=None,
        suggestion_provider=_command_suggestions,
    )

    def _run_dream() -> None:
        if memory_path is None:
            app._append_output("[dim]Memory system not configured.[/dim]\n")
            return
        app._append_output("[dim]Starting dream consolidation…[/dim]\n")
        saved_messages = engine.get_messages()
        engine.set_messages([])
        try:
            dream_prompt = build_dream_prompt(Path(memory_path))
            run_query(engine, dream_prompt, print_mode=False, permissions=permissions, quiet=True)
        finally:
            engine.set_messages(saved_messages)
        engine.system_prompt = (
            build_system_prompt(cwd=cwd, model=engine.get_model(), memory_dir=Path(memory_path))
            + ("\n\n" + skills_section if skills_section else "")
        )
        record_consolidation(Path(memory_path))
        app._append_output("[dim]Dream consolidation complete. Memory index updated.[/dim]\n")

    def _run(command_text: str) -> Optional[str]:
        console = Console(record=True)
        ctx = CommandContext(
            engine=engine,
            session_store=session_store_ref[0],
            compact_service=compact_service,
            console=console,
            app_config=app_config,
            memory_dir=Path(memory_path) if memory_path is not None else None,
            permissions=permissions,
            run_dream=_run_dream if memory_path is not None else None,
            cost_tracker=cost_tracker,
            new_session_store=lambda: SessionStore(
                cwd=cwd,
                model=current_model[0],
                mode=current_session_mode(),
            ),
            reconfigure_mode=None,
            plan_manager=plan_manager,
            on_model_change=lambda model: current_model.__setitem__(0, model),
        )
        handled = parse_command(command_text)
        if handled is None:
            return None
        name, args = handled
        handle_command(name, args, ctx)
        session_store_ref[0] = ctx.session_store
        text = console.export_text()
        if text:
            app._append_output(text if text.endswith("\n") else text + "\n")
        return ctx.pending_query

    app._command_runner = _run
    return app
