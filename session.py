"""Sessions: saving a run so it can be reopened, inspected and resumed.

A run used to live only in memory. If the process died, the work was lost
and the repository was left in whatever half-changed state the agent had
reached. A Session writes that state down after every step, so the run can
be reopened after a complete restart of the application.

WHAT IS SAVED
    Session         a stable id, the TaskSpec, the status, every action with
                    its result and errors, and the latest Checkpoint.
    Checkpoint      everything needed to carry on: the step number, the
                    consumed limits, the conversation, which files the model
                    has read, which actions already failed, the last check
                    result, the repository's branch/commit/dirty state, a
                    digest per file the agent changed, and -- the important
                    one -- `pending`, the action that was started but whose
                    result was never recorded.

HOW A CHECKPOINT IS WRITTEN (durable and atomic)
    A checkpoint is written to "<id>.json.tmp", flushed, fsync'ed, and then
    os.replace()d over "<id>.json". os.replace is atomic on POSIX and on
    Windows, so a reader never sees a half-written file: it sees either the
    previous checkpoint or the new one. A crash in the middle costs at most
    the last step, never the file.

WHY `pending` EXISTS
    The harness saves a checkpoint with `pending` set *before* it runs an
    action, and saves again with `pending` cleared *after* the result is
    recorded. So if the process is killed while a tool is running, the
    reopened session still has `pending` set. That action's outcome is
    genuinely unknown -- the file may or may not have been written -- so it
    is marked UNCERTAIN and is NEVER replayed automatically. The user is
    told what it was and has to decide.

WHY RESUME REVALIDATES
    A saved session describes a world that may have moved on: someone may
    have switched branch, edited the files, or changed what the task is
    allowed to touch. `check_resume()` compares the saved state with the
    real one and returns every mismatch it finds. On any mismatch the
    harness pauses and explains, rather than carrying on against a
    repository it no longer recognises.
"""

import hashlib
import json
import os
import re
import time
import uuid
from contextlib import contextmanager
from dataclasses import dataclass, field, replace
from pathlib import Path

from task_record import ActionRecord, TaskSpec

# Session statuses. "interrupted" is what a reopened, unfinished run looks like.
RUNNING = "running"
INTERRUPTED = "interrupted"

# Only a completed task is truly over. A run that was cancelled, hit a limit or
# died on a model error is still resumable in principle -- a model that was
# unreachable may be back, and the brief expects "cancel an active run, then
# resume from a safe checkpoint". Whether it MAY continue is then decided by the
# other checks in check_resume: exhausted limits, moved commit, changed
# permissions. Those give a far more useful message than "already finished".
FINISHED = ("done",)

SESSION_SUFFIX = ".json"
TEMP_SUFFIX = ".json.tmp"

# A session id: "20261031-190500-a1b2c3d4". Sortable, readable, unique.
ID_PATTERN = re.compile(r"^\d{8}-\d{6}-[0-9a-f]{8}$")


def new_session_id(now=None):
    """A stable, sortable, unique id for one task."""
    stamp = time.strftime("%Y%m%d-%H%M%S", time.localtime(now or time.time()))
    return f"{stamp}-{uuid.uuid4().hex[:8]}"


def file_digest(path):
    """sha256 of one file's bytes, or "" if it cannot be read."""
    try:
        return hashlib.sha256(Path(path).read_bytes()).hexdigest()
    except OSError:
        return ""


class SessionError(Exception):
    """A session could not be read, or is not usable."""


@dataclass
class Checkpoint:
    """Everything needed to carry a run on from where it stopped."""

    step: int = 0
    counters: dict = field(default_factory=dict)   # consumed limits
    messages: list = field(default_factory=list)   # the conversation so far
    changed_files: list = field(default_factory=list)
    file_digests: dict = field(default_factory=dict)  # path -> sha256 of the agent's edits
    read_files: list = field(default_factory=list)
    partly_read: list = field(default_factory=list)
    failed_actions: list = field(default_factory=list)  # [[tool, arguments-json], ...]
    repeats: int = 0
    rejected_in_a_row: int = 0
    checked_since_edit: bool = False
    last_check: dict = None          # name + status text of the last check
    branch: str = ""
    commit: str = ""
    dirty: list = field(default_factory=list)   # git status --short lines
    pending: dict = None             # action started but never confirmed finished

    def to_dict(self):
        return {
            "step": self.step, "counters": dict(self.counters),
            "messages": list(self.messages), "changed_files": list(self.changed_files),
            "file_digests": dict(self.file_digests), "read_files": list(self.read_files),
            "partly_read": list(self.partly_read),
            "failed_actions": [list(entry) for entry in self.failed_actions],
            "repeats": self.repeats, "rejected_in_a_row": self.rejected_in_a_row,
            "checked_since_edit": self.checked_since_edit, "last_check": self.last_check,
            "branch": self.branch, "commit": self.commit, "dirty": list(self.dirty),
            "pending": self.pending,
        }

    @classmethod
    def from_dict(cls, data):
        if not isinstance(data, dict):
            raise SessionError("session checkpoint must be a JSON object")
        return cls(
            step=data.get("step", 0), counters=dict(data.get("counters", {})),
            messages=list(data.get("messages", [])),
            changed_files=list(data.get("changed_files", [])),
            file_digests=dict(data.get("file_digests", {})),
            read_files=list(data.get("read_files", [])),
            partly_read=list(data.get("partly_read", [])),
            failed_actions=[tuple(entry) for entry in data.get("failed_actions", [])],
            repeats=data.get("repeats", 0),
            rejected_in_a_row=data.get("rejected_in_a_row", 0),
            checked_since_edit=data.get("checked_since_edit", False),
            last_check=data.get("last_check"), branch=data.get("branch", ""),
            commit=data.get("commit", ""), dirty=list(data.get("dirty", [])),
            pending=data.get("pending"),
        )


@dataclass
class Session:
    """One saved task: what was asked, what happened, and where it stopped."""

    id: str
    spec: TaskSpec
    status: str = RUNNING
    summary: str = ""
    actions: list = field(default_factory=list)       # ActionRecord
    checkpoint: Checkpoint = field(default_factory=Checkpoint)
    created_at: str = ""
    updated_at: str = ""
    # Set when a reopened session had an action in flight. It is reported and
    # never replayed; the field is kept so the user can see what it was.
    uncertain: dict = None
    run_settings: dict = field(default_factory=dict)
    result: dict = field(default_factory=dict)  # final checks/diff; empty on older sessions

    @property
    def finished(self):
        return self.status in FINISHED

    @property
    def has_uncertain_action(self):
        return bool(self.uncertain or self.checkpoint.pending)

    def consumed(self, name):
        """How much of limit `name` this session has already used."""
        return self.checkpoint.counters.get(name, 0)

    def remaining_actions(self):
        """Actions still allowed by the saved limits (never below 0)."""
        limit = self.spec.limits.get("max_actions")
        if limit is None:
            return None
        return max(0, limit - self.consumed("actions"))

    def progress(self):
        """A short, readable description of how far the run got."""
        limit = self.spec.limits.get("max_actions")
        used = self.consumed("actions")
        budget = f"{used}/{limit}" if limit is not None else str(used)
        lines = [
            f"Session:  {self.id}",
            f"Status:   {self.status}" + (" (reopened)" if self.status == INTERRUPTED else ""),
            f"Request:  {self.spec.request}",
            f"Project:  {self.spec.project}  ({self.spec.root})",
            f"Commit:   {self.checkpoint.commit or self.spec.starting_commit or '(unknown)'}"
            f" on {self.checkpoint.branch or '(unknown branch)'}",
            f"Actions:  {budget} used, {len(self.actions)} recorded",
            f"Changed:  {', '.join(self.checkpoint.changed_files) or 'nothing'}",
            f"Check:    {_check_text(self.checkpoint.last_check)}",
        ]
        if self.has_uncertain_action:
            pending = self.uncertain or self.checkpoint.pending
            lines.append(f"UNCERTAIN: {_action_text(pending)} was started but never "
                         "confirmed finished. It will NOT be replayed automatically.")
        if self.result:
            lines.append(f"Final verification: {self.result.get('verdict', 'UNAVAILABLE')}")
        return "\n".join(lines)

    def to_dict(self):
        return {
            "id": self.id, "spec": self.spec.to_dict(), "status": self.status,
            "summary": self.summary,
            "actions": [record.to_dict() for record in self.actions],
            "checkpoint": self.checkpoint.to_dict(),
            "created_at": self.created_at, "updated_at": self.updated_at,
            "uncertain": self.uncertain,
            "run_settings": self.run_settings, "result": self.result,
        }

    @classmethod
    def from_dict(cls, data):
        try:
            if not isinstance(data, dict) or not isinstance(data.get("spec"), dict):
                raise SessionError("session file must be an object with a task specification")
            return cls(
                id=data["id"], spec=TaskSpec.from_dict(data["spec"]),
                status=data.get("status", RUNNING), summary=data.get("summary", ""),
                actions=[ActionRecord.from_dict(a) for a in data.get("actions", [])],
                checkpoint=Checkpoint.from_dict(data.get("checkpoint", {})),
                created_at=data.get("created_at", ""), updated_at=data.get("updated_at", ""),
                uncertain=data.get("uncertain"),
                run_settings=dict(data.get("run_settings", {})),
                result=dict(data.get("result", {})),
            )
        except (KeyError, TypeError, ValueError, AttributeError) as error:
            raise SessionError(f"session file is missing or malformed: {error}") from None


class SessionStore:
    """Saves and reopens sessions as one JSON file each, written atomically."""

    def __init__(self, directory):
        self.directory = Path(directory)

    def path_for(self, session_id):
        if not ID_PATTERN.match(str(session_id)):
            raise SessionError(f"not a valid session id: {session_id!r}")
        return self.directory / f"{session_id}{SESSION_SUFFIX}"

    @contextmanager
    def claim(self, session_id):
        """One runner per session (POSIX); readers/cancellation never need a lock."""
        import fcntl

        path = self.path_for(session_id).with_suffix(".lock")
        self.directory.mkdir(parents=True, exist_ok=True)
        with path.open("a") as handle:
            try:
                fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError:
                raise SessionError(f"session {session_id} is active in another process; "
                                   "use status or cancel before resuming") from None
            try:
                yield
            finally:
                fcntl.flock(handle, fcntl.LOCK_UN)

    def save(self, session):
        """Write the session durably and atomically. Returns the path.

        The data goes to a temporary file in the same directory, is flushed
        and fsync'ed, and is then os.replace()d over the real name. A reader
        therefore sees either the old file or the new one, never a partial
        write, and a crash costs at most the newest step.
        """
        session.updated_at = _now()
        if not session.created_at:
            session.created_at = session.updated_at
        target = self.path_for(session.id)
        self.directory.mkdir(parents=True, exist_ok=True)
        temporary = self.directory / f"{session.id}{TEMP_SUFFIX}"
        text = json.dumps(session.to_dict(), indent=2) + "\n"
        try:
            with open(temporary, "w", encoding="utf-8") as handle:
                handle.write(text)
                handle.flush()
                os.fsync(handle.fileno())   # the bytes are on disk, not just in a buffer
            os.replace(temporary, target)   # atomic: readers see one version or the other
        except OSError as error:
            raise SessionError(f"cannot save session {session.id}: {error}") from None
        return target

    def load(self, session_id):
        """Reopen a saved session. Raises SessionError if it cannot be read."""
        target = self.path_for(session_id)
        try:
            data = json.loads(target.read_text(encoding="utf-8"))
        except FileNotFoundError:
            raise SessionError(f"no session {session_id} in {self.directory}") from None
        except OSError as error:
            raise SessionError(f"cannot read session {session_id}: {error}") from None
        except json.JSONDecodeError as error:
            raise SessionError(f"session {session_id} is not valid JSON: {error}") from None
        except UnicodeError:
            raise SessionError(f"session {session_id} is not valid UTF-8") from None
        saved = Session.from_dict(data)
        if saved.id != session_id:
            raise SessionError(f"session id {saved.id!r} does not match filename {session_id}")
        return saved

    def list_sessions(self):
        """Every saved session, newest id first. Unreadable files are skipped."""
        if not self.directory.is_dir():
            return []
        sessions = []
        for path in sorted(self.directory.glob(f"*{SESSION_SUFFIX}"), reverse=True):
            if path.name.endswith(TEMP_SUFFIX):
                continue
            try:
                sessions.append(self.load(path.stem))
            except SessionError:
                continue
        return sessions

    def reopen(self, session_id):
        """Load a session and mark an in-flight action as uncertain.

        A `pending` action in the saved checkpoint means the process stopped
        between starting that action and recording its result, so nobody
        knows whether it took effect. It is moved to `uncertain`, which is
        reported and never replayed.
        """
        session = self.load(session_id)
        if session.checkpoint.pending and not session.finished:
            session.uncertain = session.checkpoint.pending
            session.checkpoint = replace(session.checkpoint, pending=None)
            session.status = INTERRUPTED
        elif not session.finished:
            session.status = INTERRUPTED
        return session


# ----- resume safety ----------------------------------------------------------

@dataclass
class ResumeReport:
    """The result of checking whether a saved session may be resumed."""

    mismatches: list = field(default_factory=list)   # reasons it may NOT continue
    notes: list = field(default_factory=list)        # things worth saying anyway

    @property
    def safe(self):
        return not self.mismatches

    def text(self):
        lines = []
        if self.mismatches:
            lines.append("PAUSED: the saved session does not match the current state.")
            lines += [f"  - {reason}" for reason in self.mismatches]
            lines.append("Nothing was executed. Fix the mismatch, or start a new task.")
        else:
            lines.append("Safe to resume: the saved state matches the repository.")
        lines += [f"  note: {note}" for note in self.notes]
        return "\n".join(lines)


def check_resume(session, root, branch, commit, dirty, mode, allowed_changes,
                 allowed_projects=()):
    """Compare a saved session with the world as it is now.

    Everything that must still hold is checked here, and every mismatch is
    collected rather than raised, so the user is told all of them at once:

      - the project is still allowed,
      - the permissions (mode and allowed changes) are unchanged,
      - the Git branch and commit are the saved ones,
      - the files the agent changed are byte-for-byte as it left them,
      - no other file has appeared or changed since,
      - the limits are not already used up,
      - the session is not already finished.

    Returns a ResumeReport. `safe` is False if anything differs.
    """
    report = ResumeReport()
    saved = session.checkpoint

    if session.finished:
        report.mismatches.append(
            f"this session already finished with status {session.status!r}; "
            "a finished run is not resumed")

    # 1. project allowlist
    if allowed_projects:
        resolved = Path(root).resolve()
        if not any(resolved == Path(allowed).resolve()
                   for allowed in allowed_projects):
            report.mismatches.append(
                f"project {resolved} is not in the allowed projects list "
                f"({', '.join(str(p) for p in allowed_projects)})")
    if Path(session.spec.root).resolve() != Path(root).resolve():
        report.mismatches.append(
            f"the session was saved for {session.spec.root}, not {root}")

    # 2. permissions
    if session.spec.mode != mode:
        report.mismatches.append(
            f"mode changed: the session was saved in {session.spec.mode!r} mode, "
            f"this run is {mode!r}")
    if tuple(session.spec.allowed_changes) != tuple(allowed_changes):
        report.mismatches.append(
            "allowed changes differ: the session may change "
            f"{', '.join(session.spec.allowed_changes) or 'any file'}, "
            f"this run allows {', '.join(allowed_changes) or 'any file'}")

    # 3. Git branch and commit
    if saved.branch and branch and saved.branch != branch:
        report.mismatches.append(
            f"branch changed: saved on {saved.branch!r}, now on {branch!r}")
    if saved.commit and commit and saved.commit != commit:
        report.mismatches.append(
            f"commit changed: saved at {saved.commit[:12]}, now at {commit[:12]}")

    # 4. the working copy, including the agent's own edits
    for path, digest in sorted(saved.file_digests.items()):
        now = file_digest(Path(root) / path)
        if now == digest:
            continue
        if not now:
            report.mismatches.append(
                f"{path} was changed by the agent but is now missing or unreadable")
        else:
            report.mismatches.append(
                f"{path} was changed by the agent and has been modified since "
                "(the saved content is not what is on disk)")

    if sorted(saved.dirty) != sorted(dirty):
        appeared = sorted(set(dirty) - set(saved.dirty))
        vanished = sorted(set(saved.dirty) - set(dirty))
        detail = []
        if appeared:
            detail.append("new: " + ", ".join(appeared))
        if vanished:
            detail.append("gone: " + ", ".join(vanished))
        report.mismatches.append(
            "the working copy has other changes than the saved ones (" +
            "; ".join(detail) + ")")

    # 5. limits already consumed
    remaining = session.remaining_actions()
    if remaining == 0:
        report.mismatches.append(
            f"the action limit is already used up "
            f"({session.consumed('actions')}/{session.spec.limits.get('max_actions')})")
    max_retries = session.spec.limits.get("max_retries")
    if max_retries is not None and saved.repeats >= max_retries:
        report.mismatches.append(
            f"the retry limit is already used up ({saved.repeats}/{max_retries} "
            "repeated failed actions)")

    # 6. an action whose outcome nobody knows
    if session.has_uncertain_action:
        pending = session.uncertain or saved.pending
        report.notes.append(
            f"{_action_text(pending)} was started but never confirmed finished. "
            "It is NOT replayed. Check the diff and decide yourself.")
    if remaining is not None and remaining > 0:
        report.notes.append(f"{remaining} of "
                            f"{session.spec.limits.get('max_actions')} actions left")
    return report


# ----- helpers ----------------------------------------------------------------

def _now():
    return time.strftime("%Y-%m-%dT%H:%M:%S", time.localtime())


def _action_text(action):
    if not action:
        return "(no action)"
    tool = action.get("tool", "?")
    arguments = action.get("arguments") or {}
    detail = (arguments.get("path") or arguments.get("query")
              or arguments.get("pattern") or arguments.get("name") or "")
    step = action.get("step")
    prefix = f"step {step}: " if step else ""
    return f"{prefix}{tool} {detail}".strip()


def _check_text(last_check):
    if not last_check:
        return "not run"
    return f"{last_check.get('name', '?')} {last_check.get('status', '?')}"
