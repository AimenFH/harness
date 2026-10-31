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

With a DockerSandbox, the command runs inside a throwaway container that
only sees the repository copy (read-only), has no network and none of the
host's files. That is the contained environment for repository code.
"""

import os
import re
import signal
import subprocess
import sys
import time
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Optional

from cancellation import POLL_SECONDS

# The Python running the harness. Using it instead of a bare "python3"
# works on every OS and inside a virtual environment.
PYTHON = sys.executable

# Name -> exact argument list. Only these commands can ever be run.
ALLOWED_COMMANDS = {
    "unittest": [PYTHON, "-m", "unittest", "discover", "-s", "tests", "-v"],
    # -p no:cacheprovider: don't create a .pytest_cache folder in the repository
    "pytest": [PYTHON, "-m", "pytest", "-p", "no:cacheprovider"],
    "ruff": ["ruff", "check", "--isolated", "--no-cache", "."],
    # Bytecode goes to the container's tmpfs, never the read-only repository.
    "compileall": [PYTHON, "-I", "-X", "pycache_prefix=/tmp/harness-build", "-m", "compileall", "-q", "."],
}
TEST_COMMANDS = ("unittest", "pytest")
LINT_COMMANDS = ("ruff",)
BUILD_COMMANDS = ("compileall",)
DEFAULT_CONTAINMENT = object()

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


# ----- sandbox ----------------------------------------------------------------

SANDBOX_IMAGE = "harness-sandbox"  # built from sandbox/Dockerfile
SANDBOX_WORKDIR = "/work"          # where the repository copy appears in the container

# Settings the docker program itself needs to find the Docker engine. They are
# given to the docker client on this machine only, never to the container.
DOCKER_CLIENT_VARS = ("DOCKER_HOST", "DOCKER_CONTEXT", "DOCKER_CONFIG", "DOCKER_CERT_PATH",
                      "DOCKER_TLS_VERIFY", "XDG_RUNTIME_DIR")

# The only host variable passed into the container (lets the user pass pytest options).
CONTAINER_VARS = ("PYTEST_ADDOPTS",)


def docker_client_env():
    """The environment for the docker program: command_env() plus Docker's own settings."""
    env = command_env()
    env.update({name: os.environ[name] for name in DOCKER_CLIENT_VARS if name in os.environ})
    return env


class DockerSandbox:
    """Runs commands inside a throwaway Docker container.

    The container:
      - sees only the repository copy, mounted read-only at /work
        (plus any extra read-only `mounts`, e.g. an acceptance check),
      - has no network, so tests cannot send data anywhere,
      - gets no host environment variables except CONTAINER_VARS,
      - runs as a normal user with no Linux capabilities, a read-only
        system, and limits on memory, CPU and number of processes,
      - is removed afterwards (--rm), or force-removed on timeout.

    Host files such as ~/.ssh or cloud credentials simply do not exist inside it.
    """

    def __init__(self, image=SANDBOX_IMAGE, docker="docker", mounts=()):
        """
        image:  Docker image with Python, pytest and the target's packages
        docker: the docker program (changeable for tests)
        mounts: extra (host path, container path) pairs, mounted read-only
        """
        self.image = image
        self.docker = docker
        self.mounts = list(mounts)

    def describe(self):
        return f"docker (image {self.image}, no network, read-only repository)"

    def check(self):
        """Raise ExecutionError with a clear fix if Docker or the image is missing."""
        try:
            done = subprocess.run([self.docker, "image", "inspect", self.image],
                                  capture_output=True, text=True, timeout=60,
                                  env=docker_client_env())
        except OSError:
            raise ExecutionError(
                f"Docker is not installed ({self.docker!r} not found). Install Docker, "
                "and build the sandbox image. --sandbox none disables repository checks.") from None
        except subprocess.TimeoutExpired:
            raise ExecutionError("Docker did not answer within 60s. Is it running?") from None
        if done.returncode != 0:
            reason = (done.stderr.strip().splitlines() or ["unknown error"])[0]
            raise ExecutionError(
                f"Sandbox image {self.image!r} is not available ({reason}). Start Docker and "
                f"build the image: docker build -t {self.image} -f sandbox/Dockerfile .")

    def wrap(self, command, root, container_name):
        """Return the docker command that runs `command` in a fresh container."""
        # The harness's own Python path means nothing inside the container.
        if command and command[0] == PYTHON:
            command = ["python"] + list(command[1:])
        args = [
            self.docker, "run", "--rm", "--name", container_name,
            "--network", "none",
            "--read-only", "--tmpfs", "/tmp",
            "--cap-drop", "ALL", "--security-opt", "no-new-privileges",
            "--pids-limit", "256", "--memory", "1g", "--cpus", "2",
            "--mount", f"type=bind,source={root},target={SANDBOX_WORKDIR},readonly",
            "--workdir", SANDBOX_WORKDIR,
            "--env", "HOME=/tmp",
        ]
        for host_path, container_path in self.mounts:
            args += ["--mount", f"type=bind,source={host_path},target={container_path},readonly"]
        container_env = dict(EXTRA_ENV)
        container_env.update({name: os.environ[name] for name in CONTAINER_VARS
                              if name in os.environ})
        for name, value in container_env.items():
            args += ["--env", f"{name}={value}"]
        return args + [self.image] + list(command)

    def stop(self, container_name):
        """Force-remove a container that may still be running. Safe to call twice."""
        try:
            subprocess.run([self.docker, "rm", "--force", container_name],
                           capture_output=True, timeout=60, env=docker_client_env())
        except (OSError, subprocess.TimeoutExpired):
            pass


class ExecutionError(Exception):
    """A command request was refused (for example, it is not allowed)."""


class _Stopped(Exception):
    """Internal: the command was killed, by the timeout or by a cancellation."""

    def __init__(self, timed_out=False, cancelled=False, stdout="", stderr=""):
        super().__init__("command stopped")
        self.timed_out = timed_out
        self.cancelled = cancelled
        self.stdout = stdout
        self.stderr = stderr


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
    sandbox: str = ""           # where it ran, e.g. "docker (...)"; "" = directly on this machine
    cancelled: bool = False     # stopped because the run was cancelled
    unavailable: bool = False
    skipped_tests: int = 0
    tests_run: Optional[int] = None

    @property
    def succeeded(self):
        """True only if the command finished on its own with exit code 0
        and (for test commands) actually ran at least one test.

        A cancelled command never counts as a pass: it was killed, so its
        exit code says nothing about whether the tests were going to pass.
        """
        return (not self.timed_out and not self.cancelled and not self.unavailable
                and not self.skipped_tests
                and self.exit_code == 0 and not self.no_tests_ran)

    def status(self):
        """Return a short, unambiguous one-line status."""
        if self.cancelled:
            return f"CANCELLED after {self.duration_seconds:.1f}s"
        if self.timed_out:
            return f"TIMED OUT after {self.duration_seconds:.1f}s"
        if self.unavailable:
            return "UNAVAILABLE (command could not be run)"
        if self.exit_code is None:
            return "FAILED (command could not be started)"
        if self.skipped_tests:
            return f"NOT PASSED ({self.skipped_tests} skipped/expected-failure tests)"
        if self.no_tests_ran:
            return f"FAILED (no tests were run, exit code {self.exit_code})"
        if self.exit_code == 0:
            return "SUCCEEDED (exit code 0)"
        return f"FAILED (exit code {self.exit_code})"

    def report(self):
        """Return a readable multi-line report including all output."""
        return "\n".join([
            f"Command: {' '.join(self.command)}",
            f"Sandbox: {self.sandbox or 'none (ran directly on this machine)'}",
            f"Status:  {self.status()}",
            "--- stdout ---",
            self.stdout.rstrip() or "(empty)",
            "--- stderr ---",
            self.stderr.rstrip() or "(empty)",
        ])


class ExecutionEnvironment:
    """Runs allowed commands inside one repository, with a timeout."""

    def __init__(self, root, timeout_seconds=30, allowed_commands=None, sandbox=DEFAULT_CONTAINMENT):
        """Store the repository root, timeout, the command whitelist and the
        sandbox (omitted: Docker for repository checks; explicit None refuses them)."""
        self.root = Path(root).resolve()
        if not self.root.is_dir():
            raise ExecutionError(f"Repository root is not a directory: {self.root}")
        if timeout_seconds <= 0:
            raise ExecutionError("timeout_seconds must be greater than 0")
        self.timeout_seconds = timeout_seconds
        self.allowed_commands = ALLOWED_COMMANDS if allowed_commands is None else allowed_commands
        self.sandbox = (DockerSandbox() if allowed_commands is None else None) if sandbox is DEFAULT_CONTAINMENT else sandbox

    def available_commands(self):
        """Return the names of the commands that may be run."""
        return sorted(self.allowed_commands)

    def run(self, name, cancel=None):
        """Run the configured command called `name` and return a CommandResult.

        With `cancel`, the wait is a poll loop instead of one blocking call, so
        a cancellation from another process is noticed while the command is
        still running. On cancel the command and every process it started are
        killed, and a Docker container is force-removed, exactly as on timeout.
        """
        if name not in self.allowed_commands:
            raise ExecutionError(
                f"Command {name!r} is not allowed. "
                f"Allowed commands: {', '.join(self.available_commands())}"
            )
        command = list(self.allowed_commands[name])
        if cancel is not None and cancel.cancelled:
            # Already cancelled: do not start the process at all. A command
            # that begins after a cancellation could still write to the repo.
            return CommandResult(name, command, None, "",
                                 f"Not started: {cancel.reason}", False, 0.0,
                                 cancelled=True)
        if name in ALLOWED_COMMANDS and self.sandbox is None:
            return CommandResult(name, command, None, "",
                                 "Repository checks require approved contained execution. "
                                 "Select --sandbox docker and build the sandbox image; "
                                 "host execution is disabled.", False, 0.0, unavailable=True)
        launch, env, container, where = command, command_env(), None, ""
        if self.sandbox:
            container = f"harness-check-{uuid.uuid4().hex[:12]}"
            launch = self.sandbox.wrap(command, self.root, container)
            env, where = docker_client_env(), self.sandbox.describe()

        start = time.monotonic()
        try:
            process = subprocess.Popen(
                launch,                  # a list, so no shell is involved
                cwd=self.root,           # always inside the target repository
                stdout=subprocess.PIPE,  # collect stdout ...
                stderr=subprocess.PIPE,  # ... and stderr
                text=True,               # give us str instead of bytes
                env=env,
                **NEW_PROCESS_GROUP,     # so we can kill everything it starts
            )
        except OSError as error:
            # For example, the program does not exist.
            return CommandResult(name, command, None, "", f"Could not start command: {error}",
                                 False, time.monotonic() - start, sandbox=where, unavailable=True)

        timed_out = cancelled = False
        try:
            stdout, stderr = self._wait(process, container, cancel, start)
        except _Stopped as stopped:
            timed_out, cancelled = stopped.timed_out, stopped.cancelled
            stdout, stderr = stopped.stdout, stopped.stderr
        except BaseException:
            # E.g. Ctrl+C: don't leave the command running behind us.
            kill_process_tree(process)
            self._stop_container(container)
            raise
        finally:
            # Also remove anything the command left running in the background.
            kill_process_tree(process)

        stopped_early = timed_out or cancelled
        output = stdout + stderr
        tests_run, skipped = test_counts(name, output)
        no_tests = (not stopped_early and name in TEST_COMMANDS
                    and (tests_run in (None, 0) or bool(NO_TESTS_PATTERN.search(output))))
        unavailable = (not stopped_early and
                       (bool(re.search(r"No module named ['\"]?(?:pytest|ruff|unittest|compileall)\b", output))
                        or (bool(where) and process.returncode in (125, 126, 127))))
        return CommandResult(
            name=name,
            command=command,
            exit_code=None if stopped_early else process.returncode,
            stdout=stdout,
            stderr=stderr,
            timed_out=timed_out,
            duration_seconds=time.monotonic() - start,
            no_tests_ran=no_tests,
            sandbox=where,
            cancelled=cancelled,
            unavailable=unavailable,
            skipped_tests=skipped,
            tests_run=tests_run,
        )

    def _wait(self, process, container, cancel, start):
        """Wait for the command, checking `cancel` while it runs.

        Raises _Stopped when the command had to be killed, either because the
        timeout ran out or because the run was cancelled. In both cases the
        process group is killed and a Docker container is force-removed, so
        nothing is left running and nothing can write to the repository after
        this returns.
        """
        deadline = start + self.timeout_seconds
        while True:
            slice_seconds = self.timeout_seconds if cancel is None else POLL_SECONDS
            try:
                return process.communicate(timeout=slice_seconds)
            except subprocess.TimeoutExpired:
                pass
            if cancel is not None and cancel.cancelled:
                kill_process_tree(process)
                self._stop_container(container)   # killing the client does not stop it
                stdout, stderr = _collect_after_kill(process)
                raise _Stopped(cancelled=True, stdout=stdout, stderr=stderr)
            if time.monotonic() >= deadline:
                kill_process_tree(process)
                self._stop_container(container)
                stdout, stderr = _collect_after_kill(process)
                raise _Stopped(timed_out=True, stdout=stdout, stderr=stderr)

    def _stop_container(self, container):
        if self.sandbox and container:
            self.sandbox.stop(container)


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


def test_counts(name, output):
    """Read runner summaries, never infer a passing test from exit code alone."""
    if name == "unittest":
        counts = re.findall(r"^Ran (\d+) tests?\b", output, re.MULTILINE)
        skipped = sum(int(n) for n in re.findall(r"(?:skipped|expected failures|unexpected successes)=(\d+)", output))
        not_run = sum(int(n) for n in re.findall(r"skipped=(\d+)", output))
        return (max(0, int(counts[-1]) - not_run) if counts else None), skipped
    if name == "pytest":
        # Only the final summary, not arbitrary test output, supplies counts.
        summaries = [line.strip("= \t") for line in output.splitlines()
                     if re.search(r"\b\d+ (?:passed|failed|skipped|xfailed|xpassed|error|errors)\b", line)
                     and re.search(r"\bin \d", line)]
        summary = summaries[-1] if summaries else ""
        passed_failed = sum(int(n) for n in re.findall(r"(\d+) (?:passed|failed|error|errors)\b", summary))
        skipped = sum(int(n) for n in re.findall(r"(\d+) (?:skipped|xfailed|xpassed)\b", summary))
        return (passed_failed if summary else None), skipped
    return None, 0
