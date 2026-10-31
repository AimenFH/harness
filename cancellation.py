"""Cancellation: stopping an active run, from this process or another one.

Ctrl+C only reaches the process you are sitting in front of. A long harness
run may be in another terminal, started by a script, or left going while
you do something else, so cancelling has to work *between* processes:

    terminal 1:  python main.py --root ... --session-dir ~/sessions ...
    terminal 2:  python main.py --cancel <id> --session-dir ~/sessions

The second process writes a small file next to the session; the first one
notices it and stops. A file is enough -- it needs no daemon, no port and
no privileges, it works when the two processes cannot see each other's
memory, and it survives either of them dying.

WHERE IT IS CHECKED
    between steps          Controller._loop, before asking the model
    during a model request ModelClient waits for the reply in a worker
                           thread and polls this while it waits
    during a command       ExecutionEnvironment.run polls this while the
                           subprocess runs, and on cancel kills the whole
                           process group and removes the Docker container

ONCE CANCELLED, ALWAYS CANCELLED
    Every source latches: the first time it reports True it keeps reporting
    True, even if the file is deleted afterwards. A run must not quietly
    come back to life, and a late answer from the model must not be able to
    start another action.
"""

import os
import time
from pathlib import Path

# How often a blocking wait looks at the cancel source.
POLL_SECONDS = 0.25

CANCEL_SUFFIX = ".cancel"

DEFAULT_REASON = "cancelled by the user"


class CancelledError(Exception):
    """Raised inside a blocking wait when the run has been cancelled."""

    def __init__(self, reason=DEFAULT_REASON):
        super().__init__(reason)
        self.reason = reason


class CancelToken:
    """A cancellation inside this process. Used by the signal handler and tests."""

    def __init__(self):
        self._reason = None

    def cancel(self, reason=DEFAULT_REASON):
        """Ask the run to stop. Calling it again is harmless."""
        if self._reason is None:          # keep the first reason
            self._reason = reason or DEFAULT_REASON

    @property
    def cancelled(self):
        return self._reason is not None

    @property
    def reason(self):
        return self._reason or ""

    def check(self):
        """Raise CancelledError if cancelled. For use inside a wait loop."""
        if self.cancelled:
            raise CancelledError(self.reason)


class CancelFile:
    """A cancellation another process can request by creating a file.

    `request()` is what the cancelling process calls; `cancelled` is what the
    running process polls. The file holds the reason, so the run can say why
    it stopped. Writing it twice is safe, and so is cancelling a run that has
    already finished -- nothing reads the file afterwards.
    """

    def __init__(self, directory, session_id):
        self.path = Path(directory) / f"{session_id}{CANCEL_SUFFIX}"
        self._latched = None   # once cancelled, stays cancelled

    def request(self, reason=DEFAULT_REASON):
        """Create the cancel file. Called by the OTHER process."""
        self.path.parent.mkdir(parents=True, exist_ok=True)
        temporary = self.path.with_suffix(f"{CANCEL_SUFFIX}.tmp")
        temporary.write_text((reason or DEFAULT_REASON) + "\n", encoding="utf-8")
        os.replace(temporary, self.path)   # atomic, so a reader sees all or nothing
        return self.path

    @property
    def cancelled(self):
        if self._latched is not None:
            return True
        if not self.path.exists():
            return False
        try:
            reason = self.path.read_text(encoding="utf-8").strip()
        except OSError:
            reason = ""
        self._latched = reason or DEFAULT_REASON
        return True

    @property
    def reason(self):
        return self._latched or ""

    def check(self):
        if self.cancelled:
            raise CancelledError(self.reason)

    def clear(self):
        """Remove the file. Does not un-latch an already noticed cancellation."""
        try:
            self.path.unlink()
        except OSError:
            pass


class AnyCancel:
    """Cancelled as soon as any of several sources is.

    A run usually has two: a CancelToken for Ctrl+C in its own terminal, and
    a CancelFile for `--cancel` from another one.
    """

    def __init__(self, *sources):
        self.sources = [source for source in sources if source is not None]

    @property
    def cancelled(self):
        return any(source.cancelled for source in self.sources)

    @property
    def reason(self):
        for source in self.sources:
            if source.cancelled:
                return source.reason
        return ""

    def check(self):
        if self.cancelled:
            raise CancelledError(self.reason)


def wait_for(is_done, cancel=None, timeout=None, poll_seconds=POLL_SECONDS):
    """Block until `is_done()` is true, raising CancelledError if cancelled.

    Returns True when `is_done()` became true, False when `timeout` ran out.
    The wait is a poll loop rather than a blocking call, because a blocking
    call cannot be interrupted by a file appearing on disk.
    """
    deadline = None if timeout is None else time.monotonic() + timeout
    while True:
        if is_done():
            return True
        if cancel is not None and cancel.cancelled:
            raise CancelledError(cancel.reason)
        if deadline is not None and time.monotonic() >= deadline:
            return False
        remaining = poll_seconds
        if deadline is not None:
            remaining = min(remaining, max(0.0, deadline - time.monotonic()))
        time.sleep(remaining)


def cancel_path(directory, session_id):
    """Where the cancel file for one session lives."""
    return Path(directory) / f"{session_id}{CANCEL_SUFFIX}"
