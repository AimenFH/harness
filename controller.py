"""Controller: the central agent loop.

The loop is:
    ask the model for an action -> validate it -> run the matching tool
    -> send the result back to the model -> repeat

The controller, not the model, decides what is allowed. The model only
sends requests; every request is checked here before anything runs.

The loop stops when:
  - the model says "done",
  - max_actions model replies have been used (action limit),
  - max_retries requests in a row were malformed or denied (retry limit),
  - a fatal error happens (e.g. Ollama is not reachable).
"""

import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional

from cancellation import CancelledError
from execution import TEST_COMMANDS, CommandResult, ExecutionError
from model_client import (EDIT_TOOLS, InvalidActionError, ModelError, initial_messages,
                          validate_tool_call)
from navigation import RepositoryNavigator
from repository_tools import AccessDeniedError, RepositoryError
from session import Checkpoint, file_digest
from task_record import MALFORMED, ScopePolicy, TaskRecord, TaskSpec

# Possible values of RunResult.status
DONE = "done"
ACTION_LIMIT = "action_limit_reached"
RETRY_LIMIT = "retry_limit_reached"
CANCELLED = "cancelled"
ERROR = "error"

# Why a run can stop. Printed by the CLI so the stopping rule is never implicit.
STOPPING_RULES = {
    DONE: "the model called done and every change had a passing check",
    ACTION_LIMIT: "the action limit was reached; nothing further was executed",
    RETRY_LIMIT: "too many malformed or denied requests in a row",
    CANCELLED: "the run was cancelled; no further action was executed",
    ERROR: "a fatal error (for example the model was unreachable)",
}

# Possible outcomes of executing one action
OK = "ok"
DENIED = "denied"
FAILED = "failed"
# An action that was started but whose result was never recorded, because the
# process stopped in between. Its effect is unknown, so it is never replayed.
UNCERTAIN = "uncertain"

TRUNCATION_MARKER = "[OUTPUT TRUNCATED]"


@dataclass
class Counters:
    """Numbers the controller keeps during a run."""

    actions: int = 0      # model replies received; every reply counts
    executed: int = 0     # tool calls that ran successfully
    malformed: int = 0    # replies that were not a valid action
    denied: int = 0       # actions refused by a permission/safety check
    tool_errors: int = 0  # allowed actions whose tool failed (e.g. file not found)
    truncated: int = 0    # tool outputs that were cut to max_output_chars


@dataclass
class RunResult:
    """What happened during one run of the harness."""

    status: str          # DONE, ACTION_LIMIT, RETRY_LIMIT, CANCELLED or ERROR
    summary: str         # the model's summary, or why we stopped
    counters: Counters
    max_actions: int
    changed_files: list = field(default_factory=list)
    last_check: Optional[CommandResult] = None
    record: Optional[TaskRecord] = None  # the task specification and every action
    session: object = None               # the saved Session, when one is in use

    def stopping_rule(self):
        """Why the run stopped, in words."""
        return STOPPING_RULES.get(self.status, self.status)

    @property
    def succeeded(self):
        """True if the model finished and the last check (if any) passed."""
        check_ok = self.last_check is None or self.last_check.succeeded
        return self.status == DONE and check_ok

    def counters_text(self):
        """One line with all counters, e.g. 'actions 5/20 | executed 4 | ...'."""
        c = self.counters
        return (f"actions {c.actions}/{self.max_actions} | executed {c.executed} | "
                f"malformed {c.malformed} | denied {c.denied} | "
                f"tool errors {c.tool_errors} | truncated {c.truncated}")

    def report(self):
        """Return a short human-readable report of the run."""
        if self.last_check is None:
            check = "not run"
        else:
            check = f"{self.last_check.name} {self.last_check.status()}"
        return "\n".join([
            f"Result:  {self.status}",
            f"Summary: {self.summary}",
            f"Changed files: {', '.join(self.changed_files) or 'none'}",
            f"Last check: {check}",
            f"Stopped because: {self.stopping_rule()}",
            f"Counters: {self.counters_text()}",
        ])


class Controller:
    """Runs the loop between the model and the tools."""

    def __init__(self, config, model, tools, executor, log=print, navigator=None,
                 spec=None, cancel=None, session=None, store=None):
        """
        config:    Config (mode, limits, check_command)
        model:     anything with request_action(messages) -> Action
        tools:     RepositoryTools for the target repository
        executor:  ExecutionEnvironment for the target repository
        log:       function used for progress lines (print by default)
        navigator: RepositoryNavigator; by default one built on `tools`, so
                   navigation has exactly the same path restrictions
        spec:      TaskSpec saved with the run. By default one is built from
                   `config`, so the scope comes from config.allowed_changes.
        cancel:    CancelToken checked between steps; None = never cancelled
        session:   Session to record this run in; None = nothing is saved
        store:     SessionStore the session is written to after every step
        """
        self.config = config
        self.model = model
        self.tools = tools
        self.executor = executor
        self.log = log
        self.navigator = navigator or RepositoryNavigator(
            tools, timeout_seconds=config.command_timeout_seconds)
        self.spec = spec or TaskSpec(
            request=config.task, project=Path(config.root).name, root=str(config.root),
            mode=config.mode, allowed_changes=tuple(config.allowed_changes),
            checks=(config.check_command,),
            limits={"max_actions": config.max_actions, "max_retries": config.max_retries},
        )
        self.scope = ScopePolicy(self.spec.allowed_changes)
        self.cancel = cancel
        self.session = session
        self.store = store
        self.failed_actions = set()  # actions that already failed; see _repeat_problem
        self.repeats = 0             # denied requests to repeat a failed action
        self.partly_read = set()     # files the model has only seen truncated
        self.read_files = set()      # files the model has seen in full this run
        # Fresh run state, so execute() and check_permission() also work when
        # they are called directly rather than through run().
        self.counters = Counters()
        self.changed_files = []
        self.last_check = None
        self.checked_since_edit = False
        self.step = 0
        self.record = TaskRecord(spec=self.spec)

        if config.check_command not in executor.available_commands():
            raise ValueError(f"Unknown check_command {config.check_command!r}")

        # Tool name -> method that carries it out and returns text for the model.
        self.handlers = {
            "list_files": self._list_files,
            "read_file": self._read_file,
            "search": self._search,
            "find_files": self._find_files,
            "list_symbols": self._list_symbols,
            "find_symbol": self._find_symbol,
            "module_dependencies": self._module_dependencies,
            "git_state": self._git_state,
            "replace_in_file": self._replace_in_file,
            "edit_file": self._edit_file,
            "run_check": self._run_check,
        }

    # ----- the loop ----------------------------------------------------------

    def run(self, task, context_files=()):
        """Work on `task` until done, a limit is reached, or a fatal error.

        `context_files` are paths the user selected; their content is sent
        with the task so the model does not have to ask for them first.
        """
        self.counters = Counters()
        self.changed_files = []
        self.last_check = None
        self.partly_read = set()  # files the model has only seen truncated
        self.read_files = set()   # files the model has seen in full this run
        self.checked_since_edit = False  # a check PASSED after the last file change
        self.failed_actions = set()
        self.repeats = 0
        self.record = TaskRecord(spec=self.spec)
        messages = initial_messages(task, self.config.mode, self._check_names(),
                                    self.selected_files_text(context_files))
        self._checkpoint(messages, 0, status="running")
        return self._loop(messages, rejected_in_a_row=0)

    def resume(self, session):
        """Carry on a saved session from its checkpoint.

        The caller must have checked with session.check_resume() that this is
        safe; this method restores the state and continues the loop. The
        consumed limits come back with it, so a resumed run cannot get more
        actions than the task was given. An uncertain action is NOT replayed:
        it is recorded as such and the model is told about it instead.
        """
        saved = session.checkpoint
        self.counters = Counters(**{name: saved.counters.get(name, 0)
                                    for name in _COUNTER_NAMES})
        self.changed_files = list(saved.changed_files)
        self.partly_read = {self._key(path) for path in saved.partly_read}
        self.partly_read.discard(None)
        self.read_files = {self._key(path) for path in saved.read_files}
        self.read_files.discard(None)
        self.checked_since_edit = saved.checked_since_edit
        self.failed_actions = {tuple(entry) for entry in saved.failed_actions}
        self.repeats = saved.repeats
        self.last_check = None  # the CommandResult itself is not saved; see below
        self.record = TaskRecord(spec=self.spec, actions=list(session.actions))
        self.session = session

        messages = list(saved.messages)
        # A check result is saved as text, not as a CommandResult. Reload it as
        # a stand-in so done_problem() still refuses `done` after a failed one.
        if saved.last_check:
            self.last_check = _check_placeholder(saved.last_check)
        if session.uncertain:
            self.record.add(saved.step, session.uncertain.get("tool", "?"),
                            session.uncertain.get("arguments", {}),
                            outcome=UNCERTAIN,
                            detail="started but never confirmed finished; not replayed")
            messages.append({"role": "user", "content": _uncertain_notice(session.uncertain)})
            self.log(f"[resume] uncertain action not replayed: "
                     f"{session.uncertain.get('tool', '?')}")
        self.log(f"[resume] step {saved.step}, "
                 f"{self.counters.actions}/{self.config.max_actions} actions used")
        return self._loop(messages, rejected_in_a_row=saved.rejected_in_a_row)

    def _loop(self, messages, rejected_in_a_row=0):
        """The agent loop. Used both for a fresh run and for a resumed one."""
        while True:
            # Cancellation: checked BEFORE asking, so no further model call and
            # no further tool runs once a stop has been requested.
            if self.cancel is not None and self.cancel.cancelled:
                return self._stop(CANCELLED, f"Stopped: {self.cancel.reason}. "
                                             "No further actions were executed.")

            # Action limit: checked BEFORE asking, so nothing runs past it.
            if self.counters.actions >= self.config.max_actions:
                return self._stop(
                    ACTION_LIMIT,
                    f"Stopped: action limit reached ({self.config.max_actions} actions). "
                    "No further actions were executed.",
                )
            self.counters.actions += 1
            step = self.counters.actions
            self.step = step

            # 1. Ask the model what to do next.
            try:
                action = self.model.request_action(messages, self.cancel)
            except CancelledError as error:
                # Cancelled while waiting for the model. Whatever it was about
                # to say is discarded: a late reply cannot start an action.
                return self._stop(CANCELLED, f"Stopped: {error.reason}. The model "
                                             "request was abandoned and no action ran.")
            except ModelError as error:
                # Record it too: the brief expects a reopened session to show
                # its errors, and this one is why the run ended.
                self.record.add(step, "(model error)", outcome=FAILED, detail=str(error))
                return self._stop(ERROR, f"Stopped: model error: {error}")
            except InvalidActionError as error:
                # The model answered, but badly. Tell it and let it try again.
                self.counters.malformed += 1
                rejected_in_a_row += 1
                self.log(f"[{step}] malformed reply: {error}")
                self.record.add(step, "(malformed reply)", outcome=MALFORMED,
                                detail=str(error))
                messages.append({"role": "assistant", "content": error.raw})
                messages.append({"role": "user", "content":
                                 f"ERROR: invalid action: {error}\n"
                                 "Reply with exactly one JSON action."})
            else:
                self.log(f"[{step}] {describe(action)}")
                messages.append({"role": "assistant", "content": action.raw or _as_json(action)})

                # 2. Finished? Only if the change has been checked.
                if action.tool == "done":
                    problem = self.done_problem()
                    if not problem:
                        self.record.add(step, "done", outcome=OK,
                                        detail=action.summary or "")
                        return self._stop(DONE, action.summary or "(no summary given)",
                                          quiet=True)
                    outcome, text = self._deny(problem)
                    self.record.add(step, "done", outcome=DENIED, detail=problem)
                else:
                    # Save a checkpoint naming the action we are ABOUT to run.
                    # If the process dies now, the reopened session still has
                    # it pending, so it is reported as uncertain, not replayed.
                    self._checkpoint(messages, rejected_in_a_row, pending=action)
                    # 3. Validate and run the tool, 4. send the result back.
                    outcome, text = self.execute(action)
                messages.append({"role": "user", "content": text})
                rejected_in_a_row = rejected_in_a_row + 1 if outcome == DENIED else 0

            # The step is finished and its result is recorded: no action is in
            # flight any more, so the next checkpoint carries no `pending`.
            self._checkpoint(messages, rejected_in_a_row)

            # Retry limit: too many malformed/denied requests in a row.
            if rejected_in_a_row >= self.config.max_retries:
                return self._stop(
                    RETRY_LIMIT,
                    f"Stopped: {rejected_in_a_row} malformed or denied requests in a row "
                    f"(limit {self.config.max_retries}).",
                )
            # Retry limit, second form: the model keeps asking for an action that
            # already failed, so it is making no progress. These attempts are not
            # necessarily next to each other -- a model typically re-reads the file
            # in between -- so they are counted on their own.
            if self.repeats >= self.config.max_retries:
                return self._stop(
                    RETRY_LIMIT,
                    f"Stopped: the model asked {self.repeats} times for an action that "
                    f"had already failed (limit {self.config.max_retries}) and the "
                    "repository did not change in between. It is not making progress.",
                )

    # ----- basic context -----------------------------------------------------

    def selected_files_text(self, paths):
        """Text with the content of the files the user selected for the task.

        Files are read through the same safe tools as the model's read_file,
        so a path outside the root is refused here too. Each file is cut to
        max_output_chars; a cut file may not be rewritten with edit_file.
        """
        if not paths:
            return ""
        parts = ["Files selected by the user:"]
        for path in paths:
            try:
                text = self.tools.read_file(path)
            except RepositoryError as error:
                self.log(f"[context] could not include {path}: {error}")
                parts.append(f"--- {path} ---\n(not included: {error})")
                continue
            text, was_cut = truncate(text, self.config.max_output_chars)
            if was_cut:
                self.partly_read.add(self._key(path))
            else:
                self.read_files.add(self._key(path))
            self.log(f"[context] {path}{' (truncated)' if was_cut else ''}")
            parts.append(f"--- {path} ---\n{text}")
        return "\n\n".join(parts)

    # ----- validation and dispatch -------------------------------------------

    def done_problem(self):
        """Return a reason if the model may not finish yet, otherwise None.

        After changing files, the model must run a check that passes before
        it calls done. Without changes (e.g. readonly mode) done is always
        allowed, so the model can still report that it could not fix the task.
        """
        if not self.changed_files or self.checked_since_edit:
            return None
        if self.last_check is None:
            return ("Not finished: you changed files but have not run a check. "
                    "Call run_check before done.")
        return (f"Not finished: the last check {self.last_check.status()}. "
                "Fix the problem and run the check again before done.")

    def check_permission(self, action):
        """Return a reason if `action` must be refused, otherwise None."""
        if self.cancel is not None and self.cancel.cancelled:
            # Belt and braces: the loop already stops on cancellation, but if
            # anything reaches here afterwards it must not touch a file.
            return (f"Permission denied: the run was cancelled ({self.cancel.reason}). "
                    "No further action is executed.")
        try:
            validate_tool_call(action.tool, action.arguments)
        except InvalidActionError as error:
            return str(error)

        if action.tool not in self.handlers:
            return f"Tool {action.tool!r} cannot be executed"
        if "path" in action.arguments:
            try:
                self.tools.resolve_path(action.arguments["path"])
            except RepositoryError as error:  # outside the root, or not a valid path
                return str(error)
        if action.tool in EDIT_TOOLS and self.config.mode != "edit":
            return f"Permission denied: {action.tool} is not allowed in readonly mode"
        if action.tool in EDIT_TOOLS:
            # File scope is enforced here, in the harness, BEFORE anything is
            # written -- not by a script that inspects the damage afterwards.
            relative = self._relative(action.arguments["path"])
            if relative is not None and not self.scope.allows(relative):
                return self.scope.refusal(relative)
        if action.tool == "edit_file":
            key = self._key(action.arguments["path"])
            if key in self.partly_read:
                return ("Permission denied: this file was only shown truncated, so "
                        "editing it could delete the part you have not seen")
            # edit_file replaces the WHOLE file. Doing that to a file the model
            # has never seen deletes code it does not know about: a real model
            # wrote "# existing code" as a placeholder and destroyed the module.
            if key is not None and key.is_file() and key not in self.read_files:
                return ("Permission denied: you have not read this file in this run, "
                        "so replacing all of it would delete code you have not seen. "
                        "Call read_file first, then change only the lines that need "
                        "changing with replace_in_file.")
        if action.tool == "run_check":
            name = action.arguments.get("name", self.config.check_command)
            if name not in self.executor.available_commands():
                return f"Unknown check {name!r}. Available: {', '.join(self._check_names())}"
        return self._repeat_problem(action)

    def _repeat_problem(self, action):
        """Refuse an action that already failed with exactly these arguments.

        Without this, a model that cannot get `old` right retries the same
        replace_in_file until the action limit runs out: every attempt is a
        tool error, and tool errors do not count towards the retry limit, so
        nothing stops it. Repeating an identical failed action cannot succeed
        while the repository is unchanged, so it is denied instead -- and a
        denial does count, which makes the retry limit bite.

        The memory is cleared whenever a file changes (see _record_change),
        because then the same action may well work.
        """
        if action.tool == "run_check":  # its result legitimately changes
            return None
        if _action_key(action) not in self.failed_actions:
            return None
        self.repeats += 1
        return ("Permission denied: this exact action already failed once and the "
                "repository has not changed since, so repeating it cannot work. Read "
                "the file again and copy the text exactly, or use a different tool.")

    def execute(self, action):
        """Validate and run one action.

        Returns (outcome, text): outcome is OK, DENIED or FAILED, and text is
        what gets sent back to the model.
        """
        problem = self.check_permission(action)
        if problem:
            self._note(action, DENIED, problem)
            return self._deny(problem)

        try:
            text = self.handlers[action.tool](**action.arguments)
        except AccessDeniedError as error:
            self._note(action, DENIED, str(error))
            return self._deny(str(error))
        except (RepositoryError, ExecutionError) as error:
            self.counters.tool_errors += 1
            self.failed_actions.add(_action_key(action))
            self.log(f"    error: {error}")
            self._note(action, FAILED, str(error))
            return FAILED, f"ERROR: {error}"

        self._note(action, OK, text)
        self.counters.executed += 1
        text, was_cut = truncate(text, self.config.max_output_chars)
        if was_cut:
            self.counters.truncated += 1
            self.log(f"    output truncated to {self.config.max_output_chars} characters")
        if action.tool == "read_file":
            key = self._key(action.arguments["path"])
            if was_cut:
                self.partly_read.add(key)
                self.read_files.discard(key)
                text += "\nThis file cannot be edited because you have not seen all of it."
            else:
                self.partly_read.discard(key)
                self.read_files.add(key)
        return OK, f"Result of {action.tool}:\n{text}"

    def _deny(self, reason):
        self.counters.denied += 1
        self.log(f"    denied: {reason}")
        return DENIED, f"DENIED: {reason}"

    # ----- one method per tool: run it and turn the result into text --------

    def _list_files(self, path="."):
        files = self.tools.list_files(path)
        return "\n".join(files) if files else f"No files in {path!r}"

    def _read_file(self, path):
        return f"--- {path} ---\n{self.tools.read_file(path)}"

    def _search(self, query, path="."):
        matches = self.tools.search(query, path)
        if not matches:
            return f"No matches for {query!r}"
        return f"{len(matches)} match(es):\n" + "\n".join(matches)

    # Navigation (read-only). Errors are RepositoryErrors: reported, never fatal.

    def _find_files(self, pattern, path="."):
        return "\n".join(self.navigator.find_files(pattern, path))

    def _list_symbols(self, path):
        return f"Symbols in {path}:\n" + "\n".join(self.navigator.list_symbols(path))

    def _find_symbol(self, name, path="."):
        return f"Definitions of {name!r}:\n" + "\n".join(self.navigator.find_symbol(name, path))

    def _module_dependencies(self, path):
        return "\n".join(self.navigator.module_dependencies(path))

    def _git_state(self):
        return "\n".join(self.navigator.git_state())

    def _replace_in_file(self, path, old, new):
        message = self.tools.replace_in_file(path, old, new)
        self._record_change(path)
        return message

    def _edit_file(self, path, content):
        message = self.tools.edit_file(path, content)
        key = self._key(path)
        self.partly_read.discard(key)  # the model wrote all of it
        self.read_files.add(key)       # ... so it knows what is in it now
        self._record_change(path)
        return message

    def _record_change(self, path):
        self.checked_since_edit = False  # the old check result no longer counts
        self.failed_actions.clear()      # the file changed: a failed action may now work
        self.repeats = 0                 # real progress was made
        if path not in self.changed_files:
            self.changed_files.append(path)

    def _run_check(self, name=None):
        result = self.executor.run(name or self.config.check_command, cancel=self.cancel)
        self.last_check = result  # keeps the FULL output, even if truncated below
        if result.name in TEST_COMMANDS:
            self.checked_since_edit = result.succeeded
        self.log(f"    {result.status()}")
        return result.report()

    # ----- helpers -------------------------------------------------------------

    def _check_names(self):
        """Available checks, with the default one first."""
        others = [n for n in self.executor.available_commands() if n != self.config.check_command]
        return [self.config.check_command] + others

    def _key(self, path):
        """A single name for a file, so 'a.py' and './a.py' count as the same."""
        try:
            return self.tools.resolve_path(path)
        except RepositoryError:
            return None

    def _relative(self, path):
        """`path` relative to the repository root, or None if it is not inside it."""
        try:
            return self.tools.resolve_path(path).relative_to(self.tools.root).as_posix()
        except (RepositoryError, ValueError):
            return None

    def _note(self, action, outcome, detail):
        """Record one tool action and its result in the task record."""
        record = getattr(self, "record", None)
        if record is None:
            return
        check = self.last_check.status() if (action.tool == "run_check"
                                             and self.last_check is not None) else ""
        record.add(getattr(self, "step", len(record.actions) + 1), action.tool,
                   action.arguments, outcome=outcome, detail=detail, check=check)

    # ----- sessions ----------------------------------------------------------

    def _checkpoint(self, messages, rejected_in_a_row, pending=None, status=None,
                    keep_pending=False):
        """Save the run's state, durably and atomically. Does nothing without a store.

        Called twice per step: once naming the action about to run (`pending`),
        and once after its result has been recorded (`pending` cleared). That
        pair is what makes an interrupted action detectable afterwards.
        """
        if self.session is None or self.store is None:
            return None
        branch, commit, dirty = self.navigator.state_summary()
        self.session.checkpoint = Checkpoint(
            step=self.counters.actions,
            counters=vars(self.counters).copy(),
            messages=list(messages),
            changed_files=list(self.changed_files),
            file_digests={path: file_digest(self.tools.root / path)
                          for path in self.changed_files},
            read_files=sorted(self._relative_of(key) for key in self.read_files),
            partly_read=sorted(self._relative_of(key) for key in self.partly_read),
            failed_actions=[list(entry) for entry in sorted(self.failed_actions)],
            repeats=self.repeats,
            rejected_in_a_row=rejected_in_a_row,
            checked_since_edit=self.checked_since_edit,
            last_check=({"name": self.last_check.name,
                         "status": self.last_check.status()}
                        if self.last_check is not None else None),
            branch=branch, commit=commit, dirty=dirty,
            pending=({"step": self.counters.actions, "tool": pending.tool,
                      "arguments": dict(pending.arguments)} if pending
                     else (self.session.checkpoint.pending if keep_pending else None)),
        )
        self.session.actions = list(self.record.actions)
        if status:
            self.session.status = status
        return self.store.save(self.session)

    def _relative_of(self, key):
        """Turn a resolved path back into a root-relative one for saving."""
        try:
            return Path(key).relative_to(self.tools.root).as_posix()
        except (TypeError, ValueError):
            return str(key)

    def _stop(self, status, summary, quiet=False):
        if not quiet:
            self.log(summary)
        record = getattr(self, "record", None)
        if record is not None:
            record.finish(status, summary)
        if self.session is not None and self.store is not None:
            self.session.summary = summary
            # Keep any action still marked pending: stopping must never turn an
            # unknown outcome into a known one.
            self._checkpoint(list(self.session.checkpoint.messages), 0, status=status,
                             keep_pending=True)
        return RunResult(status, summary, self.counters, self.config.max_actions,
                         list(self.changed_files), self.last_check, record,
                         self.session)


def truncate(text, limit):
    """Cut `text` to `limit` characters and add a marker. Returns (text, was_cut)."""
    if len(text) <= limit:
        return text, False
    marker = f"{TRUNCATION_MARKER} showed the first {limit} of {len(text)} characters."
    return f"{text[:limit]}\n{marker}", True


def describe(action):
    """One-line description for progress output, e.g. 'read_file orders/pricing.py'."""
    args = action.arguments
    detail = (args.get("query") or args.get("pattern") or args.get("name")
              or args.get("path") or "")
    return f"{action.tool} {detail}".strip()


def _as_json(action):
    """Rebuild the JSON text of an action (used if the original reply is missing)."""
    return json.dumps({"tool": action.tool, "arguments": action.arguments})


def _action_key(action):
    """A hashable identity for one action, so an identical repeat is recognised."""
    return action.tool, json.dumps(action.arguments, sort_keys=True)


# Counters() field names, used to restore consumed limits on resume.
_COUNTER_NAMES = tuple(vars(Counters()))


def _check_placeholder(saved):
    """Stand in for a saved check result.

    Only the check's name and status line are saved, not its whole output, so
    a resumed run cannot show the old stdout. What matters for safety is
    whether it PASSED: done_problem() must still refuse `done` after a failure.
    """
    status = saved.get("status", "")
    passed = status.startswith("SUCCEEDED")
    return CommandResult(
        name=saved.get("name", "check"), command=[saved.get("name", "check")],
        exit_code=0 if passed else 1, stdout="", stderr="",
        timed_out=False, duration_seconds=0.0,
        no_tests_ran=False, sandbox="(from the saved session)",
    )


def _uncertain_notice(pending):
    """What the model is told about an action that was interrupted."""
    arguments = pending.get("arguments") or {}
    detail = ", ".join(f"{name}={value!r}" for name, value in arguments.items())
    return (
        "NOTE: this task was interrupted. The action "
        f"{pending.get('tool', '?')}({detail}) was started but its result was never "
        "recorded, so nobody knows whether it took effect. It has NOT been repeated. "
        "Check the current state of the files yourself before you change anything."
    )
