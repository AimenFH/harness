"""The task record: what was asked, what is allowed, and what happened.

Stage 1 ran a task and forgot it. Stage 2 writes it down. One run produces
one TaskRecord:

    TaskSpec    the request, the project, the exact starting commit, the
                files the agent may change, the checks, the acceptance
                commands, and the limits it must stay inside
    ActionRecord one per model reply: which tool, which arguments, what
                came of it. Denied and malformed replies are recorded too,
                because what the agent was stopped from doing matters as
                much as what it did.

The record is plain JSON, so a later phase can reopen it and a human can
read it. It is written next to the harness, never inside the target
repository, so the agent cannot edit its own task.

ScopePolicy is the part with teeth. The *harness* decides whether a file
may be changed, before any write happens -- not a script that checks
afterwards, by which time the file is already different.
"""

import fnmatch
import json
from dataclasses import dataclass, field, replace
from pathlib import Path

# CancelToken lives in cancellation.py with the other cancel sources;
# it is re-exported here because it was first introduced with the task record.
from cancellation import CancelToken  # noqa: F401

# Outcomes of one model reply. OK/DENIED/FAILED mirror controller.py;
# MALFORMED covers a reply that was not a valid action at all.
OK = "ok"
DENIED = "denied"
FAILED = "failed"
MALFORMED = "malformed"

# Arguments longer than this are shortened in the record. The full file
# content of an edit belongs in the diff, not in the action log.
MAX_RECORDED_ARGUMENT = 200

# Result text longer than this is shortened in the record.
MAX_RECORDED_DETAIL = 2000


class ScopeError(Exception):
    """A task specification is not usable (e.g. acceptance inside the root)."""


class ScopePolicy:
    """Which files inside the repository the agent may change.

    `patterns` are glob patterns relative to the repository root, e.g.
    "service_layer/services.py" or "domain/*.py". An empty pattern list
    means every file inside the root may be changed, which is the Stage 1
    behaviour and stays the default.

    Matching uses fnmatch, so "*" also matches "/". That is deliberately
    simple and predictable: "src/*.py" allows "src/a/b.py" too. Name the
    exact files when a task must be tight.
    """

    def __init__(self, patterns=()):
        self.patterns = tuple(p.strip() for p in patterns if p and p.strip())

    def allows(self, relative_path):
        """True if `relative_path` (posix, relative to the root) may be changed."""
        if not self.patterns:
            return True
        path = str(relative_path).replace("\\", "/").lstrip("./")
        return any(fnmatch.fnmatch(path, pattern) for pattern in self.patterns)

    def refusal(self, relative_path):
        """The message explaining why `relative_path` may not be changed."""
        return (f"Permission denied: {relative_path} is outside the allowed changes "
                f"for this task ({self.describe()}). Change only the allowed files.")

    def describe(self):
        if not self.patterns:
            return "any file in the repository"
        return ", ".join(self.patterns)


@dataclass
class TaskSpec:
    """Everything that defines one task. Saved with the run."""

    request: str                      # what the user asked for
    project: str                      # the project's name
    root: str                         # the target repository (the agent's writable area)
    starting_commit: str = ""         # git HEAD when the task started; "" if unknown
    mode: str = "edit"
    allowed_changes: tuple = ()       # glob patterns; () = any file in the root
    checks: tuple = ()                # check names the loop may run
    acceptance_commands: tuple = ()   # checks run OUTSIDE the agent's writable area
    limits: dict = field(default_factory=dict)

    def scope(self):
        """The ScopePolicy for this task."""
        return ScopePolicy(self.allowed_changes)

    def validate(self):
        """Raise ScopeError if the specification is unsafe or contradictory.

        The important rule: an acceptance command must not live inside the
        agent's writable area. If it did, the agent could edit the very test
        that judges it.
        """
        root = Path(self.root).resolve()
        for command in self.acceptance_commands:
            for part in str(command).split():
                candidate = Path(part)
                if not candidate.is_absolute():
                    continue
                if _inside(candidate, root):
                    raise ScopeError(
                        f"Acceptance command {command!r} points inside the agent's "
                        f"writable area ({root}). Keep acceptance checks outside --root "
                        "so the agent cannot change what judges it.")
        if self.mode == "edit" and not self.request.strip():
            raise ScopeError("A task needs a request")
        return self

    def summary(self):
        """A readable, multi-line description of the task specification."""
        return "\n".join([
            f"Request:     {self.request}",
            f"Project:     {self.project}",
            f"Root:        {self.root}",
            f"Commit:      {self.starting_commit or '(unknown)'}",
            f"Mode:        {self.mode}",
            f"May change:  {self.scope().describe()}",
            f"Checks:      {', '.join(self.checks) or '(none)'}",
            f"Acceptance:  {', '.join(self.acceptance_commands) or '(none)'}",
            f"Limits:      {_limits_text(self.limits)}",
        ])

    def to_dict(self):
        return {
            "request": self.request,
            "project": self.project,
            "root": self.root,
            "starting_commit": self.starting_commit,
            "mode": self.mode,
            "allowed_changes": list(self.allowed_changes),
            "checks": list(self.checks),
            "acceptance_commands": list(self.acceptance_commands),
            "limits": dict(self.limits),
        }

    @classmethod
    def from_dict(cls, data):
        return cls(
            request=data["request"],
            project=data.get("project", ""),
            root=data.get("root", ""),
            starting_commit=data.get("starting_commit", ""),
            mode=data.get("mode", "edit"),
            allowed_changes=tuple(data.get("allowed_changes", ())),
            checks=tuple(data.get("checks", ())),
            acceptance_commands=tuple(data.get("acceptance_commands", ())),
            limits=dict(data.get("limits", {})),
        )


@dataclass
class ActionRecord:
    """One model reply and what came of it."""

    step: int
    tool: str
    arguments: dict = field(default_factory=dict)
    outcome: str = OK            # OK, DENIED, FAILED or MALFORMED
    detail: str = ""             # result text, or the reason it was refused
    check: str = ""              # for run_check: the check's one-line status

    def text(self):
        arguments = " ".join(f"{name}={value!r}" for name, value in self.arguments.items())
        line = f"[{self.step}] {self.outcome.upper():9} {self.tool} {arguments}".rstrip()
        return f"{line}  -> {self.check}" if self.check else line

    def to_dict(self):
        return {"step": self.step, "tool": self.tool, "arguments": self.arguments,
                "outcome": self.outcome, "detail": self.detail, "check": self.check}

    @classmethod
    def from_dict(cls, data):
        return cls(step=data["step"], tool=data["tool"],
                   arguments=dict(data.get("arguments", {})),
                   outcome=data.get("outcome", OK), detail=data.get("detail", ""),
                   check=data.get("check", ""))


@dataclass
class TaskRecord:
    """A task specification plus the actions taken under it."""

    spec: TaskSpec
    actions: list = field(default_factory=list)
    status: str = "running"
    summary: str = ""

    def add(self, step, tool, arguments=None, outcome=OK, detail="", check=""):
        """Record one action and return it."""
        record = ActionRecord(step=step, tool=tool,
                              arguments=_shorten_arguments(arguments or {}),
                              outcome=outcome, detail=_shorten(detail, MAX_RECORDED_DETAIL),
                              check=check)
        self.actions.append(record)
        return record

    def finish(self, status, summary):
        self.status = status
        self.summary = summary
        return self

    def actions_text(self):
        """One line per action, for the CLI."""
        if not self.actions:
            return "(no actions)"
        return "\n".join(record.text() for record in self.actions)

    def to_dict(self):
        return {"spec": self.spec.to_dict(), "status": self.status, "summary": self.summary,
                "actions": [record.to_dict() for record in self.actions]}

    def save(self, path):
        """Write the record as JSON. The folder is created if needed."""
        target = Path(path)
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(json.dumps(self.to_dict(), indent=2) + "\n", encoding="utf-8")
        return target

    @classmethod
    def load(cls, path):
        """Read a record back. Raises ValueError on unusable JSON."""
        try:
            data = json.loads(Path(path).read_text(encoding="utf-8"))
        except OSError as error:
            raise ValueError(f"cannot read task record {path}: {error}") from None
        except json.JSONDecodeError as error:
            raise ValueError(f"task record {path} is not valid JSON: {error}") from None
        try:
            return cls(spec=TaskSpec.from_dict(data["spec"]),
                       actions=[ActionRecord.from_dict(a) for a in data.get("actions", [])],
                       status=data.get("status", "running"),
                       summary=data.get("summary", ""))
        except (KeyError, TypeError) as error:
            raise ValueError(f"task record {path} is missing {error}") from None


# ----- helpers ----------------------------------------------------------------

def _inside(candidate, root):
    """True if `candidate` is `root` or sits inside it."""
    try:
        return candidate.resolve().is_relative_to(root)
    except (OSError, ValueError):
        return False


def _shorten(text, limit):
    text = str(text)
    if len(text) <= limit:
        return text
    return f"{text[:limit]}... ({len(text)} characters)"


def _shorten_arguments(arguments):
    """Shorten long argument values so the record stays readable."""
    return {name: _shorten(value, MAX_RECORDED_ARGUMENT) for name, value in arguments.items()}


def _limits_text(limits):
    if not limits:
        return "(defaults)"
    return ", ".join(f"{name} {value}" for name, value in sorted(limits.items()))


def spec_from_config(config, project="", starting_commit="", acceptance_commands=()):
    """Build a TaskSpec from a Config, for the CLI."""
    return TaskSpec(
        request=config.task,
        project=project or Path(config.root).name,
        root=str(config.root),
        starting_commit=starting_commit,
        mode=config.mode,
        allowed_changes=tuple(config.allowed_changes),
        checks=tuple(dict.fromkeys((config.check_command,) + tuple(config.final_checks))),
        acceptance_commands=tuple(acceptance_commands),
        limits={"max_actions": config.max_actions, "max_retries": config.max_retries,
                "max_output_chars": config.max_output_chars,
                "command_timeout_seconds": config.command_timeout_seconds},
    ).validate()


# `replace` is re-exported so callers can make an adjusted copy of a spec
# without importing dataclasses themselves.
__all__ = ["ActionRecord", "CancelToken", "ScopeError", "ScopePolicy", "TaskRecord",
           "TaskSpec", "DENIED", "FAILED", "MALFORMED", "OK", "replace", "spec_from_config"]
