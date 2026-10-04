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
from typing import Optional

from execution import CommandResult, ExecutionError
from model_client import (EDIT_TOOLS, InvalidActionError, ModelError, initial_messages,
                          validate_tool_call)
from repository_tools import AccessDeniedError, RepositoryError

# Possible values of RunResult.status
DONE = "done"
ACTION_LIMIT = "action_limit_reached"
RETRY_LIMIT = "retry_limit_reached"
ERROR = "error"

# Possible outcomes of executing one action
OK = "ok"
DENIED = "denied"
FAILED = "failed"

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

    status: str          # DONE, ACTION_LIMIT, RETRY_LIMIT or ERROR
    summary: str         # the model's summary, or why we stopped
    counters: Counters
    max_actions: int
    changed_files: list = field(default_factory=list)
    last_check: Optional[CommandResult] = None

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
            f"Counters: {self.counters_text()}",
        ])


class Controller:
    """Runs the loop between the model and the tools."""

    def __init__(self, config, model, tools, executor, log=print):
        """
        config:   Config (mode, limits, check_command)
        model:    anything with request_action(messages) -> Action
        tools:    RepositoryTools for the target repository
        executor: ExecutionEnvironment for the target repository
        log:      function used for progress lines (print by default)
        """
        self.config = config
        self.model = model
        self.tools = tools
        self.executor = executor
        self.log = log

        if config.check_command not in executor.available_commands():
            raise ValueError(f"Unknown check_command {config.check_command!r}")

        # Tool name -> method that carries it out and returns text for the model.
        self.handlers = {
            "list_files": self._list_files,
            "read_file": self._read_file,
            "search": self._search,
            "replace_in_file": self._replace_in_file,
            "edit_file": self._edit_file,
            "run_check": self._run_check,
        }

    # ----- the loop ----------------------------------------------------------

    def run(self, task):
        """Work on `task` until done, a limit is reached, or a fatal error."""
        self.counters = Counters()
        self.changed_files = []
        self.last_check = None
        self.partly_read = set()  # files the model has only seen truncated
        rejected_in_a_row = 0
        messages = initial_messages(task, self.config.mode, self._check_names())

        while True:
            # Action limit: checked BEFORE asking, so nothing runs past it.
            if self.counters.actions >= self.config.max_actions:
                return self._stop(
                    ACTION_LIMIT,
                    f"Stopped: action limit reached ({self.config.max_actions} actions). "
                    "No further actions were executed.",
                )
            self.counters.actions += 1
            step = self.counters.actions

            # 1. Ask the model what to do next.
            try:
                action = self.model.request_action(messages)
            except ModelError as error:
                return self._stop(ERROR, f"Stopped: model error: {error}")
            except InvalidActionError as error:
                # The model answered, but badly. Tell it and let it try again.
                self.counters.malformed += 1
                rejected_in_a_row += 1
                self.log(f"[{step}] malformed reply: {error}")
                messages.append({"role": "assistant", "content": error.raw})
                messages.append({"role": "user", "content":
                                 f"ERROR: invalid action: {error}\n"
                                 "Reply with exactly one JSON action."})
            else:
                self.log(f"[{step}] {describe(action)}")
                messages.append({"role": "assistant", "content": action.raw or _as_json(action)})

                # 2. Finished?
                if action.tool == "done":
                    return self._stop(DONE, action.summary or "(no summary given)", quiet=True)

                # 3. Validate and run the tool, 4. send the result back.
                outcome, text = self.execute(action)
                messages.append({"role": "user", "content": text})
                rejected_in_a_row = rejected_in_a_row + 1 if outcome == DENIED else 0

            # Retry limit: too many malformed/denied requests in a row.
            if rejected_in_a_row >= self.config.max_retries:
                return self._stop(
                    RETRY_LIMIT,
                    f"Stopped: {rejected_in_a_row} malformed or denied requests in a row "
                    f"(limit {self.config.max_retries}).",
                )

    # ----- validation and dispatch -------------------------------------------

    def check_permission(self, action):
        """Return a reason if `action` must be refused, otherwise None."""
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
        if action.tool == "edit_file":
            if self._key(action.arguments["path"]) in self.partly_read:
                return ("Permission denied: this file was only shown truncated, so "
                        "editing it could delete the part you have not seen")
        if action.tool == "run_check":
            name = action.arguments.get("name", self.config.check_command)
            if name not in self.executor.available_commands():
                return f"Unknown check {name!r}. Available: {', '.join(self._check_names())}"
        return None

    def execute(self, action):
        """Validate and run one action.

        Returns (outcome, text): outcome is OK, DENIED or FAILED, and text is
        what gets sent back to the model.
        """
        problem = self.check_permission(action)
        if problem:
            return self._deny(problem)

        try:
            text = self.handlers[action.tool](**action.arguments)
        except AccessDeniedError as error:
            return self._deny(str(error))
        except (RepositoryError, ExecutionError) as error:
            self.counters.tool_errors += 1
            self.log(f"    error: {error}")
            return FAILED, f"ERROR: {error}"

        self.counters.executed += 1
        text, was_cut = truncate(text, self.config.max_output_chars)
        if was_cut:
            self.counters.truncated += 1
            self.log(f"    output truncated to {self.config.max_output_chars} characters")
        if action.tool == "read_file":
            key = self._key(action.arguments["path"])
            if was_cut:
                self.partly_read.add(key)
                text += "\nThis file cannot be edited because you have not seen all of it."
            else:
                self.partly_read.discard(key)
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

    def _replace_in_file(self, path, old, new):
        message = self.tools.replace_in_file(path, old, new)
        self._record_change(path)
        return message

    def _edit_file(self, path, content):
        message = self.tools.edit_file(path, content)
        self.partly_read.discard(self._key(path))  # the model wrote all of it
        self._record_change(path)
        return message

    def _record_change(self, path):
        if path not in self.changed_files:
            self.changed_files.append(path)

    def _run_check(self, name=None):
        result = self.executor.run(name or self.config.check_command)
        self.last_check = result  # keeps the FULL output, even if truncated below
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

    def _stop(self, status, summary, quiet=False):
        if not quiet:
            self.log(summary)
        return RunResult(status, summary, self.counters, self.config.max_actions,
                         list(self.changed_files), self.last_check)


def truncate(text, limit):
    """Cut `text` to `limit` characters and add a marker. Returns (text, was_cut)."""
    if len(text) <= limit:
        return text, False
    marker = f"{TRUNCATION_MARKER} showed the first {limit} of {len(text)} characters."
    return f"{text[:limit]}\n{marker}", True


def describe(action):
    """One-line description for progress output, e.g. 'read_file orders/pricing.py'."""
    args = action.arguments
    detail = args.get("query") or args.get("path") or args.get("name") or ""
    return f"{action.tool} {detail}".strip()


def _as_json(action):
    """Rebuild the JSON text of an action (used if the original reply is missing)."""
    return json.dumps({"tool": action.tool, "arguments": action.arguments})
