"""Tests for cancelling an active run (cancellation.py and its callers).

All of these are deterministic and need no live model and no real clock
guesswork:

  - a model request is blocked with model_client.Blocked, which waits inside
    the scripted client exactly where a real request would be waiting,
  - a command is blocked with a real subprocess that sleeps and spawns a
    child, so killing the process *group* can actually be observed,
  - Docker is a FakeSandbox that records its stop() calls.

Nothing here relies on Ctrl+C: cancellation is requested through a
CancelToken (this process) or a CancelFile (another process), which is the
point -- a run in another terminal has to be stoppable too.
"""

import os
import subprocess
import sys
import time

import pytest

from cancellation import (CANCEL_SUFFIX, AnyCancel, CancelFile, CancelledError,
                          CancelToken, cancel_path, wait_for)
from controller import CANCELLED, DENIED
from execution import PYTHON, ExecutionEnvironment, kill_process_tree
from model_client import Blocked, ScriptedModelClient
from session import Session, SessionStore, new_session_id
from task_record import TaskSpec
from tests.helpers import PASSING_TEST, commit_all, make_harness, write_repo

RIGHT_HELLO = 'def greet():\n    return "hello"\n'
WRONG_HELLO = 'def greet():\n    return "goodbye"\n'

# A command that spawns a child and then sleeps, so we can prove the whole
# process group dies. Both processes carry the marker in their command line.
MARKER = "harness-cancel-test-marker"


def blocking_command(marker=MARKER):
    return [PYTHON, "-c",
            "import subprocess, sys, time\n"
            f"child = subprocess.Popen([sys.executable, '-c', \"import time; time.sleep(60)  # {marker}\"])\n"
            "sys.stdout.write('started\\n'); sys.stdout.flush()\n"
            "time.sleep(60)\n"]


def marker_processes(marker=MARKER):
    """How many live processes mention `marker` in their command line."""
    done = subprocess.run(["pgrep", "-f", marker], capture_output=True, text=True)
    return len([line for line in done.stdout.split() if line.strip()])


@pytest.fixture
def repo(tmp_path):
    root = write_repo(tmp_path / "repo", {"hello.py": WRONG_HELLO,
                                          "tests/test_hello.py": PASSING_TEST})
    commit_all(root)
    return root


# ===== 1. The cancel sources ==================================================

def test_a_token_latches_and_keeps_the_first_reason():
    token = CancelToken()
    assert not token.cancelled and token.reason == ""

    token.cancel("first reason")
    token.cancel("second reason")        # repeated cancellation is harmless

    assert token.cancelled
    assert token.reason == "first reason"
    with pytest.raises(CancelledError, match="first reason"):
        token.check()


def test_a_cancel_file_is_seen_by_another_process_object(tmp_path):
    """The whole point: two objects, no shared memory, one file."""
    writer = CancelFile(tmp_path, "20261031-120000-aaaaaaaa")
    reader = CancelFile(tmp_path, "20261031-120000-aaaaaaaa")
    assert not reader.cancelled

    path = writer.request("cancelled from another terminal")

    assert path == cancel_path(tmp_path, "20261031-120000-aaaaaaaa")
    assert path.name.endswith(CANCEL_SUFFIX)
    assert reader.cancelled
    assert reader.reason == "cancelled from another terminal"


def test_requesting_a_cancel_twice_is_safe(tmp_path):
    cancel = CancelFile(tmp_path, "20261031-120000-aaaaaaaa")
    cancel.request("first")
    cancel.request("second")             # must not raise, must not duplicate

    assert cancel.cancelled
    assert list(tmp_path.glob(f"*{CANCEL_SUFFIX}")) == [cancel.path]
    assert list(tmp_path.glob("*.tmp")) == []


def test_a_noticed_cancellation_cannot_be_undone(tmp_path):
    """Deleting the file must not bring the run back to life."""
    cancel = CancelFile(tmp_path, "20261031-120000-aaaaaaaa")
    cancel.request("stop")
    assert cancel.cancelled

    cancel.clear()

    assert not cancel.path.exists()
    assert cancel.cancelled               # still latched
    assert cancel.reason == "stop"


def test_clear_lets_a_fresh_reader_start_again(tmp_path):
    """A resumed run must not be stopped by the flag that stopped it before."""
    CancelFile(tmp_path, "20261031-120000-aaaaaaaa").request("stop")
    CancelFile(tmp_path, "20261031-120000-aaaaaaaa").clear()

    assert not CancelFile(tmp_path, "20261031-120000-aaaaaaaa").cancelled


def test_any_cancel_reports_the_source_that_fired(tmp_path):
    token = CancelToken()
    file_cancel = CancelFile(tmp_path, "20261031-120000-aaaaaaaa")
    combined = AnyCancel(token, file_cancel, None)
    assert not combined.cancelled

    file_cancel.request("from another process")

    assert combined.cancelled
    assert combined.reason == "from another process"


def test_wait_for_returns_false_on_timeout_and_raises_on_cancel():
    assert wait_for(lambda: True) is True
    assert wait_for(lambda: False, timeout=0.05, poll_seconds=0.01) is False

    token = CancelToken()
    token.cancel("stop now")
    with pytest.raises(CancelledError, match="stop now"):
        wait_for(lambda: False, cancel=token, timeout=5)


# ===== 2. Cancelling a blocked model request ==================================

def test_a_blocked_model_request_is_cancelled(repo):
    """Requirement: interrupt or safely terminate an active model request."""
    token = CancelToken()
    blocked = Blocked(timeout=10)
    h = make_harness(repo, [blocked], cancel=token)

    # Cancel from "another thread" the moment the client starts waiting.
    import threading
    threading.Thread(target=lambda: (blocked.waiting.wait(5),
                                     token.cancel("cancelled while thinking")),
                     daemon=True).start()

    result = h.controller.run("Wait for a model that never answers")

    assert result.status == CANCELLED
    assert "cancelled while thinking" in result.summary
    assert "no action ran" in result.summary
    assert h.tools.calls == []            # nothing touched the repository


def test_a_late_model_reply_cannot_start_an_action(repo):
    """Requirement: prevent delayed model responses from triggering new actions."""
    token = CancelToken()
    # The blocked reply would ask for an edit if it were ever acted on.
    blocked = Blocked(action={"tool": "edit_file",
                              "arguments": {"path": "hello.py", "content": "OWNED = 1\n"}},
                      timeout=10)
    h = make_harness(repo, [blocked], cancel=token)

    import threading

    def cancel_then_answer():
        blocked.waiting.wait(5)
        token.cancel("cancelled first")
        time.sleep(0.1)
        blocked.release()                 # the model answers, but too late

    threading.Thread(target=cancel_then_answer, daemon=True).start()
    result = h.controller.run("Cancel, then let the model answer")

    assert result.status == CANCELLED
    assert (repo / "hello.py").read_text() == WRONG_HELLO   # the edit never happened
    assert ("edit_file", "hello.py") not in h.tools.calls


def test_cancelling_before_the_first_request_asks_the_model_nothing(repo):
    token = CancelToken()
    token.cancel("stopped before starting")
    h = make_harness(repo, [{"tool": "list_files", "arguments": {}}], cancel=token)

    result = h.controller.run("Should not start")

    assert result.status == CANCELLED
    assert h.model.calls == 0
    assert h.tools.calls == []


# ===== 3. Cancelling a running command, and its children ======================

@pytest.mark.skipif(os.name != "posix", reason="uses pgrep to find child processes")
def test_cancelling_a_command_kills_it_and_its_children(tmp_path):
    """Requirement: stop a running command and its child processes."""
    root = write_repo(tmp_path / "repo", {"hello.py": RIGHT_HELLO})
    token = CancelToken()
    executor = ExecutionEnvironment(root, timeout_seconds=60,
                                    allowed_commands={"slow": blocking_command()})

    import threading
    threading.Thread(target=lambda: (time.sleep(1.0), token.cancel("stop the command")),
                     daemon=True).start()

    started = time.monotonic()
    result = executor.run("slow", cancel=token)
    took = time.monotonic() - started

    assert result.cancelled
    assert not result.succeeded                 # a killed command is never a pass
    assert "CANCELLED" in result.status()
    assert took < 30                            # it did not wait for the timeout
    # The command and the child it spawned are both gone.
    for _ in range(40):
        if marker_processes() == 0:
            break
        time.sleep(0.1)
    assert marker_processes() == 0


def test_a_cancelled_command_is_not_a_passing_check(tmp_path):
    root = write_repo(tmp_path / "repo", {"hello.py": RIGHT_HELLO})
    token = CancelToken()
    token.cancel("stop")
    executor = ExecutionEnvironment(
        root, timeout_seconds=30,
        allowed_commands={"ok": [PYTHON, "-c", "pass"]})   # would exit 0

    result = executor.run("ok", cancel=token)

    assert result.cancelled
    assert not result.succeeded
    assert result.exit_code is None


def test_without_a_cancel_source_a_command_still_times_out(tmp_path):
    """The timeout must keep working when nothing is cancellable."""
    root = write_repo(tmp_path / "repo", {"hello.py": RIGHT_HELLO})
    executor = ExecutionEnvironment(
        root, timeout_seconds=1,
        allowed_commands={"slow": [PYTHON, "-c", "import time; time.sleep(30)"]})

    result = executor.run("slow")

    assert result.timed_out
    assert not result.cancelled
    assert not result.succeeded


# ===== 4. Docker containers are cleaned up ====================================

class FakeSandbox:
    """Runs a slow local command instead of docker and records stop() calls."""

    def __init__(self):
        self.stopped = []
        self.container = None

    def describe(self):
        return "fake docker"

    def wrap(self, command, root, container_name):
        self.container = container_name
        return [PYTHON, "-c", "import time; time.sleep(60)"]

    def stop(self, container_name):
        self.stopped.append(container_name)


def test_cancelling_removes_the_docker_container(tmp_path):
    """Requirement: clean up Docker containers.

    Killing the local `docker run` client does not stop the container, so the
    sandbox has to be told to remove it explicitly -- exactly as on timeout.
    """
    root = write_repo(tmp_path / "repo", {"hello.py": RIGHT_HELLO})
    sandbox = FakeSandbox()
    token = CancelToken()
    executor = ExecutionEnvironment(root, timeout_seconds=60, sandbox=sandbox,
                                    allowed_commands={"slow": [PYTHON, "-c", "pass"]})

    import threading
    threading.Thread(target=lambda: (time.sleep(0.6), token.cancel("stop")),
                     daemon=True).start()
    result = executor.run("slow", cancel=token)

    assert result.cancelled
    assert sandbox.stopped == [sandbox.container]      # force-removed exactly once
    assert sandbox.container.startswith("harness-check-")


def test_repeated_container_cleanup_is_safe(tmp_path):
    """stop() is called on cancel and again in the cleanup path; both must be fine."""
    root = write_repo(tmp_path / "repo", {"hello.py": RIGHT_HELLO})
    sandbox = FakeSandbox()
    token = CancelToken()
    token.cancel("stop")
    executor = ExecutionEnvironment(root, timeout_seconds=5, sandbox=sandbox,
                                    allowed_commands={"slow": [PYTHON, "-c", "pass"]})

    executor.run("slow", cancel=token)
    sandbox.stop(sandbox.container)        # a second removal must not raise

    assert len(sandbox.stopped) >= 1


# ===== 5. No further edits after cancellation =================================

def test_no_file_is_edited_after_cancellation(repo):
    """Requirement: prevent additional file edits after cancellation."""
    token = CancelToken()
    h = make_harness(repo, [
        {"tool": "read_file", "arguments": {"path": "hello.py"}},
        {"tool": "edit_file", "arguments": {"path": "hello.py", "content": "OWNED = 1\n"}},
        {"tool": "replace_in_file", "arguments": {"path": "hello.py",
                                                  "old": 'return "goodbye"',
                                                  "new": "OWNED"}},
    ], cancel=token, allowed_changes=("hello.py",))

    original = h.controller.execute

    def execute_then_cancel(action):
        outcome = original(action)
        token.cancel("cancelled by the user")
        return outcome

    h.controller.execute = execute_then_cancel
    result = h.controller.run("Read, then get cancelled before editing")

    assert result.status == CANCELLED
    assert result.changed_files == []
    assert (repo / "hello.py").read_text() == WRONG_HELLO
    assert h.model.calls == 1                    # never asked for a second action


def test_an_edit_reaching_the_gate_after_cancellation_is_denied(repo):
    """Belt and braces: even called directly, a write is refused once cancelled."""
    token = CancelToken()
    h = make_harness(repo, [], cancel=token, allowed_changes=("hello.py",))
    token.cancel("cancelled by the user")

    from model_client import Action
    outcome, text = h.controller.execute(
        Action(tool="edit_file", arguments={"path": "hello.py", "content": "OWNED = 1\n"}))

    assert outcome == DENIED
    assert "the run was cancelled" in text
    assert (repo / "hello.py").read_text() == WRONG_HELLO


# ===== 6. The cancelled state is persisted ====================================

def test_the_cancelled_state_is_saved(repo, tmp_path):
    """Requirement: persist the cancelled task state."""
    store = SessionStore(tmp_path / "sessions")
    spec = TaskSpec(request="Get cancelled", project="demo", root=str(repo),
                    mode="edit", allowed_changes=("hello.py",), checks=("unittest",),
                    limits={"max_actions": 20, "max_retries": 3})
    session = Session(id=new_session_id(), spec=spec)
    token = CancelToken()
    h = make_harness(repo, [
        {"tool": "read_file", "arguments": {"path": "hello.py"}},
        {"tool": "edit_file", "arguments": {"path": "hello.py", "content": "OWNED = 1\n"}},
    ], cancel=token, session=session, store=store, allowed_changes=("hello.py",))

    original = h.controller.execute

    def execute_then_cancel(action):
        outcome = original(action)
        token.cancel("cancelled from another terminal")
        return outcome

    h.controller.execute = execute_then_cancel
    h.controller.run(spec.request)

    # A completely fresh store, as a new process would have.
    saved = SessionStore(store.directory).load(session.id)
    assert saved.status == CANCELLED
    assert "cancelled from another terminal" in saved.summary
    assert [a.tool for a in saved.actions] == ["read_file"]
    assert saved.checkpoint.counters["actions"] == 1


def test_a_cancellation_during_a_command_is_recorded_as_cancelled(repo, tmp_path):
    """Requirement: persist the cancelled task state, mid-command included.

    A cooperative cancellation is NOT uncertain. The command is killed, the
    kill is observed, and the result is written down as CANCELLED -- which is
    a *known* outcome, so it must not be dressed up as an unknown one. Real
    uncertainty needs a hard kill, where nobody gets to record anything; that
    is the `pending` mechanism, covered in test_sessions.py.
    """
    store = SessionStore(tmp_path / "sessions")
    spec = TaskSpec(request="Cancel during a check", project="demo", root=str(repo),
                    mode="edit", allowed_changes=("hello.py",), checks=("unittest",),
                    limits={"max_actions": 20, "max_retries": 3})
    session = Session(id=new_session_id(), spec=spec)
    token = CancelToken()
    h = make_harness(repo, [
        {"tool": "run_check", "arguments": {}},
        {"tool": "edit_file", "arguments": {"path": "hello.py", "content": "OWNED = 1\n"}},
    ], cancel=token, session=session, store=store, allowed_changes=("hello.py",))
    h.controller.executor.allowed_commands = {
        "unittest": [PYTHON, "-c", "import time; time.sleep(60)"]}

    import threading
    threading.Thread(target=lambda: (time.sleep(0.8), token.cancel("stopped mid-check")),
                     daemon=True).start()
    result = h.controller.run(spec.request)

    assert result.status == CANCELLED
    assert result.last_check.cancelled
    assert not result.last_check.succeeded          # a killed check is never a pass
    assert "CANCELLED" in result.last_check.status()
    # The edit that was queued behind the check never happened.
    assert (repo / "hello.py").read_text() == WRONG_HELLO

    saved = SessionStore(store.directory).load(session.id)
    assert saved.status == CANCELLED
    assert "stopped mid-check" in saved.summary
    check = [a for a in saved.actions if a.tool == "run_check"][0]
    assert "CANCELLED" in check.check               # the outcome is recorded, not guessed


def test_stopping_does_not_clear_a_pending_action(repo, tmp_path):
    """_stop must never turn an unknown outcome into a known one.

    If an action really is still in flight when the run stops, the checkpoint
    has to keep saying so, or reopening would wrongly look clean.
    """
    store = SessionStore(tmp_path / "sessions")
    spec = TaskSpec(request="x", project="demo", root=str(repo), mode="edit",
                    allowed_changes=("hello.py",), checks=("unittest",),
                    limits={"max_actions": 20, "max_retries": 3})
    session = Session(id=new_session_id(), spec=spec)
    token = CancelToken()
    h = make_harness(repo, [{"tool": "list_files", "arguments": {}}],
                     cancel=token, session=session, store=store)
    h.controller.run(spec.request)

    # Put an action back in flight, as a hard kill would have left it, then stop.
    session.checkpoint.pending = {"step": 9, "tool": "edit_file",
                                  "arguments": {"path": "hello.py"}}
    store.save(session)
    h.controller._stop(CANCELLED, "stopped again")

    saved = SessionStore(store.directory).load(session.id)
    assert saved.checkpoint.pending is not None
    assert saved.checkpoint.pending["tool"] == "edit_file"
    assert SessionStore(store.directory).reopen(session.id).has_uncertain_action


def test_repeated_cancellation_of_a_run_is_safe(repo):
    """Requirement: handle repeated cancellation safely."""
    token = CancelToken()
    h = make_harness(repo, [{"tool": "list_files", "arguments": {}}] * 3, cancel=token)

    for _ in range(5):                 # five cancellations, same run
        token.cancel("stop")

    first = h.controller.run("Cancelled many times over")
    second = h.controller.run("And run again while still cancelled")

    assert first.status == CANCELLED
    assert second.status == CANCELLED  # still cancelled, still safe
    assert h.model.calls == 0


# ===== 7. Cancelling from a second process, for real ==========================

@pytest.mark.skipif(os.name != "posix", reason="starts real OS processes")
def test_another_process_cancels_a_running_task(tmp_path):
    """The headline requirement, end to end with two real processes.

    Process 1 runs a task whose check blocks. Process 2 is a separate OS
    process (`main.py --cancel`). Afterwards: the run stopped, the command and
    its child are gone, the pending edit never happened, and the state is saved.
    """
    harness = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    root = write_repo(tmp_path / "repo", {"hello.py": WRONG_HELLO})
    commit_all(root)
    sessions = tmp_path / "sessions"
    marker = "harness-xproc-marker"

    runner = tmp_path / "runner.py"
    runner.write_text(
        "import sys\n"
        f"sys.path.insert(0, {harness!r})\n"
        "import main as m, execution\n"
        "from model_client import ScriptedModelClient\n"
        "execution.ALLOWED_COMMANDS['unittest'] = [sys.executable, '-c',\n"
        "    \"import subprocess, sys, time;\"\n"
        f"    \"subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(60)  # {marker}']);\"\n"
        "    \"time.sleep(60)\"]\n"
        "m.build_model = lambda config: ScriptedModelClient([\n"
        "    {'tool': 'run_check', 'arguments': {}},\n"
        "    {'tool': 'edit_file', 'arguments': {'path': 'hello.py', 'content': 'OWNED = 1\\n'}},\n"
        "])\n"
        f"sys.exit(m.main(['--root', {str(root)!r}, '--task', 'block', '--mode', 'edit',\n"
        f"    '--model', 'scripted', '--sandbox', 'none', '--session-dir', {str(sessions)!r},\n"
        "    '--allow', 'hello.py']))\n"
    )

    process = subprocess.Popen([PYTHON, str(runner)], stdout=subprocess.PIPE,
                               stderr=subprocess.STDOUT, text=True,
                               start_new_session=True)
    try:
        # Wait until the session file says run_check is actually in flight.
        session_id = None
        for _ in range(80):
            files = [p for p in sessions.glob("*.json") if not p.name.endswith(".tmp")]
            if files and '"tool": "run_check"' in files[0].read_text():
                session_id = files[0].stem
                break
            time.sleep(0.25)
        assert session_id, "the run never reached run_check"

        # A genuinely separate process does the cancelling.
        cancelling = subprocess.run(
            [PYTHON, os.path.join(harness, "main.py"), "--cancel", session_id,
             "--session-dir", str(sessions), "--reason", "cancelled from another terminal"],
            capture_output=True, text=True, timeout=60)
        assert cancelling.returncode == 0, cancelling.stderr
        assert "Cancellation requested" in cancelling.stdout

        output = process.communicate(timeout=60)[0]
    finally:
        if process.poll() is None:
            kill_process_tree(process)
        subprocess.run(["pkill", "-f", marker], capture_output=True)

    assert "CANCELLED" in output
    assert "cancelled from another terminal" in output
    assert "No further actions were executed" in output

    # The pending edit never ran.
    assert (root / "hello.py").read_text() == WRONG_HELLO
    assert "OWNED" not in (root / "hello.py").read_text()

    # The blocked command's child is gone.
    for _ in range(40):
        if marker_processes(marker) == 0:
            break
        time.sleep(0.1)
    assert marker_processes(marker) == 0

    # And the state was saved for a later reopen.
    saved = SessionStore(sessions).load(session_id)
    assert saved.status == CANCELLED
    assert "cancelled from another terminal" in saved.summary


def test_cancelling_an_unknown_session_is_a_clear_error(tmp_path):
    import main
    code = main.main(["--cancel", "20261031-120000-aaaaaaaa",
                      "--session-dir", str(tmp_path / "none")])
    assert code == 2


def test_cancelling_a_finished_session_says_so(tmp_path, capsys):
    import main
    store = SessionStore(tmp_path / "sessions")
    spec = TaskSpec(request="already over", project="p", root=str(tmp_path))
    session = Session(id=new_session_id(), spec=spec, status="done")
    store.save(session)

    code = main.main(["--cancel", session.id, "--session-dir", str(store.directory)])

    assert code == 0
    assert "already finished" in capsys.readouterr().out


def test_cancellation_does_not_depend_on_ctrl_c():
    """The brief says not to rely only on Ctrl+C: nothing here uses SIGINT."""
    import cancellation
    source = open(cancellation.__file__, encoding="utf-8").read()
    assert "SIGINT" not in source
    assert "KeyboardInterrupt" not in source
    # SIGINT is only an extra convenience, installed by the lifecycle service.
    import application
    assert "signal.signal" in open(application.__file__, encoding="utf-8").read()
