"""Keyboard-first full-screen interface for the coding harness."""

import asyncio
import json
import threading
from rich.text import Text
from textual import on, work
from textual.app import App, ComposeResult
from textual.binding import Binding
from textual.containers import Horizontal, Vertical, VerticalScroll
from textual.events import Resize
from textual.message import Message
from textual.screen import ModalScreen
from textual.widgets import Button, Footer, Input, OptionList, RichLog, Static, TextArea

from . import hunks
from .usage import take_usage, usage_totals
from .apply import MAX_PATCH
from .core import MODEL_ID
from .coding import TOOLS as CODING_TOOLS
from .coding import apply_reviewed, solve
from .session import MAX_PROMPT, _terminal, _terminal_line, session_error


_one = _terminal_line

class Composer(TextArea):
    class MentionRequested(Message):
        pass

    async def _on_key(self, event):
        if event.character == "@" and self._at_word_start():
            event.prevent_default()
            event.stop()
            self.post_message(self.MentionRequested())
            return
        await super()._on_key(event)

    def _at_word_start(self):
        row, column = self.cursor_location
        line = self.document.get_line(row)
        before = line[:column]
        return not before or before[-1].isspace()


class PatchReview(ModalScreen):
    BINDINGS = [Binding("escape", "dismiss(False)", "Back"),
                Binding("n", "move(1)", "Next file"),
                Binding("p", "move(-1)", "Previous file"),
                Binding("]", "next_hunk", "Next hunk"),
                Binding("[", "previous_hunk", "Previous hunk"),
                Binding("space", "toggle_hunk", "Include/exclude hunk"),
                Binding("m", "toggle_mode", "Include/exclude mode"),
                Binding("x", "toggle_file", "Include/exclude file")]

    def __init__(self, diff):
        super().__init__()
        self.files = hunks.split(diff)
        self.selected = hunks.all_hunks(self.files)
        self.index = 0
        self.hunk_positions = []
        self.hunk_index = -1

    def compose(self) -> ComposeResult:
        with Vertical(id="review-panel"):
            yield Static("REVIEW  /  PROPOSED PATCH", classes="label")
            yield Static(id="file-status", markup=False)
            yield RichLog(id="patch", wrap=False, markup=False, highlight=False)
            with Horizontal(classes="actions"):
                yield Button("Back", id="cancel")
                yield Button("Apply selected", id="selected")
                yield Button("Apply all", id="confirm", variant="success")

    def on_mount(self):
        self.show_file()
        self.query_one("#patch").focus()

    def action_move(self, amount):
        if not self.files:
            return
        self.index = min(max(self.index + amount, 0), len(self.files) - 1)
        self.hunk_index = -1
        self.show_file()

    def action_next_hunk(self):
        if self.hunk_positions:
            self.hunk_index = min(self.hunk_index + 1, len(self.hunk_positions) - 1)
            self._scroll_to_hunk()

    def action_previous_hunk(self):
        if self.hunk_positions:
            self.hunk_index = max(self.hunk_index - 1, 0)
            self._scroll_to_hunk()

    def _scroll_to_hunk(self):
        patch = self.query_one("#patch", RichLog)
        patch.scroll_to(y=max(self.hunk_positions[self.hunk_index] - 1, 0), animate=False)
        self._update_status()

    def _update_status(self):
        item = self.files[self.index]
        excluded = sum(1 for hunk_index in range(len(item["hunks"]))
                       if (self.index, hunk_index) not in self.selected)
        if item["hunks"]:
            hunk = (f"hunk {self.hunk_index + 1}/{len(item['hunks'])}"
                    if self.hunk_index >= 0 else "n/p files")
            extra = f"  ·  ] / [ hunks  ·  space toggles  ·  {hunk}"
            if excluded:
                extra += f"  ·  {excluded} excluded"
            if item["mode"]:
                extra += ("  ·  mode included"
                          if (self.index, "mode") in self.selected
                          else "  ·  mode excluded")
                extra += "  ·  m toggles mode"
        else:
            extra = ("  ·  whole file included" if (self.index, None) in self.selected
                     else "  ·  whole file excluded")
        self.query_one("#file-status", Static).update(
            f"{self.index + 1}/{len(self.files)}  {_one(item['path'] or 'unknown')}{extra}")

    def show_file(self):
        item = self.files[self.index]
        self.hunk_positions = []
        self.hunk_index = -1
        self._update_status()
        patch = self.query_one("#patch", RichLog)
        patch.clear()
        display = Text()
        shown = 0
        excluded_file = (not item["hunks"]
                         and (self.index, None) not in self.selected)
        mode_excluded = (bool(item["mode"])
                         and (self.index, "mode") not in self.selected)
        for line in item["header"]:
            excluded = excluded_file or (mode_excluded and hunks.is_mode(line))
            display.append(_terminal(line.rstrip("\n")) + "\n",
                           style="dim" if excluded else _line_style(line))
            shown += 1
        for hunk_index, hunk in enumerate(item["hunks"]):
            self.hunk_positions.append(shown)
            excluded = (self.index, hunk_index) not in self.selected
            for line in hunk:
                if excluded:
                    display.append(_terminal(line.rstrip("\n")) + "\n", style="dim")
                else:
                    display.append(_terminal(line.rstrip("\n")) + "\n",
                                   style=_line_style(line))
                shown += 1
        patch.write(display, scroll_end=False)
        patch.scroll_home(animate=False)

    def action_toggle_hunk(self):
        item = self.files[self.index]
        if not item["hunks"]:
            key = (self.index, None)
            if key in self.selected:
                self.selected.discard(key)
            else:
                self.selected.add(key)
            self.show_file()
            return
        position = self.hunk_index if self.hunk_index >= 0 else 0
        key = (self.index, position)
        if key in self.selected:
            self.selected.discard(key)
        else:
            self.selected.add(key)
        self.hunk_index = position
        self.show_file()
        self.hunk_index = position
        self._scroll_to_hunk()

    def action_toggle_mode(self):
        item = self.files[self.index]
        if not item["mode"]:
            return
        key = (self.index, "mode")
        if key in self.selected:
            self.selected.discard(key)
        else:
            self.selected.add(key)
        self.show_file()

    def action_toggle_file(self):
        item = self.files[self.index]
        keys = [(self.index, hunk_index) for hunk_index in range(len(item["hunks"]))]
        if item["mode"] and item["hunks"]:
            keys.append((self.index, "mode"))
        if not keys:
            key = (self.index, None)
            if key in self.selected:
                self.selected.discard(key)
            else:
                self.selected.add(key)
            self.show_file()
            return
        if all(key in self.selected for key in keys):
            for key in keys:
                self.selected.discard(key)
        else:
            self.selected.update(keys)
        self.show_file()

    @on(Button.Pressed)
    def choose(self, event):
        if event.button.id == "cancel":
            self.dismiss(False)
        elif event.button.id == "confirm":
            self.dismiss(True)
        else:
            patch = hunks.render(self.files, self.selected)
            if not patch.strip():
                self.notify("No hunks are selected.")
                return
            if len(patch) > MAX_PATCH:
                self.notify("The selection is too large to apply.")
                return
            self.dismiss({"patch": patch, "kept": len(self.selected),
                          "total": len(hunks.all_hunks(self.files))})


def _applied_message(kept, total):
    if kept is not None and total is not None and kept < total:
        return (f"Applied {kept} of {total} selected hunks; the rest of the proposal "
                "was discarded. Review git diff and run project checks. Ctrl+U reverts "
                "this apply.")
    return ("Review git diff and run project checks. Commit your changes before "
            "starting a fresh session; Ctrl+U reverts this apply.")


def _line_style(line):
    return ("bold bright_cyan" if line.startswith(("diff ", "@@")) else
            "green" if line.startswith("+") else
            "red" if line.startswith("-") else "")


class FilePicker(ModalScreen[str]):
    BINDINGS = [Binding("escape", "dismiss('')", "Back")]

    def __init__(self, paths):
        super().__init__()
        self.paths = tuple(paths)
        self.matches = list(self.paths[:100])

    def compose(self):
        with Vertical(id="command-panel"):
            yield Static("Insert file reference", classes="label")
            yield Static("The model can read this path with its tools; nothing is attached automatically.",
                         classes="muted", markup=False)
            yield Input(placeholder="Filter paths…", id="file-filter")
            yield OptionList(*[_one(path) for path in self.matches],
                             id="file-options", markup=False)

    def on_mount(self):
        self.query_one("#file-filter", Input).focus()

    @on(Input.Changed)
    def filter_paths(self, event):
        query = event.value.strip().lower()
        self.matches = [path for path in self.paths if query in path.lower()][:100] \
            if query else list(self.paths[:100])
        options = self.query_one("#file-options", OptionList)
        options.clear_options()
        options.add_options([_one(path) for path in self.matches])
        self.matches = self.matches or []

    @on(Input.Submitted)
    def submit(self, event):
        if self.matches:
            self.dismiss(self.matches[0])

    @on(OptionList.OptionSelected)
    def pick(self, event):
        self.dismiss(self.matches[event.option_index])


class ApprovalPrompt(ModalScreen[bool]):
    BINDINGS = [Binding("escape", "dismiss(False)", "Deny"),
                Binding("y", "dismiss(True)", "Allow"),
                Binding("n", "dismiss(False)", "Deny")]

    def __init__(self, name, arguments):
        super().__init__()
        self.tool_name = name
        self.arguments = arguments

    def compose(self):
        with Vertical(id="command-panel"):
            yield Static("TOOL APPROVAL", classes="label")
            yield Static("This tool matches tools.ask and may change data outside the repository.",
                         classes="muted", markup=False)
            yield Static(_terminal(self.tool_name), markup=False)
            payload = json.dumps(self.arguments, ensure_ascii=True)
            shown = payload[:600]
            if len(payload) > 600:
                shown += f"\n… truncated ({len(payload)} characters total)"
            yield Static(_terminal(shown), markup=False, classes="muted")
            with Horizontal(classes="actions"):
                yield Button("Deny", id="cancel")
                yield Button("Allow once", id="confirm", variant="success")

    def on_mount(self):
        self.query_one("#cancel").focus()

    @on(Button.Pressed)
    def choose(self, event):
        self.dismiss(event.button.id == "confirm")


class PromptPicker(ModalScreen[object]):
    BINDINGS = [Binding("escape", "dismiss(None)", "Back")]

    def __init__(self, prompts):
        super().__init__()
        self.prompts = prompts

    def compose(self):
        with Vertical(id="command-panel"):
            yield Static("MCP PROMPTS", classes="label")
            yield Static("Choose a prompt to insert into the task field.", classes="muted", markup=False)
            yield OptionList(*[f"{_one(prompt.name)} — {_one(prompt.description)}"
                               for prompt in self.prompts],
                             id="prompt-list", markup=False)

    def on_mount(self):
        self.query_one("#prompt-list", OptionList).focus()

    @on(OptionList.OptionSelected)
    def pick(self, event):
        self.dismiss(self.prompts[event.option_index])


class PromptArguments(ModalScreen[dict]):
    BINDINGS = [Binding("escape", "dismiss(None)", "Back")]

    def __init__(self, prompt):
        super().__init__()
        self.prompt = prompt

    def compose(self):
        with Vertical(id="prompt-panel"):
            yield Static(_one(self.prompt.name), classes="label", markup=False)
            yield Static(_one(self.prompt.description), classes="muted", markup=False)
            for index, argument in enumerate(self.prompt.arguments):
                label = _one(argument["name"]) + (
                    " (required)" if argument.get("required") else "")
                yield Input(placeholder=label, id=f"arg-{index}")
            with Horizontal(classes="actions"):
                yield Button("Cancel", id="cancel")
                yield Button("Render", id="confirm", variant="success")

    def on_mount(self):
        first = self.query("Input")
        if first:
            first.first().focus()

    @on(Button.Pressed)
    def choose(self, event):
        if event.button.id == "cancel":
            self.dismiss(None)
            return
        values = {}
        for index, argument in enumerate(self.prompt.arguments):
            value = self.query_one(f"#arg-{index}", Input).value.strip()
            if argument.get("required") and not value:
                self.notify(f"{_one(argument['name'])} is required.", markup=False)
                return
            if value:
                values[argument["name"]] = value
        self.dismiss(values)


class VerifyReview(ModalScreen[bool]):
    BINDINGS = [Binding("escape", "dismiss(False)", "Back")]

    def __init__(self, commands, purpose="verify"):
        super().__init__()
        self.commands = commands
        self.purpose = purpose

    def compose(self):
        fixing = self.purpose == "fix"
        with Vertical(id="command-panel"):
            yield Static("FIX VERIFICATION" if fixing else "RUN VERIFICATION",
                         classes="label")
            yield Static(
                ("These commands run on a temporary snapshot, then the model is asked "
                 "for a patch if they fail. Nothing is applied automatically."
                 if fixing else
                 "These commands run with your account. Review them first; provider "
                 "keys are removed."), classes="muted", markup=False)
            yield RichLog(id="verify-list", markup=False, highlight=False)
            with Horizontal(classes="actions"):
                yield Button("Back", id="cancel")
                yield Button("Run and propose fix" if fixing else "Run commands",
                             id="confirm", variant="success")

    def on_mount(self):
        log = self.query_one("#verify-list", RichLog)
        for argv in self.commands:
            log.write("$ " + " ".join(_one(part) for part in argv))
        self.query_one("#cancel").focus()

    @on(Button.Pressed)
    def choose(self, event):
        self.dismiss(event.button.id == "confirm")


class CredentialReview(ModalScreen[bool]):
    BINDINGS = [Binding("escape", "dismiss(False)", "Back")]

    def __init__(self, findings, truncated=False):
        super().__init__()
        self.findings = tuple(findings)
        self.truncated = truncated

    def compose(self):
        with Vertical(id="command-panel"):
            yield Static("CREDENTIALS DETECTED", classes="label")
            yield Static(
                ("The credential scan stopped at its size limit; the rest was not "
                 "scanned. Apply anyway only after reviewing the patch."
                 if self.truncated else
                 "The patch adds lines that look like credentials. Values are "
                 "not shown; apply only after reviewing them."),
                classes="muted", markup=False)
            yield RichLog(id="credential-list", markup=False, highlight=False)
            with Horizontal(classes="actions"):
                yield Button("Back", id="cancel")
                yield Button("Apply anyway", id="confirm", variant="warning")

    def on_mount(self):
        log = self.query_one("#credential-list", RichLog)
        for item in self.findings:
            log.write(f"{_one(item['file'])}:{_one(item['line'])} "
                      f"{_one(item['kind'])}")
        self.query_one("#cancel").focus()

    @on(Button.Pressed)
    def choose(self, event):
        self.dismiss(event.button.id == "confirm")


class ModelPrompt(ModalScreen[str]):
    BINDINGS = [Binding("escape", "dismiss('')", "Back")]
    PATTERN = MODEL_ID

    def __init__(self, current, models=()):
        super().__init__()
        self.current = current
        self.models = tuple(models)

    def compose(self):
        with Vertical(id="command-panel"):
            yield Static("Provider model", classes="label")
            yield Static("Pick a listed model or type a custom ID, for example beam-free.",
                         classes="muted", markup=False)
            if self.models:
                yield OptionList(*[_one(name) for name in self.models], id="model-options", markup=False)
            yield Input(value="", id="model-input", placeholder=self.current)

    def on_mount(self):
        if self.models:
            self.query_one("#model-options", OptionList).focus()
        else:
            self.query_one("#model-input", Input).focus()

    @on(OptionList.OptionSelected)
    def pick(self, event):
        self.dismiss(str(event.option.prompt))

    @on(Input.Submitted)
    def submit(self, event):
        value = event.value.strip()
        if not self.PATTERN.fullmatch(value):
            self.notify("Use the provider's model ID (letters, digits, . _ : -).")
            return
        self.dismiss(value)


class CommandMenu(ModalScreen[str]):
    BINDINGS = [Binding("escape", "dismiss('')", "Back")]

    def compose(self):
        with Vertical(id="command-panel"):
            yield Static("Commands", classes="label")
            yield OptionList("Review proposed changes", "Toggle workspace context", "Focus task",
                             "Show skills", "Show prompt commands", "Show extensions",
                             "Show sessions", "Fork session", "Discard proposal",
                             "Undo last step", "Change model", "Run verification",
                             "Fix verification", "List provider models", "MCP prompts",
                             "Resume latest session", "Search sessions",
                             "Insert file reference", id="commands")

    @on(OptionList.OptionSelected)
    def pick(self, event):
        self.dismiss(("review", "context", "focus", "skills", "commands", "extensions",
                      "sessions", "fork", "discard", "undo", "model", "verify", "fix",
                      "models", "prompts", "resume", "search",
                      "file")[event.option_index])


class NalarApp(App):
    TITLE = "Nalar Agent"
    ENABLE_COMMAND_PALETTE = False
    CSS_PATH = "assets/terminal.tcss"
    BINDINGS = [
        Binding("ctrl+g", "send_task", "Send task", priority=True),
        Binding("ctrl+r", "review_patch", "Review diff", priority=True),
        Binding("enter", "submit_composer", show=False, priority=True),
        Binding("ctrl+j,shift+enter", "newline", show=False, priority=True),
        Binding("ctrl+p", "commands", "Commands", priority=True),
        Binding("ctrl+b", "context_panel", "Context"),
        Binding("ctrl+u", "undo_apply", "Undo apply", priority=True),
        Binding("escape", "cancel", "Cancel"),
        Binding("ctrl+q", "quit", "Quit"),
    ]

    def __init__(self, repo, model, chat_factory, *, extensions=None, permission="ask",
                 max_turns=12, max_calls=24, context_budget=120_000, token_budget=0,
                 budget=None, store=None, session=None, retries=2,
                 verify_commands=(), secret_env=(), models_fetcher=None, commands=(),
                 fix_config=None, review_factory=None, review_model=None,
                 subagent_model=None):
        super().__init__()
        self.repo = repo
        self.model = model
        self.chat_factory = chat_factory
        self.extensions = extensions
        self.permission = permission
        self.max_turns = max_turns
        self.max_calls = max_calls
        self.context_budget = context_budget
        self.token_budget = token_budget
        self.retries = retries
        from .agent import Budget

        self.budget = budget if budget is not None else Budget(token_budget)
        self.store = store
        self.session = session
        self._session_generation = 0
        self.cancel_event = None
        self.verify_commands = tuple(verify_commands)
        self.secret_env = tuple(secret_env)
        self.last_verify = None
        self.last_run = None
        self.last_review = None
        self.last_search = None
        self.plan = []
        self.models_fetcher = models_fetcher
        self.models_cache = []
        self.commands = tuple(commands)
        self.fix_config = fix_config
        self.review_factory = review_factory
        self.review_model = review_model
        self.subagent_model = subagent_model
        self._pending_fix = False
        self.composer_from_prompt = False
        self.composer_prompt_text = ""
        self.busy = False
        self.previous = []
        self.history = []
        self.applied = False
        self.read_only = chat_factory is None

    def compose(self) -> ComposeResult:
        with Horizontal(id="banner"):
            yield Static("◆  nalar", id="wordmark")
            yield Static(f"{_terminal(self.repo.root.name)}  /  {_terminal(self.model)}", id="surface", markup=False)
        with Horizontal(id="workspace"):
            with Vertical(id="main"):
                yield RichLog(id="conversation", wrap=True, markup=False, highlight=False, max_lines=1600)
                yield Static("", id="live", markup=False)
                yield Static("TASK  ·  Enter sends  ·  Ctrl+J adds a line", classes="label", id="composer-label")
                yield Composer(id="composer")
                with Horizontal(classes="actions"):
                    yield Button("Send task", id="send", variant="success")
                    yield Button("Review diff", id="review", disabled=True)
            with VerticalScroll(id="sidebar"):
                yield Static("WORKSPACE", classes="label")
                yield Static(id="repo-context", markup=False)
                yield Static("PROVIDER", classes="label")
                yield Static(f"the default provider\n{self.model}\nBYOK", classes="muted", markup=False)
                yield Static("CHANGE POLICY", classes="label")
                yield Static("Read from Git snapshot.\nWrite only after review.\nNo shell execution.", classes="muted")
                yield Static("EXTENSIONS", classes="label")
                yield Static(id="extensions", markup=False, classes="muted")
                yield Static("PLAN", classes="label")
                yield Static(id="plan", markup=False, classes="muted")
                yield Static("SESSION", classes="label")
                yield Static(id="session-id", markup=False, classes="muted")
                yield Static("LAST RUN", classes="label")
                yield Static(id="run-stats", markup=False, classes="muted")
        yield Static("READY", id="status", markup=False)
        yield Footer()

    def on_mount(self):
        self.add_class("context-hidden")
        self._show_repo_context()
        self._show_extensions()
        self._show_run_stats()
        self._show_plan()
        self._open_session()
        self._say("Nalar", "Describe a coding task. I’ll propose a patch for you to review.")
        if self.session is not None:
            self._replay_session()
        if self.permission == "auto":
            self._say("Policy", "Auto-apply is on: a successful proposal is applied when the base is clean.", "#ffd08a")
        if self.read_only:
            self._say("Setup", "Set NALAR_API_KEY in your terminal and launch Nalar again.", "#ffd08a")
        for warning in getattr(self.extensions, "warnings", ()):
            self._say("Extension", warning, "#ffd08a")
        self._state("BYOK KEY NOT SET" if self.read_only else "READY")
        self.query_one("#composer").focus()

    def _open_session(self):
        if self.store is None:
            self.query_one("#session-id", Static).update("not persisted")
            return
        if self.session is None:
            if self.read_only:
                self.query_one("#session-id", Static).update("not saved (no key)")
                return
            self.session = self.store.create(self.repo.root, self.model, self.repo.commit.decode())
            self._session_generation += 1
        self.query_one("#session-id", Static).update(self.session.id)

    def _replay_session(self):
        if not self.session.entries:
            return
        self._say("Resumed", f"Session {self.session.id}; previous moves are replayed.", "#b7cbc2")
        for entry in self.session.entries[-20:]:
            if entry["kind"] == "user":
                self._say("You (saved)", entry["text"], "#b7cbc2")
            elif entry["kind"] == "assistant":
                self._say("Nalar (saved)", entry["text"], "#b7cbc2")
        self.previous = [entry["text"] for entry in self.session.entries
                         if entry["kind"] == "user"][-4:]

    def _show_run_stats(self):
        if self.last_run is None:
            text = "No task yet."
        else:
            tokens = self.last_run.tokens if self.last_run.tokens is not None else 0
            text = (f"{tokens} tokens · {self.last_run.compacted} compacted\n"
                    f"{self.last_run.turns} turns · {self.last_run.calls} calls")
        self.query_one("#run-stats", Static).update(text)

    def _show_plan(self):
        if not self.plan:
            text = "No plan yet."
        else:
            marks = {"completed": "x", "in_progress": ">", "pending": " "}
            text = "\n".join(f"[{marks.get(item.get('status'), '?')}] "
                             f"{_terminal(item.get('step', ''))}" for item in self.plan[:20])
        self.query_one("#plan", Static).update(text)

    def _show_extensions(self):
        if self.extensions is None:
            summary = "Built-in tools only"
        else:
            skills = ", ".join(skill.name for skill in self.extensions.skills) or "none"
            builtin = ({spec["function"]["name"] for spec in CODING_TOOLS}
                       | {"use_skill", "delegate"})
            remote = len([name for name in self.extensions.belt.names
                          if name not in builtin])
            names = getattr(self.extensions, "instructions", ())
            summary = (f"Skills: {skills}\nMCP/plugin tools: {max(remote, 0)}"
                       f"\nInstructions: {', '.join(names) or 'none'}")
        self.query_one("#extensions", Static).update(summary)

    def on_resize(self, event: Resize):
        self.set_class(event.size.width < 82, "compact")

    def check_action(self, action, parameters):
        if action in {"submit_composer", "newline"}:
            return self.screen is self.screen_stack[0] and isinstance(self.focused, TextArea)
        return super().check_action(action, parameters)

    def action_submit_composer(self):
        self.action_send_task()

    def action_newline(self):
        self.query_one("#composer", TextArea).insert("\n")

    def action_context_panel(self):
        if self.screen is self.screen_stack[0]:
            self.toggle_class("context-hidden")

    def action_cancel(self):
        if self.screen is not self.screen_stack[0]:
            return
        if self.busy and self.cancel_event is not None:
            self.cancel_event.set()
            self._say("Policy", "Cancelling after the current step; partial edits are discarded.", "#ffd08a")
            return
        composer = self.query_one("#composer", TextArea)
        if composer.text:
            composer.load_text("")
        self.composer_from_prompt = False
        self.composer_prompt_text = ""

    def approve_tool(self, name, arguments):
        if not self.is_running or self.screen is not self.screen_stack[0]:
            return False
        event = threading.Event()
        decision = {"approved": False}

        def show():
            def resolved(approved):
                decision["approved"] = bool(approved)
                event.set()
            self.push_screen(ApprovalPrompt(name, arguments), resolved)

        try:
            self.call_from_thread(show)
        except RuntimeError:
            return False
        return event.wait(300) and decision["approved"]

    def action_resume_latest(self):
        if self.busy or self.screen is not self.screen_stack[0]:
            return
        if self.store is None:
            self._say("Sessions", "Session persistence is disabled.", "#b7cbc2")
            return
        self._resume_latest()

    @work(exit_on_error=False)
    async def _resume_latest(self):
        generation = self._session_generation
        current = self.session.id if self.session is not None else None
        try:
            recent = await asyncio.to_thread(
                self.store.list, self.repo.root, 1)
        except Exception:
            self._say("Sessions", "The saved sessions could not be read.", "#ffd08a")
            return
        if (self.busy or self.screen is not self.screen_stack[0]
                or self._session_generation != generation
                or (self.session.id if self.session is not None else None) != current):
            return
        if not recent:
            self._say("Sessions", "No saved session for this repository.", "#b7cbc2")
            return
        self._resume_session(recent[0]["id"])

    def action_resume(self, session_id):
        if self.busy or self.screen is not self.screen_stack[0]:
            return
        if self.store is None:
            self._say("Sessions", "Session persistence is disabled.", "#b7cbc2")
            return
        self._resume_session(session_id)

    def _resume_session(self, session_id):
        try:
            latest = self.store.load(session_id)
        except (ValueError, OSError, RecursionError):
            self._say("Sessions", "The saved session could not be read.", "#ffd08a")
            return
        if latest.repo != str(self.repo.root):
            self._say("Sessions", "That session belongs to another repository.", "#ffd08a")
            return
        if latest.base != self.repo.commit.decode():
            self._say("Sessions", "That session started from another commit; start fresh.",
                      "#ffd08a")
            return
        if self.session is not None and self.session.id == latest.id and self.session.entries:
            self._say("Sessions", "That transcript is already open.", "#b7cbc2")
            return
        self.session = latest
        self._session_generation += 1
        self.query_one("#session-id", Static).update(latest.id)
        self._replay_session()
        self._state(f"RESUMED  /  {len(latest.entries)} entries")

    def action_search_sessions(self, query=None):
        if self.busy or self.screen is not self.screen_stack[0]:
            return
        if self.store is None:
            self._say("Sessions", "Session persistence is disabled.", "#b7cbc2")
            return
        if not query:
            self._say("Search", "Usage: /search TEXT", "#b7cbc2")
            return
        self.busy = True
        self.last_search = None
        self._state("SEARCH  /  Looking through transcripts")
        self._search_sessions(query)

    @work(exit_on_error=False)
    async def _search_sessions(self, query):
        error = ""
        try:
            found = await asyncio.to_thread(self.store.search, query,
                                            self.repo.root, 5)
        except ValueError as exc:
            found = None
            error = session_error(exc)
        except Exception:
            found = None
            error = "The transcripts could not be searched."
        self.busy = False
        self.last_search = found
        if found is None:
            self._say("Search", error or "Search failed.", "#ffd08a")
            self._state("SEARCH FAILED")
            return
        if not found:
            self._say("Search", "No matching transcripts for this repository.",
                      "#b7cbc2")
            self._state("SEARCH  /  no matches")
            return
        lines = []
        for item in found:
            lines.append(f"{item.get('id', '')}  {item.get('updated', '')}  "
                         f"{str(item.get('model', ''))}  "
                         f"{str(item.get('task', ''))[:60]}")
            for match in item.get("matches", []):
                lines.append(f"  {match.get('kind', '')}: "
                             f"{match.get('snippet', '')}")
        self._say("Search", "\n".join(lines))
        self._state(f"SEARCH  /  {len(found)} session(s)")

    def action_files(self):
        if self.busy or self.screen is not self.screen_stack[0]:
            return
        paths = self.repo.paths()
        if not paths:
            self._say("Files", "No readable files in the snapshot.", "#b7cbc2")
            return
        self.push_screen(FilePicker(paths), self._file_selected)

    def _file_selected(self, path):
        if not path:
            return
        composer = self.query_one("#composer", TextArea)
        composer.insert(f"@{path} ")
        composer.focus()
        self._state("FILE REFERENCE INSERTED")

    def action_prompts(self):
        if self.busy or self.screen is not self.screen_stack[0]:
            return
        prompts = getattr(self.extensions, "prompts", ())
        if not prompts:
            self._say("Prompts", "No MCP prompts are available.", "#b7cbc2")
            return
        self.push_screen(PromptPicker(prompts), self._prompt_chosen)

    def _prompt_chosen(self, prompt):
        if prompt is None:
            return
        arguments = getattr(prompt, "arguments", ())
        if arguments:
            self.push_screen(PromptArguments(prompt),
                             lambda values: self._prompt_values(prompt, values))
            return
        self._fetch_prompt(prompt, {})

    def _prompt_values(self, prompt, values):
        if values is not None:
            self._fetch_prompt(prompt, values)

    def _fetch_prompt(self, prompt, arguments):
        self.busy = True
        self._state(f"PROMPT  /  Loading {_one(prompt.name)}")
        self._load_prompt(prompt, arguments)

    @work
    async def _load_prompt(self, prompt, arguments):
        try:
            text = await asyncio.to_thread(prompt.fetch, arguments)
        except Exception:
            text = ""
        self.busy = False
        if not text:
            self._say("Prompts", f"{_one(prompt.name)} could not be loaded "
                                "(arguments may be required).", "#ffd08a")
            self._state("PROMPT UNAVAILABLE")
            return
        shown = _terminal(text)
        if len(shown) > MAX_PROMPT:
            self._say("Prompts", f"{_one(prompt.name)} rendered {len(shown)} characters; "
                                 f"the task limit is {MAX_PROMPT}.", "#ffd08a")
            self._state("PROMPT TOO LARGE")
            return
        composer = self.query_one("#composer", TextArea)
        composer.insert(shown)
        self.composer_from_prompt = True
        self.composer_prompt_text = shown.strip()
        composer.focus()
        self._say("Prompt", f"Inserted {_one(prompt.name)}. Review it before sending.", "#b7cbc2")
        self._state("PROMPT INSERTED")

    def action_models(self):
        if self.busy or self.screen is not self.screen_stack[0]:
            return
        if self.models_fetcher is None:
            self._say("Models", "Set the provider key in your environment to list models.", "#ffd08a")
            return
        self.busy = True
        self._state("MODELS  /  Querying the provider")
        self._fetch_models()

    @work
    async def _fetch_models(self):
        try:
            models = await asyncio.to_thread(self.models_fetcher)
        except Exception:
            models = []
        self.busy = False
        self.last_models = models
        self.models_cache = list(models)
        self._say("Models", "\n".join(_one(name) for name in models[:200])
                  if models else "No models were returned.",
                  "#b7cbc2" if models else "#ffd08a")
        self._state(f"{len(models)} models available" if models else "MODEL LIST UNAVAILABLE")

    def action_verify(self):
        if self.busy or self.screen is not self.screen_stack[0]:
            return
        if not self.verify_commands:
            self._say("Verify", "Add a verify list to nalar.json, for example "
                      '[[\"pytest\", \"-q\"]].', "#ffd08a")
            return
        self.push_screen(VerifyReview(self.verify_commands), self._verified)

    def action_fix(self):
        if self.busy or self.screen is not self.screen_stack[0]:
            return
        if self.fix_config is None or not self.verify_commands:
            self._say("Fix", "Add a verify list to nalar.json first.", "#ffd08a")
            return
        if self.repo.patch():
            self._say("Fix", "Apply or discard the current proposal first.", "#ffd08a")
            return
        self._pending_fix = True
        self.push_screen(VerifyReview(self.verify_commands, purpose="fix"),
                         self._verified)

    def _verified(self, approved):
        if not approved:
            self._pending_fix = False
            return
        self.busy = True
        if self._pending_fix:
            self._pending_fix = False
            self._session_generation += 1
            self._state("FIXING  /  Verifying and proposing")
            self._run_fix()
            return
        self._state("VERIFYING  /  Running configured commands")
        self._run_verify()

    @work
    async def _run_fix(self):
        from .fix import run_fix

        limit = (self.fix_config.token_budget if self.fix_config is not None
                 else self.token_budget)
        if self.budget is not None:
            self.budget.reset(limit)
        payload = None
        verification = {}
        try:
            chat = self.chat_factory(self.model) if self.chat_factory else None
            payload = await asyncio.to_thread(
                run_fix, self.repo, chat, self.fix_config, attempts=2,
                belt=self.extensions.belt if self.extensions else None,
                system=self.extensions.system if self.extensions else None,
                budget=self.budget, approve=self.approve_tool,
                on_event=self._run_event)
        except Exception as exc:
            self._say("Fix", str(exc), "#ffd08a")
            status = "FIX FAILED"
        else:
            verification = payload.get("verification") or {}
            if payload.get("already_ok"):
                self._say("Fix", "Verification already passes.")
                status = "VERIFIED"
            elif payload.get("patch"):
                self._say("Fix", payload["patch"][:6_000])
                if verification.get("ok"):
                    self._say("Fix", "Patch verified. Ctrl+R reviews it before apply.")
                    status = "FIX PROPOSED  /  Verified"
                else:
                    self._say("Fix", "Patch proposed but verification still fails. "
                              "Ctrl+R reviews it; nothing was applied.", "#ffd08a")
                    status = "FIX PROPOSED  /  Verification failed"
            else:
                self._say("Fix", "No patch was proposed.", "#ffd08a")
                status = "FIX FAILED  /  No patch"
        finally:
            usage = take_usage(self.extensions.subagent_usage
                               if self.extensions is not None else None)
            delegated = usage_totals(usage)
        if payload is not None:
            parent = payload.get("tokens") or 0
            if self.budget is not None:
                parent = max(0, parent - sum(delegated.values()))
            self._record("fix", f"{payload.get('attempts', 0)} attempt(s)",
                         status="ok" if verification.get("ok") else "failed",
                         tokens=parent, model=self.model)
        elif delegated:
            self._record("fix", "failed", status="failed", tokens=0,
                         model=self.model)
        for label, amount in delegated.items():
            self._record("subagent", f"{amount} tokens",
                         model=label, tokens=amount)
        self.busy = False
        self._state(status)

    @work
    async def _run_verify(self):
        from .verify import run_commands

        try:
            result = await asyncio.to_thread(run_commands, self.verify_commands, self.repo.root,
                                             secret_names=self.secret_env)
        except (OSError, ValueError):
            result = {"ok": False, "commands": []}
            self._say("Verify", "Verification could not start.", "#ffd08a")
        self.last_verify = result
        for item in result["commands"]:
            color = "#b7cbc2" if item["exit"] == 0 else "#ffd08a"
            from .verify import status_text

            self._say("Verify", f"$ {' '.join(_one(arg) for arg in item['argv'])}\n"
                                 f"{status_text(item)} · {item['duration_ms']} ms\n"
                                 f"{item['output'][:4000]}", color)
        self.busy = False
        self._record("verify", "ok" if result["ok"] else "failed")
        self._state("VERIFIED" if result["ok"] else "VERIFY FAILED")

    def action_model(self):
        if self.busy or self.screen is not self.screen_stack[0]:
            return
        if self.models_cache or self.models_fetcher is None:
            self.push_screen(ModelPrompt(self.model, self.models_cache), self._model_selected)
            return
        self.busy = True
        self._state("MODELS  /  Loading the provider list")
        self._prepare_model_prompt()

    @work
    async def _prepare_model_prompt(self):
        try:
            models = await asyncio.to_thread(self.models_fetcher)
        except Exception:
            models = []
        self.models_cache = list(models)
        self.busy = False
        self.push_screen(ModelPrompt(self.model, models), self._model_selected)
        self._state(f"{len(models)} models available" if models else "TYPE A MODEL ID")

    def _model_selected(self, value):
        if value:
            self._set_model(value)

    def _set_model(self, value):
        if self.busy:
            self._say("Model", "Wait for the current operation first.", "#ffd08a")
            return False
        if not ModelPrompt.PATTERN.fullmatch(value):
            self._say("Model", "Use the provider model ID (letters, digits, . _ : -).", "#ffd08a")
            return False
        if value == self.model:
            return True
        self.model = value
        if isinstance(self.subagent_model, dict):
            self.subagent_model["value"] = value
        self.query_one("#surface", Static).update(
            f"{_terminal(self.repo.root.name)}  /  {_terminal(value)}")
        if self.store is not None and self.session is not None:
            self.session.model = value
            try:
                self.store.save(self.session)
            except ValueError:
                pass
        self._say("Model", f"Now using {value}.")
        return True

    def action_commands(self):
        if self.busy:
            return
        if self.screen is self.screen_stack[0]:
            self.push_screen(CommandMenu(), self._command_selected)

    def _command_selected(self, selection):
        if self.busy:
            self._say("Commands", "Wait for the current operation first.", "#ffd08a")
            return
        if selection == "review":
            self.action_review_patch()
        elif selection == "context":
            self.action_context_panel()
        elif selection == "focus":
            self.query_one("#composer").focus()
        elif selection == "skills":
            names = [f"{skill.name}: {skill.description}"
                     for skill in getattr(self.extensions, "skills", ())]
            self._say("Skills", "\n".join(names) or "No skills discovered.")
        elif selection == "commands":
            names = [f"/{item.name}  {item.description}" for item in self.commands]
            self._say("Prompt commands", "\n".join(names) or "No commands discovered.")
        elif selection == "extensions":
            if self.extensions is None:
                self._say("Extensions", "Built-in tools only.")
            else:
                lines = [f"{spec['function']['name']}: {spec['function']['description'][:120]}"
                         for spec in self.extensions.specs]
                self._say("Extensions", "\n".join(lines))
        elif selection == "discard":
            self.repo.restore({}, {})
            self.history.clear()
            self.previous.clear()
            self._say("Proposal", "Discarded all unapplied edits.")
            self._state("READY  /  No proposal")
        elif selection == "undo":
            self.action_undo()
        elif selection == "model":
            self.action_model()
        elif selection == "search":
            composer = self.query_one("#composer", TextArea)
            if composer.text.strip():
                composer.focus()
                self._say("Search", "Send or clear the draft before searching.",
                          "#ffd08a")
                return
            composer.load_text("/search ")
            composer.cursor_location = (0, len(composer.text))
            composer.focus()
            self._state("SEARCH  /  type a query")
        elif selection == "file":
            self.action_files()
        elif selection == "resume":
            self.action_resume_latest()
        elif selection == "prompts":
            self.action_prompts()
        elif selection == "models":
            self.action_models()
        elif selection == "verify":
            self.action_verify()
        elif selection == "fix":
            self.action_fix()
        elif selection == "sessions":
            self._list_sessions()
        elif selection == "fork":
            self.action_fork_session()

    def _show_repo_context(self):
        root = _terminal(self.repo.root.name)
        base = self.repo.commit.decode()[:12]
        self.query_one("#repo-context", Static).update(f"{root}\n{base}")

    def _say(self, who, text, color="#e1ece6"):
        log = self.query_one("#conversation", RichLog)
        block = Text()
        block.append(who.upper() + "\n", style="bold #7fe2b8")
        block.append(_terminal(text), style=color)
        log.write(block)
        log.write("")

    def _slash(self, text):
        command, _, argument = text.partition(" ")
        command = command.lower()
        argument = argument.strip()
        if command == "/help":
            names = ", ".join(f"/{item.name}" for item in self.commands)
            self._say("Commands", self.SLASH + (f"\nPrompt commands: {names}" if names else ""))
        elif command == "/file":
            self.action_files()
        elif command in {"/diff", "/apply"}:
            self.action_review_patch()
        elif command == "/review":
            self.action_review_proposal()
        elif command == "/undo":
            self.action_undo()
        elif command == "/reset":
            self.repo.restore({}, {})
            self.history.clear()
            self.previous.clear()
            self._state("READY  /  No proposal")
            self._say("Proposal", "Discarded all unapplied edits.")
        elif command == "/verify":
            self.action_verify()
        elif command == "/fix":
            self.action_fix()
        elif command == "/model":
            if argument:
                self._set_model(argument)
            else:
                self.action_model()
        elif command == "/search":
            self.action_search_sessions(argument)
        elif command == "/sessions":
            self._list_sessions()
        elif command == "/fork":
            self.action_fork_session()
        elif command == "/resume":
            if argument:
                self.action_resume(argument)
            else:
                self.action_resume_latest()
        elif command == "/quit":
            self.exit()
        else:
            from .commands import find

            found = find(self.commands, command[1:]) if command.startswith("/") else None
            if found is None:
                self._say("Commands", f"Unknown command {command}.\n{self.SLASH}", "#ffd08a")
                return
            if len(argument) > MAX_PROMPT:
                self._state(f"Arguments must be under {MAX_PROMPT} characters.")
                return
            self._run_command(found, argument)

    def _run_command(self, command, argument):
        from .commands import CommandError, render

        if self.busy or self.screen is not self.screen_stack[0]:
            self._say("Commands", "Wait for the current operation first.", "#ffd08a")
            return
        if self.read_only:
            self._say("Commands", "Prompt commands need a model session; no task was sent.",
                      "#ffd08a")
            return
        try:
            task = render(command, argument)
        except CommandError as exc:
            self._say("Commands", str(exc), "#ffd08a")
            return
        if not task:
            self._say("Commands", f"/{command.name} has an empty body.", "#ffd08a")
            return
        self.busy = True
        self.budget.reset(self.token_budget)
        self._reset_live()
        self._say("You", f"/{command.name} {argument}".strip())
        self._record("command", f"/{command.name} {argument}".strip())
        self._state("RUNNING  /  Awaiting model and tools")
        self._run_task(task)

    def action_undo(self):
        if self.busy or self.screen is not self.screen_stack[0]:
            return
        if not self.history:
            self._say("Proposal", "Nothing to undo.", "#b7cbc2")
            return
        snapshot, previous = self.history.pop()
        self.repo.restore(snapshot)
        self.previous = previous
        self._state(f"UNDONE  /  {len(self.history)} step(s) remain")
        self._say("Proposal", "Restored the previous proposal state.")
        self._record("undo", "Reverted the last accepted proposal step.")

    @work(exit_on_error=False)
    async def _list_sessions(self):
        if self.store is None:
            self._say("Sessions", "Session persistence is disabled.", "#b7cbc2")
            return
        try:
            sessions = await asyncio.to_thread(self.store.list, self.repo.root)
        except Exception:
            self._say("Sessions", "The saved sessions could not be read.", "#ffd08a")
            return
        lines = [f"{item.get('id', '')}  {item.get('updated', '')}  "
                 f"{str(item.get('task', ''))[:60]}" for item in sessions]
        self._say("Sessions", "\n".join(lines) or "No saved sessions for this repository.")

    def action_fork_session(self):
        if self.busy or self.screen is not self.screen_stack[0]:
            return
        if self.store is None:
            self._say("Sessions", "Session persistence is disabled.", "#b7cbc2")
            return
        if self.session is None:
            self._say("Sessions", "No session is open yet.", "#b7cbc2")
            return
        try:
            forked = self.store.fork(self.session.id)
        except ValueError as exc:
            self._say("Sessions", session_error(exc), "#ffd08a")
            return
        self.session = forked
        self._session_generation += 1
        self.query_one("#session-id", Static).update(forked.id)
        self._say("Sessions", f"Forked {forked.id}; continuing in the new branch.",
                  "#b7cbc2")
        self._state(f"FORKED  /  {forked.id}")

    def action_review_proposal(self):
        if self.busy or self.screen is not self.screen_stack[0]:
            return
        if not self.repo.patch():
            self._say("Review", "There is no proposed patch to review.", "#b7cbc2")
            return
        self.busy = True
        self._state("REVIEW  /  Asking the model to review the proposal")
        self._review_proposal()

    @work
    async def _review_proposal(self):
        from .core import ProviderError
        from .review import review

        patch = self.repo.patch()
        review_model = self.review_model or self.model
        if self.review_factory is not None:
            reviewer = self.review_factory()
        elif self.chat_factory is not None:
            reviewer = self.chat_factory(review_model)
        else:
            self.busy = False
            self._say("Review", "Set the provider key to run a model review.", "#ffd08a")
            self._state("REVIEW UNAVAILABLE")
            return
        try:
            result = await asyncio.to_thread(review, reviewer, patch,
                                             model=review_model, retries=self.retries,
                                             budget=self.budget)
        except ProviderError as exc:
            result = {"status": "provider_error", "error": str(exc),
                      "findings": [], "text": "", "tokens": None,
                      "model": review_model}
        self.last_review = result
        self.busy = False
        self._record("review", f"{len(result.get('findings') or [])} finding(s)",
                     status=result["status"],
                     tokens=result.get("tokens") or 0,
                     model=review_model)
        if result["status"] != "ok":
            if result.get("error"):
                self._say("Review", f"Review failed: {result['error']}", "#ffd08a")
                self._state("REVIEW FAILED")
            else:
                self._say("Review", f"Review was not parsed: {result['status']}",
                          "#ffd08a")
                if result.get("text"):
                    self._say("Review output", result["text"], "#b7cbc2")
                self._state("REVIEW UNPARSED")
            return
        findings = result["findings"]
        if not findings:
            self._say("Review", "No concrete defects were reported.")
        else:
            self._say("Review", "\n".join(
                f"[{_one(item['severity'])}] {_one(item['file'])}:"
                f"{_one(item['line'])} {_one(item['title'])} — "
                f"{_one(item['detail'])}"
                for item in findings), "#ffd08a")
        self._state(f"REVIEW  /  {len(findings)} finding(s)")

    def _state(self, status):
        self.query_one("#status", Static).update(_terminal(status))
        self.query_one("#send", Button).disabled = self.busy or self.read_only
        self.query_one("#review", Button).disabled = self.busy or self.read_only or not self.repo.patch()

    def _reset_live(self):
        self._live_buffer = ""
        self.query_one("#live", Static).update("")

    def _run_event(self, event):
        kind = event.get("type")
        if kind == "request":
            text = f"RUNNING  /  Waiting for the model (turn {event.get('turn')})"
        elif kind == "tool":
            text = f"RUNNING  /  {event.get('name')} → {event.get('status')}"
        elif kind == "retry":
            text = f"RETRY  /  {event.get('code')} (attempt {event.get('attempt')})"
        elif kind == "plan":
            items = event.get("items") or []
            self.plan = list(items)
            try:
                self.call_from_thread(self._show_plan)
            except RuntimeError:
                pass
            done = sum(1 for item in items if item.get("status") == "completed")
            current = next((item.get("step") for item in items
                            if item.get("status") == "in_progress"), "")
            text = f"PLAN  /  {done}/{len(items)} done"
            if current:
                text += f" · {current}"
        elif kind == "approval":
            text = "RUNNING  /  Waiting for approval"
        else:
            return
        try:
            self.call_from_thread(self._state, text)
        except RuntimeError:
            pass

    def _stream_piece(self, piece):
        try:
            self.call_from_thread(self._live_append, piece)
        except RuntimeError:
            pass

    def _live_append(self, piece):
        self._live_buffer = (getattr(self, "_live_buffer", "") + _terminal(piece))[-2000:]
        self.query_one("#live", Static).update(self._live_buffer)

    def _record(self, kind, text, **meta):
        if self.store is None or self.session is None:
            return
        try:
            self.store.append(self.session, kind, text, **meta)
        except ValueError:
            self._say("Session", "Transcript could not be saved.", "#ffd08a")

    @on(Composer.MentionRequested)
    def mention_requested(self):
        self.action_files()

    @on(Button.Pressed, "#send")
    def send_button(self):
        self.action_send_task()

    @on(Button.Pressed, "#review")
    def review_button(self):
        self.action_review_patch()

    SLASH = ("/help  /diff  /apply  /review  /file  /model [id]  /verify  /fix  "
             "/search TEXT  /sessions  /fork  /resume [id]  /undo  /reset  /quit"
             "  ·  /name runs a prompt command")

    def action_send_task(self):
        if self.busy or self.screen is not self.screen_stack[0]:
            return
        composer = self.query_one("#composer", TextArea)
        task = composer.text.strip()
        snapshot = self.composer_prompt_text
        from_prompt = bool(self.composer_from_prompt and snapshot and task.startswith(snapshot))
        if task.startswith("/") and not from_prompt:
            self.composer_from_prompt = False
            self.composer_prompt_text = ""
            composer.load_text("")
            self._slash(task)
            return
        if self.read_only:
            return
        if not task or len(task) > MAX_PROMPT:
            self._state(f"Enter a task of 1–{MAX_PROMPT} characters.")
            return
        self.composer_from_prompt = False
        self.composer_prompt_text = ""
        self._session_generation += 1
        self.busy = True
        self.budget.reset(self.token_budget)
        self._reset_live()
        composer.load_text("")
        composer.focus()
        self._say("You", task)
        self._record("user", task)
        self._state("RUNNING  /  Awaiting model and tools")
        self._run_task(task)

    @work
    async def _run_task(self, task):
        previous = "\n".join(self.previous)[-MAX_PROMPT:]
        prompt = f"Previous user revisions:\n{previous}\n\nCurrent task:\n{task}" if previous else task
        snapshot = self.repo.snapshot()
        previous_plan = list(self.plan)
        before = (snapshot, list(self.previous))
        self.cancel_event = threading.Event()
        try:
            if self.extensions is None:
                result, _ = await asyncio.to_thread(solve, self.chat_factory(self.model), self.repo,
                                                    prompt, on_text=self._stream_piece,
                                                    cancel=self.cancel_event,
                                                    approve=self.approve_tool,
                                                    budget=self.budget, retries=self.retries,
                                                    on_event=self._run_event)
            else:
                result, _ = await asyncio.to_thread(
                    solve, self.chat_factory(self.model), self.repo, prompt,
                    belt=self.extensions.belt, system=self.extensions.system,
                    max_turns=self.max_turns, max_calls=self.max_calls,
                    context_budget=self.context_budget, cancel=self.cancel_event,
                    on_text=self._stream_piece, approve=self.approve_tool,
                    token_budget=self.token_budget, budget=self.budget,
                    retries=self.retries, on_event=self._run_event)
        except (OSError, ValueError):
            result = None
        if result is None or result.status != "ok":
            self.repo.restore(snapshot)
            self.plan = previous_plan
            self._show_plan()
        else:
            self.previous = (self.previous + [task])[-4:]
            self.history = (self.history + [before])[-10:]
        self.query_one("#live", Static).update("")
        usage = take_usage(self.extensions.subagent_usage
                           if self.extensions is not None else None)
        delegated = usage_totals(usage)
        if result:
            trace = "  ·  ".join(f'{step.get("tool", "provider")}: {step["status"]}' for step in result.trace)
            if trace:
                self._say("Tools", trace, "#b7cbc2")
            if result.text:
                self._say("Nalar", result.text)
            self._record("assistant", result.text or result.status,
                         status=result.status,
                         tokens=max(0, (result.tokens or 0) - sum(delegated.values())),
                         model=self.model)
        for label, amount in delegated.items():
            self._record("subagent", f"{amount} tokens",
                         model=label, tokens=amount)
        self.last_run = result
        if result is not None and result.status == "ok":
            self.plan = list(result.plan)
            self._show_plan()
        self._show_run_stats()
        self.busy = False
        if (self.permission == "auto" and result and result.status == "ok"
                and self.repo.patch() and not self.read_only):
            self._say("Policy", "Auto-apply is enabled; applying the proposal.", "#ffd08a")
            self._reviewed(True)
            return
        if result and result.status == "ok":
            summary = ("PLAN READY  /  Read-only" if self.permission == "plan" or self.read_only
                       else "PROPOSAL READY" if self.repo.patch() else "NO PATCH")
        else:
            summary = "TASK FAILED  /  Previous proposal kept"
        if result:
            summary += f"  ·  {result.turns} turns  ·  {result.calls} calls"
            if result.tokens is not None:
                summary += f"  ·  {result.tokens} tokens"
        self._state(summary)

    def action_review_patch(self):
        if self.busy or self.read_only or self.screen is not self.screen_stack[0]:
            return
        if not (diff := self.repo.patch()):
            return
        self.push_screen(PatchReview(diff), self._reviewed)

    async def action_quit(self):
        if self.busy:
            self.notify("Wait for the current operation before quitting.")
            return
        await super().action_quit()

    def action_undo_apply(self):
        if self.busy or self.screen is not self.screen_stack[0] or not self.applied:
            return
        self.busy = True
        self._state("UNDOING  /  Checking the recorded patch")
        self._undo_apply()

    @work
    async def _undo_apply(self):
        from .apply import read_last_apply

        try:
            record = await asyncio.to_thread(read_last_apply, self.repo)
            await asyncio.to_thread(apply_reviewed, self.repo, record["patch"],
                                    reverse=True, allowed_files=record["files"])
        except ValueError as exc:
            self._say("Undo refused", str(exc), "#ffd08a")
            status = "UNDO REFUSED  /  Applied patch kept"
        else:
            self.repo.restore({}, {})
            self.applied = False
            self.read_only = self.chat_factory is None
            self.query_one("#composer", TextArea).read_only = self.read_only
            self._say("Undone", "Reverted the applied patch; the working tree is restored.")
            self._record("reverted", "Applied patch reverted.")
            status = "UNDONE  /  Working tree restored"
        self.busy = False
        self._state(status)

    def _reviewed(self, approved):
        if not approved:
            return
        kept = total = None
        if isinstance(approved, dict):
            patch = approved.get("patch", "")
            kept = approved.get("kept")
            total = approved.get("total")
        elif approved is True:
            patch = self.repo.patch()
        else:
            patch = approved
        self.busy = True
        self._state("APPLYING  /  Checking Git base and working tree")
        from .secrets import scan_diff

        report = scan_diff(patch)
        findings = list(report["findings"])
        if report["truncated"]:
            findings.append({"file": "(whole patch)", "line": 1,
                             "kind": "credential scan truncated"})
        if findings:
            self.busy = False
            self._state("APPLY  /  Review credentials")
            self.push_screen(
                CredentialReview(findings, truncated=report["truncated"]),
                lambda ok: self._credential_decided(ok, patch, kept, total))
            return
        self._apply(patch, kept, total)

    def _credential_decided(self, confirmed, patch, kept, total):
        if not confirmed:
            self._state("READY  /  Apply cancelled")
            self._say("Apply", "Cancelled; the proposal is retained.", "#b7cbc2")
            return
        self.busy = True
        self._state("APPLYING  /  Checking Git base and working tree")
        self._apply(patch, kept, total, allow_secrets=True)

    @work
    async def _apply(self, patch, kept=None, total=None, allow_secrets=False):
        try:
            await asyncio.to_thread(apply_reviewed, self.repo, patch,
                                    allow_secrets=allow_secrets)
        except ValueError as exc:
            self._say("Apply refused", str(exc), "#ffd08a")
            status = "NOT APPLIED  /  Proposal retained"
        else:
            self.repo.restore({}, {})
            self.applied = True
            self.read_only = True
            self.history.clear()
            self.query_one("#composer", TextArea).read_only = True
            self._say("Applied", _applied_message(kept, total))
            self._record("applied", "Patch applied to the working tree.")
            status = "APPLIED  /  Tests not run"
        self.busy = False
        self._state(status)
