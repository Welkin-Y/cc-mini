from types import SimpleNamespace
import threading
import time

from features.skills_bundled import register_bundled_skills
from tui.notebook import NotebookApp, NotebookPermissionPrompt, _command_suggestions


class _FakeOutput:
    def __init__(self):
        self.parts = []

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, tb):
        return False

    def clear_output(self):
        self.parts.clear()

    def append_stdout(self, text):
        self.parts.append(text)


class _FakeTextarea:
    def __init__(self, placeholder="", layout=None):
        self.placeholder = placeholder
        self.layout = layout
        self.value = ""
        self._observers = []

    def observe(self, callback, names=None):
        self._observers.append(callback)


class _FakeButton:
    def __init__(self, description="", button_style=""):
        self.description = description
        self.button_style = button_style
        self._callback = None

    def on_click(self, callback):
        self._callback = callback


class _FakeHTML:
    def __init__(self, value=""):
        self.value = value


class _FakeSelect:
    def __init__(self, options=None, rows=0, layout=None):
        self.options = options or []
        self.rows = rows
        self.layout = layout
        self.value = None
        self._observers = []

    def observe(self, callback, names=None):
        self._observers.append(callback)


class _FakeBox:
    def __init__(self, children):
        self.children = children


class _FakeWidgets:
    Output = _FakeOutput
    Textarea = _FakeTextarea
    Button = _FakeButton
    HTML = _FakeHTML
    VBox = _FakeBox
    HBox = _FakeBox
    Select = _FakeSelect

    @staticmethod
    def Layout(**kwargs):
        return kwargs


class _FakeEngine:
    def __init__(self):
        self.messages = []

    def submit(self, _text):
        yield ("tool_call", "Echo", {"message": "hi"}, None)
        yield ("tool_result", "Echo", {"message": "hi"}, SimpleNamespace(content="Echo: hi"))
        yield ("text", "done")

    def set_messages(self, messages):
        self.messages = messages


def test_command_suggestions_include_builtin_and_skills():
    register_bundled_skills()
    suggestions = _command_suggestions("/com")
    names = [name for name, _ in suggestions]
    assert "/compact" in names
    assert "/commit" in names


def test_notebook_app_renders_events():
    displayed = []
    engine = _FakeEngine()
    app = NotebookApp(
        engine,
        widgets_module=_FakeWidgets,
        display_fn=lambda widget: displayed.append(widget),
    )

    app.submit("hello", wait=True)

    assert displayed == []
    text = "".join(app._output.parts)
    assert "> hello" in text
    assert "Echo: hi" in text
    assert "done" in text


def test_notebook_app_runs_slash_commands():
    displayed = []
    engine = _FakeEngine()
    app_holder = {}
    called = []

    def _runner(command_text):
        called.append(command_text)
        app_holder["app"]._append_output("Available Commands\n/compact\n")
        return None

    app = NotebookApp(
        engine,
        widgets_module=_FakeWidgets,
        display_fn=lambda widget: displayed.append(widget),
        command_runner=_runner,
        suggestion_provider=_command_suggestions,
    )
    app_holder["app"] = app

    app.submit("/help", wait=True)

    text = "".join(app._output.parts)
    assert "Available Commands" in text
    assert "/compact" in text
    assert called == ["/help"]


def test_notebook_completion_inserts_selected_command():
    displayed = []
    engine = _FakeEngine()
    app = NotebookApp(
        engine,
        widgets_module=_FakeWidgets,
        display_fn=lambda widget: displayed.append(widget),
        suggestion_provider=_command_suggestions,
    )

    app._prompt.value = "/co"
    app._on_prompt_changed({"new": "/co"})
    assert app._suggestions.options
    app._on_suggestion_selected({"new": app._suggestions.options[0][1]})

    assert app._prompt.value.startswith("/")
    assert app._prompt.value in {"/compact", "/commit"}


def test_notebook_permission_prompt_returns_clicked_value():
    displayed = []
    prompt = NotebookPermissionPrompt(_FakeWidgets, lambda widget: displayed.append(widget))

    def _click_later():
        deadline = time.time() + 2
        while time.time() < deadline and not displayed:
            time.sleep(0.01)
        widget = displayed[0]
        buttons = widget.children[2].children
        buttons[0]._callback(None)

    thread = threading.Thread(target=_click_later)
    thread.start()
    assert prompt.ask(SimpleNamespace(name="Bash"), {"command": "echo hi"}) == "allow"
    thread.join()
