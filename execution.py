"""Execution: runs a small set of pre-approved commands in the repository.

Callers never pass a command line. They pass the *name* of a configured
command (for example "unittest"), and this module looks up the exact
argument list to run. Commands always run with:

- cwd set to the repository root,
- shell=False (the default), so no shell ever interprets the command,
- a timeout; on timeout (or Ctrl+C) the command AND every process it
  started are killed, so nothing keeps running or writing afterwards,
- a reduced environment, so secrets in environment variables (API keys,
  tokens) are not visible to the repository's code.
"""

import os
import re
import signal
import subprocess
import sys
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Optional

# The Python running the harness. Using it instead of a bare "python3"
# works on every OS and inside a virtual environment.
PYTHON = sys.executable

# Name -> exact argument list. Only these commands can ever be run.
ALLOWED_COMMANDS = {
    "unittest": [PYTHON, "-m", "unittest", "discover", "-s", "tests", "-v"],
    # -p no:cacheprovider: don't create a .pytest_cache folder in the repository
    "pytest": [PYTHON, "-m", "pytest", "-p", "no:cacheprovider"],
}

# Only these environment variables are passed to commands. Everything else
# (API keys, tokens, cloud credentials, ...) stays out of the repository's code.
SAFE_ENV_VARS = (
    "PATH", "HOME", "USERPROFILE", "LANG", "LC_ALL", "LC_CTYPE", "TMPDIR", "TEMP", "TMP",
    "SYSTEMROOT", "SYSTEMDRIVE", "WINDIR", "COMSPEC", "PATHEXT",  # needed on Windows
    "PYTEST_ADDOPTS",  # lets the user pass pytest options, e.g. --ignore=tests/e2e
)

# Don't let Python write __pycache__ folders into the target repository,
# so running tests does not show up as "changed files" in git status.
EXTRA_ENV = {"PYTHONDONTWRITEBYTECODE": "1"}

# unittest prints "Ran 0 tests" (and exits 0 on Python <= 3.11) when it finds
# nothing; pytest prints "no tests ran". Zero tests must never count as a pass.
NO_TESTS_PATTERN = re.compile(r"^Ran 0 tests\b|no tests ran", re.MULTILINE | re.IGNORECASE)


def command_env():
    """The environment for commands: safe variables only, plus EXTRA_ENV."""
    env = {name: os.environ[name] for name in SAFE_ENV_VARS if name in os.environ}
    env.update(EXTRA_ENV)
    return env


class ExecutionError(Exception):
    """A command request was refused (for example, it is not allowed)."""


@dataclass
class CommandResult:
    """Everything we know about one finished (or killed) command."""

    name: str
    command: list
    exit_code: Optional[int]  # None if the command never finished
    stdout: str
    stderr: str
    timed_out: bool
    duration_seconds: float
    no_tests_ran: bool = False  # a test command that found nothing to run

    @property
    def succeeded(self):
        """True only if the command finished on its own with exit code 0
        and (for test commands) actually ran at least one test."""
        return not self.timed_out and self.exit_code == 0 and not self.no_tests_ran

    def status(self):
        """Return a short, unambiguous one-line status."""
        if self.timed_out:
            return f"TIMED OUT after {self.duration_seconds:.1f}s"
        if self.exit_code is None:
            return "FAILED (command could not be started)"
        if self.no_tests_ran:
            return f"FAILED (no tests were run, exit code {self.exit_code})"
        if self.exit_code == 0:
            return "SUCCEEDED (exit code 0)"
        return f"FAILED (exit code {self.exit_code})"

    def report(self):
        """Return a readable multi-line report including all output."""
        return "\n".join([
            f"Command: {' '.join(self.command)}",
            f"Status:  {self.status()}",
            "--- stdout ---",
            self.stdout.rstrip() or "(empty)",
            "--- stderr ---",
            self.stderr.rstrip() or "(empty)",
        ])


class ExecutionEnvironment:
    """Runs allowed commands inside one repository, with a timeout."""

    def __init__(self, root, timeout_seconds=30, allowed_commands=None):
        """Store the repository root, timeout and the command whitelist."""
        self.root = Path(root).resolve()
        if not self.root.is_dir():
            raise ExecutionError(f"Repository root is not a directory: {self.root}")
        if timeout_seconds <= 0:
            raise ExecutionError("timeout_seconds must be greater than 0")
        self.timeout_seconds = timeout_seconds
        self.allowed_commands = allowed_commands or ALLOWED_COMMANDS

    def available_commands(self):
        """Return the names of the commands that may be run."""
        return sorted(self.allowed_commands)

    def run(self, name):
        """Run the configured command called `name` and return a CommandResult."""
        if name not in self.allowed_commands:
            raise ExecutionError(
                f"Command {name!r} is not allowed. "
                f"Allowed commands: {', '.join(self.available_commands())}"
            )
        command = list(self.allowed_commands[name])

        start = time.monotonic()
        try:
            process = subprocess.Popen(
                command,                 # a list, so no shell is involved
                cwd=self.root,           # always inside the target repository
                stdout=subprocess.PIPE,  # collect stdout ...
                stderr=subprocess.PIPE,  # ... and stderr
                text=True,               # give us str instead of bytes
                env=command_env(),
                **NEW_PROCESS_GROUP,     # so we can kill everything it starts
            )
        except OSError as error:
            # For example, the program does not exist.
            return CommandResult(name, command, None, "", f"Could not start command: {error}",
                                 False, time.monotonic() - start)

        timed_out = False
        try:
            stdout, stderr = process.communicate(timeout=self.timeout_seconds)
        except subprocess.TimeoutExpired:
            timed_out = True
            kill_process_tree(process)
            stdout, stderr = _collect_after_kill(process)
        except BaseException:
            # E.g. Ctrl+C: don't leave the command running behind us.
            kill_process_tree(process)
            raise
        finally:
            # Also remove anything the command left running in the background.
            kill_process_tree(process)

        no_tests = not timed_out and bool(NO_TESTS_PATTERN.search(stdout + stderr))
        return CommandResult(
            name=name,
            command=command,
            exit_code=None if timed_out else process.returncode,
            stdout=stdout,
            stderr=stderr,
            timed_out=timed_out,
            duration_seconds=time.monotonic() - start,
            no_tests_ran=no_tests,
        )


# Start each command in its own process group (POSIX: new session), so that
# the command and every process it starts can be killed together.
if os.name == "posix":
    NEW_PROCESS_GROUP = {"start_new_session": True}
else:
    NEW_PROCESS_GROUP = {"creationflags": subprocess.CREATE_NEW_PROCESS_GROUP}


def kill_process_tree(process):
    """Kill a command and all processes it started. Safe to call twice."""
    if os.name == "posix":
        try:
            os.killpg(process.pid, signal.SIGKILL)  # the whole group, not just one process
        except (ProcessLookupError, PermissionError):
            pass  # already gone
    else:
        if process.poll() is None:
            subprocess.run(["taskkill", "/F", "/T", "/PID", str(process.pid)],
                           capture_output=True)
    if process.poll() is None:
        process.kill()


def _collect_after_kill(process):
    """Read whatever output the killed command produced before it stopped."""
    try:
        stdout, stderr = process.communicate(timeout=5)
    except subprocess.TimeoutExpired:
        return "", "(output lost: the command could not be stopped cleanly)"
    return _to_text(stdout), _to_text(stderr)


def _to_text(output):
    """Partial output from a timeout may be bytes, str or None; return str."""
    if output is None:
        return ""
    if isinstance(output, bytes):
        return output.decode("utf-8", errors="replace")
    return output
