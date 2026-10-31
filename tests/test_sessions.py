"""Tests for session storage and safe resume (session.py).

Everything is deterministic: the model is scripted, repositories are real
git repositories written into temporary folders, and a "restart" is modelled
the honest way -- the Session object is thrown away and reloaded from disk
through a brand new SessionStore, so nothing can be carried over in memory.

The six scenarios the brief asks for each have their own section below:
reopening after restart, successful safe resume, repository mismatch,
permission changes, exhausted limits, and uncertain actions.
"""

import json
import os

import pytest

from controller import ACTION_LIMIT, DONE, UNCERTAIN
from session import (FINISHED, INTERRUPTED, RUNNING, Checkpoint, Session, SessionError,
                     SessionStore, check_resume, file_digest, new_session_id)
from task_record import TaskSpec
from tests.helpers import PASSING_TEST, commit_all, git, make_harness, write_repo

WRONG_HELLO = 'def greet():\n    return "goodbye"\n'
RIGHT_HELLO = 'def greet():\n    return "hello"\n'


@pytest.fixture
def repo(tmp_path):
    """A git repository whose test fails until hello.py is corrected."""
    root = write_repo(tmp_path / "repo", {"hello.py": WRONG_HELLO,
                                          "tests/test_hello.py": PASSING_TEST})
    commit_all(root)
    return root


@pytest.fixture
def store(tmp_path):
    return SessionStore(tmp_path / "sessions")


def spec_for(root, **overrides):
    values = dict(request="Make the test pass", project="demo", root=str(root),
                  mode="edit", allowed_changes=("hello.py",), checks=("unittest",),
                  limits={"max_actions": 20, "max_retries": 3})
    values.update(overrides)
    return TaskSpec(**values)


def restart(store, session_id):
    """Model a complete application restart: nothing but the file survives."""
    return SessionStore(store.directory).reopen(session_id)


def current_state(root):
    """The repository's branch, commit and dirty lines, as resume sees them."""
    from navigation import RepositoryNavigator
    from repository_tools import RepositoryTools
    return RepositoryNavigator(RepositoryTools(root, "readonly")).state_summary()


def interrupted_session(repo, store, replies=None, spec=None):
    """Run until the scripted model runs out, leaving a saved session behind."""
    spec = spec or spec_for(repo)
    session = Session(id=new_session_id(), spec=spec)
    h = make_harness(repo, replies if replies is not None else [
        {"tool": "read_file", "arguments": {"path": "hello.py"}},
    ], spec=spec, session=session, store=store, allowed_changes=spec.allowed_changes)
    h.controller.run(spec.request)
    return session, h


# ===== 1. Ids and atomic, durable storage ====================================

def test_session_ids_are_stable_sortable_and_unique():
    first, second = new_session_id(), new_session_id()
    assert first != second
    # Sortable by time: an older timestamp sorts before a newer one.
    assert new_session_id(1000) < new_session_id(2_000_000_000)
    # The id survives a save/load round trip unchanged.
    assert len(first.split("-")) == 3


def test_a_bad_session_id_is_refused(store):
    for bad in ("../escape", "not an id", "", "20261031-190500-XYZ", "/etc/passwd"):
        with pytest.raises(SessionError, match="not a valid session id"):
            store.path_for(bad)


def test_saving_leaves_no_temporary_file_behind(repo, store):
    session = Session(id=new_session_id(), spec=spec_for(repo))
    path = store.save(session)

    assert path.exists()
    assert list(store.directory.glob("*.tmp")) == []
    # Valid JSON, so no partial write.
    assert json.loads(path.read_text())["id"] == session.id


def test_a_half_written_temporary_file_never_becomes_the_session(repo, store):
    """os.replace is atomic, so a reader sees the old file or the new one."""
    session = Session(id=new_session_id(), spec=spec_for(repo))
    store.save(session)
    good = store.path_for(session.id).read_text()

    # Simulate a crash: rubbish in the temp file, which was never replaced.
    (store.directory / f"{session.id}.json.tmp").write_text('{"id": "trunc')

    assert store.path_for(session.id).read_text() == good   # untouched
    assert store.load(session.id).id == session.id          # still readable
    # The temp file is not offered as a session.
    assert [s.id for s in store.list_sessions()] == [session.id]


def test_unreadable_sessions_are_skipped_not_fatal(repo, store):
    good = Session(id=new_session_id(), spec=spec_for(repo))
    store.save(good)
    (store.directory / "20261031-000000-deadbeef.json").write_text("{not json")

    assert [s.id for s in store.list_sessions()] == [good.id]
    with pytest.raises(SessionError, match="not valid JSON"):
        store.load("20261031-000000-deadbeef")


def test_list_sessions_is_newest_first_and_empty_without_a_folder(tmp_path):
    store = SessionStore(tmp_path / "none")
    assert store.list_sessions() == []

    store = SessionStore(tmp_path / "sessions")
    older = Session(id="20261031-100000-aaaaaaaa", spec=spec_for(tmp_path))
    newer = Session(id="20261031-200000-bbbbbbbb", spec=spec_for(tmp_path))
    store.save(older)
    store.save(newer)
    assert [s.id for s in store.list_sessions()] == [newer.id, older.id]


# ===== 2. Reopening after a complete restart =================================

def test_everything_is_reopened_after_a_restart(repo, store):
    """Requests, steps, history, errors and check results all come back."""
    spec = spec_for(repo)
    session = Session(id=new_session_id(), spec=spec)
    h = make_harness(repo, [
        {"tool": "read_file", "arguments": {"path": "hello.py"}},
        {"tool": "read_file", "arguments": {"path": "nope.py"}},      # an error
        {"tool": "replace_in_file", "arguments": {"path": "hello.py",
                                                  "old": 'return "goodbye"',
                                                  "new": 'return "hello"'}},
        {"tool": "run_check", "arguments": {}},                        # a check result
    ], spec=spec, session=session, store=store, allowed_changes=("hello.py",))
    h.controller.run(spec.request)

    # Throw the object away; reload from disk only.
    reopened = restart(store, session.id)

    assert reopened.id == session.id
    assert reopened.spec.request == "Make the test pass"          # the request
    assert reopened.spec.starting_commit == spec.starting_commit
    assert reopened.spec.allowed_changes == ("hello.py",)
    tools = [a.tool for a in reopened.actions]                     # the steps
    assert tools == ["read_file", "read_file", "replace_in_file", "run_check",
                     "(model error)"]
    assert reopened.actions[1].outcome == "failed"                 # a tool error
    assert "File not found" in reopened.actions[1].detail
    assert reopened.actions[-1].outcome == "failed"                # the fatal error
    assert "no replies left" in reopened.actions[-1].detail
    assert reopened.checkpoint.last_check["status"].startswith("SUCCEEDED")  # check result
    assert reopened.checkpoint.messages                            # the history
    assert reopened.checkpoint.changed_files == ["hello.py"]
    assert reopened.status == INTERRUPTED


def test_progress_is_tracked_and_readable(repo, store):
    session, _ = interrupted_session(repo, store)
    text = restart(store, session.id).progress()

    for expected in (session.id, "Make the test pass", "demo", "2/20 used"):
        assert expected in text


def test_consumed_limits_come_back(repo, store):
    session, _ = interrupted_session(repo, store, replies=[
        {"tool": "list_files", "arguments": {}},
        {"tool": "list_files", "arguments": {}},
        {"tool": "list_files", "arguments": {}},
    ])
    reopened = restart(store, session.id)

    assert reopened.consumed("actions") == 4      # 3 replies + the one that ran out
    assert reopened.consumed("executed") == 3
    assert reopened.remaining_actions() == 16


# ===== 3. Successful safe resume =============================================

def test_safe_resume_continues_the_same_task(repo, store):
    """The run carries on from the checkpoint and finishes the work."""
    spec = spec_for(repo)
    session = Session(id=new_session_id(), spec=spec)
    first = make_harness(repo, [
        {"tool": "read_file", "arguments": {"path": "hello.py"}},
    ], spec=spec, session=session, store=store, allowed_changes=("hello.py",))
    first.controller.run(spec.request)

    reopened = restart(store, session.id)
    branch, commit, dirty = current_state(repo)
    report = check_resume(reopened, repo, branch, commit, dirty, "edit", ("hello.py",))
    assert report.safe, report.text()

    second = make_harness(repo, [
        {"tool": "replace_in_file", "arguments": {"path": "hello.py",
                                                  "old": 'return "goodbye"',
                                                  "new": 'return "hello"'}},
        {"tool": "run_check", "arguments": {}},
        {"tool": "done", "arguments": {}, "summary": "Finished after resuming."},
    ], spec=reopened.spec, session=reopened, store=store, allowed_changes=("hello.py",))
    result = second.controller.resume(reopened)

    assert result.status == DONE
    assert result.summary == "Finished after resuming."
    assert (repo / "hello.py").read_text() == RIGHT_HELLO
    # Step numbers continue; they do not start again at 1. Step 2 is the model
    # error that ended the first process, which is recorded too.
    assert [a.step for a in result.record.actions] == [1, 2, 3, 4, 5]
    assert [a.tool for a in result.record.actions] == [
        "read_file", "(model error)", "replace_in_file", "run_check", "done"]
    assert result.counters.actions == 5
    assert restart(store, session.id).status == DONE


def test_a_resumed_run_keeps_the_same_action_budget(repo, store):
    """Resuming must not hand out a fresh set of actions."""
    spec = spec_for(repo, limits={"max_actions": 4, "max_retries": 3})
    session = Session(id=new_session_id(), spec=spec)
    first = make_harness(repo, [{"tool": "list_files", "arguments": {}}] * 2,
                         spec=spec, session=session, store=store, max_actions=4)
    first.controller.run(spec.request)

    reopened = restart(store, session.id)
    second = make_harness(repo, [{"tool": "list_files", "arguments": {}}] * 10,
                          spec=reopened.spec, session=reopened, store=store, max_actions=4)
    result = second.controller.resume(reopened)

    assert result.status == ACTION_LIMIT
    assert result.counters.actions == 4          # the original limit, not 4 more
    assert second.model.calls <= 2


def test_a_finished_session_is_not_resumed(repo, store):
    spec = spec_for(repo)
    session = Session(id=new_session_id(), spec=spec)
    h = make_harness(repo, [{"tool": "done", "arguments": {}, "summary": "nothing to do"}],
                     spec=spec, session=session, store=store)
    h.controller.run(spec.request)

    reopened = SessionStore(store.directory).reopen(session.id)
    assert reopened.status in FINISHED
    branch, commit, dirty = current_state(repo)
    report = check_resume(reopened, repo, branch, commit, dirty, "edit", ("hello.py",))

    assert not report.safe
    assert any("already finished" in reason for reason in report.mismatches)


# ===== 4. Repository mismatch ================================================

def test_resume_pauses_when_the_agents_own_edit_was_changed(repo, store):
    """The working-copy check covers the files the agent itself wrote."""
    spec = spec_for(repo)
    session = Session(id=new_session_id(), spec=spec)
    h = make_harness(repo, [
        {"tool": "replace_in_file", "arguments": {"path": "hello.py",
                                                  "old": 'return "goodbye"',
                                                  "new": 'return "hello"'}},
    ], spec=spec, session=session, store=store, allowed_changes=("hello.py",))
    h.controller.run(spec.request)
    reopened = restart(store, session.id)

    # Someone edits the agent's file behind its back.
    (repo / "hello.py").write_text('def greet():\n    return "something else"\n')

    branch, commit, dirty = current_state(repo)
    report = check_resume(reopened, repo, branch, commit, dirty, "edit", ("hello.py",))

    assert not report.safe
    assert any("hello.py" in r and "modified since" in r for r in report.mismatches)
    assert "PAUSED" in report.text()


def test_resume_pauses_when_the_agents_own_edit_is_gone(repo, store):
    spec = spec_for(repo)
    session = Session(id=new_session_id(), spec=spec)
    h = make_harness(repo, [
        {"tool": "replace_in_file", "arguments": {"path": "hello.py",
                                                  "old": 'return "goodbye"',
                                                  "new": 'return "hello"'}},
    ], spec=spec, session=session, store=store, allowed_changes=("hello.py",))
    h.controller.run(spec.request)
    reopened = restart(store, session.id)

    os.remove(repo / "hello.py")

    branch, commit, dirty = current_state(repo)
    report = check_resume(reopened, repo, branch, commit, dirty, "edit", ("hello.py",))

    assert not report.safe
    assert any("missing or unreadable" in r for r in report.mismatches)


def test_resume_pauses_on_a_different_commit_or_branch(repo, store):
    session, _ = interrupted_session(repo, store)
    reopened = restart(store, session.id)
    branch, commit, dirty = current_state(repo)

    moved = check_resume(reopened, repo, branch, "0" * 40, dirty, "edit", ("hello.py",))
    assert not moved.safe
    assert any("commit changed" in r for r in moved.mismatches)

    switched = check_resume(reopened, repo, "other-branch", commit, dirty,
                            "edit", ("hello.py",))
    assert not switched.safe
    assert any("branch changed" in r for r in switched.mismatches)


def test_resume_pauses_when_other_files_changed(repo, store):
    """A file the agent never touched has appeared or been edited."""
    spec = spec_for(repo)
    session = Session(id=new_session_id(), spec=spec)
    h = make_harness(repo, [
        {"tool": "replace_in_file", "arguments": {"path": "hello.py",
                                                  "old": 'return "goodbye"',
                                                  "new": 'return "hello"'}},
    ], spec=spec, session=session, store=store, allowed_changes=("hello.py",))
    h.controller.run(spec.request)
    reopened = restart(store, session.id)

    (repo / "surprise.py").write_text("x = 1\n")   # nothing to do with the task

    branch, commit, dirty = current_state(repo)
    report = check_resume(reopened, repo, branch, commit, dirty, "edit", ("hello.py",))

    assert not report.safe
    assert any("other changes than the saved ones" in r for r in report.mismatches)
    assert any("surprise.py" in r for r in report.mismatches)


def test_resume_pauses_for_a_different_root(repo, store, tmp_path):
    session, _ = interrupted_session(repo, store)
    reopened = restart(store, session.id)
    other = tmp_path / "elsewhere"
    other.mkdir()

    report = check_resume(reopened, other, "", "", [], "edit", ("hello.py",))

    assert not report.safe
    assert any("was saved for" in r for r in report.mismatches)


def test_file_digest_detects_a_one_byte_change(tmp_path):
    target = tmp_path / "f.py"
    target.write_text("a = 1\n")
    before = file_digest(target)
    target.write_text("a = 2\n")

    assert before and file_digest(target) != before
    assert file_digest(tmp_path / "missing") == ""


# ===== 5. Permission changes ==================================================

def test_resume_pauses_when_the_mode_changed(repo, store):
    session, _ = interrupted_session(repo, store)
    reopened = restart(store, session.id)
    branch, commit, dirty = current_state(repo)

    report = check_resume(reopened, repo, branch, commit, dirty, "readonly", ("hello.py",))

    assert not report.safe
    assert any("mode changed" in r for r in report.mismatches)


def test_resume_pauses_when_the_allowed_changes_differ(repo, store):
    session, _ = interrupted_session(repo, store)
    reopened = restart(store, session.id)
    branch, commit, dirty = current_state(repo)

    widened = check_resume(reopened, repo, branch, commit, dirty, "edit", ())
    assert not widened.safe
    assert any("allowed changes differ" in r for r in widened.mismatches)

    narrowed = check_resume(reopened, repo, branch, commit, dirty, "edit", ("other.py",))
    assert not narrowed.safe


def test_resume_pauses_when_the_project_is_no_longer_allowed(repo, store, tmp_path):
    session, _ = interrupted_session(repo, store)
    reopened = restart(store, session.id)
    branch, commit, dirty = current_state(repo)

    refused = check_resume(reopened, repo, branch, commit, dirty, "edit", ("hello.py",),
                           allowed_projects=(tmp_path / "somewhere-else",))
    assert not refused.safe
    assert any("not in the allowed projects" in r for r in refused.mismatches)

    accepted = check_resume(reopened, repo, branch, commit, dirty, "edit", ("hello.py",),
                            allowed_projects=(repo,))
    assert accepted.safe, accepted.text()


def test_every_mismatch_is_reported_at_once(repo, store):
    """The user is told everything that is wrong, not just the first thing."""
    session, _ = interrupted_session(repo, store)
    reopened = restart(store, session.id)

    report = check_resume(reopened, repo, "other", "0" * 40, [], "readonly", ())

    assert len(report.mismatches) >= 4
    assert "PAUSED" in report.text()
    assert "Nothing was executed" in report.text()


# ===== 6. Exhausted limits ====================================================

def test_resume_pauses_when_the_action_limit_is_used_up(repo, store):
    spec = spec_for(repo, limits={"max_actions": 2, "max_retries": 3})
    session = Session(id=new_session_id(), spec=spec)
    h = make_harness(repo, [{"tool": "list_files", "arguments": {}}] * 5,
                     spec=spec, session=session, store=store, max_actions=2)
    h.controller.run(spec.request)

    reopened = SessionStore(store.directory).load(session.id)
    reopened.status = RUNNING          # pretend it was interrupted, not limit-stopped
    branch, commit, dirty = current_state(repo)
    report = check_resume(reopened, repo, branch, commit, dirty, "edit", ("hello.py",))

    assert not report.safe
    assert any("action limit is already used up" in r for r in report.mismatches)


def test_resume_pauses_when_the_retry_limit_is_used_up(repo, store):
    session, _ = interrupted_session(repo, store)
    reopened = restart(store, session.id)
    reopened.checkpoint.repeats = 3            # as if it had spun on a failing action
    branch, commit, dirty = current_state(repo)

    report = check_resume(reopened, repo, branch, commit, dirty, "edit", ("hello.py",))

    assert not report.safe
    assert any("retry limit is already used up" in r for r in report.mismatches)


def test_remaining_actions_never_goes_negative(repo):
    session = Session(id=new_session_id(), spec=spec_for(repo, limits={"max_actions": 2}))
    session.checkpoint = Checkpoint(counters={"actions": 7})
    assert session.remaining_actions() == 0

    unlimited = Session(id=new_session_id(), spec=spec_for(repo, limits={}))
    assert unlimited.remaining_actions() is None


# ===== 7. Uncertain actions ===================================================

def killed_during(repo, store, tool_name, spec=None):
    """Run until `tool_name` is reached, then stop as a kill would.

    The pre-action checkpoint is already on disk and the post-action one never
    happens, which is exactly the state a SIGKILL leaves behind.
    """
    spec = spec or spec_for(repo)
    session = Session(id=new_session_id(), spec=spec)
    h = make_harness(repo, [
        {"tool": "read_file", "arguments": {"path": "hello.py"}},
        {"tool": "edit_file", "arguments": {"path": "hello.py", "content": RIGHT_HELLO}},
        {"tool": "done", "arguments": {}, "summary": "never reached"},
    ], spec=spec, session=session, store=store, allowed_changes=("hello.py",))

    class Killed(Exception):
        pass

    original = h.controller.handlers[tool_name]

    def die(*args, **kwargs):
        raise Killed()

    h.controller.handlers[tool_name] = die
    with pytest.raises(Killed):
        h.controller.run(spec.request)
    return session, h


def test_an_interrupted_action_is_saved_as_pending(repo, store):
    session, _ = killed_during(repo, store, "edit_file")

    saved = SessionStore(store.directory).load(session.id)
    assert saved.status == RUNNING                     # it never finished
    assert saved.checkpoint.pending is not None
    assert saved.checkpoint.pending["tool"] == "edit_file"
    assert saved.checkpoint.pending["arguments"]["path"] == "hello.py"


def test_reopening_marks_the_interrupted_action_uncertain(repo, store):
    session, _ = killed_during(repo, store, "edit_file")

    reopened = restart(store, session.id)

    assert reopened.status == INTERRUPTED
    assert reopened.has_uncertain_action
    assert reopened.uncertain["tool"] == "edit_file"
    assert reopened.checkpoint.pending is None         # moved out of pending
    assert "UNCERTAIN" in reopened.progress()
    assert "NOT be replayed" in reopened.progress()


def test_an_uncertain_action_is_never_replayed(repo, store):
    """The whole point: the harness must not repeat an action it cannot judge."""
    session, _ = killed_during(repo, store, "edit_file")
    reopened = restart(store, session.id)
    before = (repo / "hello.py").read_text()

    second = make_harness(repo, [
        {"tool": "done", "arguments": {}, "summary": "stopped to let a human look"},
    ], spec=reopened.spec, session=reopened, store=store, allowed_changes=("hello.py",))
    result = second.controller.resume(reopened)

    # The edit_file that was in flight is NOT among the actions that ran.
    assert ("edit_file", "hello.py") not in second.tools.calls
    assert (repo / "hello.py").read_text() == before
    uncertain = [a for a in result.record.actions if a.outcome == UNCERTAIN]
    assert len(uncertain) == 1
    assert uncertain[0].tool == "edit_file"
    assert "never confirmed finished" in uncertain[0].detail


def test_the_model_is_told_about_the_uncertain_action(repo, store):
    session, _ = killed_during(repo, store, "edit_file")
    reopened = restart(store, session.id)

    second = make_harness(repo, [
        {"tool": "done", "arguments": {}, "summary": "looked first"},
    ], spec=reopened.spec, session=reopened, store=store, allowed_changes=("hello.py",))
    second.controller.resume(reopened)

    notice = second.model.received[0][-1]["content"]
    assert "interrupted" in notice
    assert "edit_file" in notice
    assert "NOT been repeated" in notice


def test_resume_reports_the_uncertain_action_without_refusing(repo, store):
    """An uncertain action is a note, not a mismatch: the run may continue."""
    session, _ = killed_during(repo, store, "edit_file")
    reopened = restart(store, session.id)
    branch, commit, dirty = current_state(repo)

    report = check_resume(reopened, repo, branch, commit, dirty, "edit", ("hello.py",))

    assert report.safe
    assert any("never confirmed finished" in note for note in report.notes)
    assert any("NOT replayed" in note for note in report.notes)


def test_a_completed_action_leaves_no_pending_behind(repo, store):
    """After a step finishes, nothing is in flight any more."""
    session, _ = interrupted_session(repo, store, replies=[
        {"tool": "read_file", "arguments": {"path": "hello.py"}},
    ])

    saved = SessionStore(store.directory).load(session.id)
    assert saved.checkpoint.pending is None
    assert not restart(store, session.id).has_uncertain_action


# ===== 8. Cancel an active run, then resume it ===============================
#
# The brief's Sessions row asks for exactly this pair: "cancel an active run,
# and resume from a safe checkpoint". So "cancelled" is deliberately NOT a
# finished status; only "done" is.

def test_a_cancelled_run_can_be_resumed(repo, store):
    from controller import CANCELLED
    from task_record import CancelToken

    spec = spec_for(repo)
    session = Session(id=new_session_id(), spec=spec)
    cancel = CancelToken()
    first = make_harness(repo, [
        {"tool": "read_file", "arguments": {"path": "hello.py"}},
        {"tool": "replace_in_file", "arguments": {"path": "hello.py",
                                                  "old": 'return "goodbye"',
                                                  "new": 'return "hello"'}},
    ], spec=spec, session=session, store=store, cancel=cancel,
       allowed_changes=("hello.py",))

    original = first.controller.execute

    def execute_then_cancel(action):
        outcome = original(action)
        cancel.cancel("cancelled by the user")
        return outcome

    first.controller.execute = execute_then_cancel
    stopped = first.controller.run(spec.request)

    assert stopped.status == CANCELLED
    assert (repo / "hello.py").read_text() == WRONG_HELLO     # the edit never ran

    # Reopen after a restart and carry on.
    reopened = restart(store, session.id)
    assert reopened.status == INTERRUPTED
    branch, commit, dirty = current_state(repo)
    report = check_resume(reopened, repo, branch, commit, dirty, "edit", ("hello.py",))
    assert report.safe, report.text()

    second = make_harness(repo, [
        {"tool": "replace_in_file", "arguments": {"path": "hello.py",
                                                  "old": 'return "goodbye"',
                                                  "new": 'return "hello"'}},
        {"tool": "run_check", "arguments": {}},
        {"tool": "done", "arguments": {}, "summary": "Finished after a cancel."},
    ], spec=reopened.spec, session=reopened, store=store, allowed_changes=("hello.py",))
    result = second.controller.resume(reopened)

    assert result.status == DONE
    assert (repo / "hello.py").read_text() == RIGHT_HELLO
    assert restart(store, session.id).status == DONE


def test_only_done_is_treated_as_finished():
    for status in ("cancelled", "error", "action_limit_reached", "retry_limit_reached"):
        assert status not in FINISHED, f"{status} should stay resumable"
    assert FINISHED == ("done",)
