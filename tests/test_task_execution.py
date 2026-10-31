"""Tests for task execution: the task record, file scope, the edit-test-fix
loop and the stopping rules.

Everything is deterministic: the model is scripted, repositories are written
into temporary folders, and checks run with the harness's own Python. No
Ollama, no account, no API key.

The centre of this file is test_failing_check_is_fed_back_and_the_fix_passes:
a check fails, the failure output is handed back to the model, the model
corrects the code, and the second check passes. That is the loop the brief
asks for, proved without a live model.
"""

import json

import pytest

from config import Config
from controller import (ACTION_LIMIT, CANCELLED, DENIED, DONE, ERROR, RETRY_LIMIT,
                        STOPPING_RULES)
from controller import FAILED as FAILED_OUTCOME
from model_client import Action
from task_record import (CancelToken, ScopeError, ScopePolicy, TaskRecord, TaskSpec,
                         spec_from_config)
from tests.helpers import PASSING_TEST, hello_repo, make_harness, write_repo

# hello.py returns the wrong word; tests/test_hello.py expects "hello".
WRONG_HELLO = 'def greet():\n    return "goodbye"\n'
RIGHT_HELLO = 'def greet():\n    return "hello"\n'


def broken_repo(tmp_path):
    """A repository whose test fails until hello.py is corrected."""
    return write_repo(tmp_path / "repo",
                      {"hello.py": WRONG_HELLO, "tests/test_hello.py": PASSING_TEST})


# ===== 1. The edit-test-fix loop ==============================================

def test_failing_check_is_fed_back_and_the_fix_passes(tmp_path):
    """A failing check guides the next edit, and the run only finishes after a pass.

    Steps: edit (still wrong) -> check FAILS -> edit (correct) -> check PASSES -> done.
    """
    root = broken_repo(tmp_path)
    h = make_harness(root, [
        # edit_file replaces the whole file, so the model must have read it.
        {"tool": "read_file", "arguments": {"path": "hello.py"}},
        # A first attempt that does not fix the problem.
        {"tool": "edit_file", "arguments": {"path": "hello.py",
                                            "content": 'def greet():\n    return "hi"\n'}},
        {"tool": "run_check", "arguments": {}},
        # Now the model has the failure output and corrects the file.
        {"tool": "edit_file", "arguments": {"path": "hello.py", "content": RIGHT_HELLO}},
        {"tool": "run_check", "arguments": {}},
        {"tool": "done", "arguments": {}, "summary": "Fixed greet() after the first check failed."},
    ])

    result = h.controller.run("Make the test pass")

    assert result.status == DONE
    assert result.succeeded

    # The failing check's output really was handed back to the model.
    feedback = h.model.received[3][-1]["content"]
    assert "FAILED" in feedback
    assert "greet" in feedback or "AssertionError" in feedback

    # Both checks ran, in the right order, with the right outcomes.
    checks = [a for a in result.record.actions if a.tool == "run_check"]
    assert len(checks) == 2
    assert checks[0].check.startswith("FAILED")
    assert checks[1].check.startswith("SUCCEEDED")
    assert (root / "hello.py").read_text() == RIGHT_HELLO


def test_done_is_refused_until_a_check_passes(tmp_path):
    """Requirement 8: an edit needs a *successful* later check before done."""
    root = broken_repo(tmp_path)
    h = make_harness(root, [
        {"tool": "read_file", "arguments": {"path": "hello.py"}},
        {"tool": "edit_file", "arguments": {"path": "hello.py", "content": WRONG_HELLO}},
        {"tool": "done", "arguments": {}, "summary": "claiming success without a check"},
        {"tool": "run_check", "arguments": {}},          # fails
        {"tool": "done", "arguments": {}, "summary": "claiming success after a failed check"},
        {"tool": "edit_file", "arguments": {"path": "hello.py", "content": RIGHT_HELLO}},
        {"tool": "run_check", "arguments": {}},          # passes
        {"tool": "done", "arguments": {}, "summary": "now it really passes"},
    ])

    result = h.controller.run("Make the test pass")

    assert result.status == DONE
    assert result.summary == "now it really passes"
    refusals = [a for a in result.record.actions if a.tool == "done" and a.outcome == DENIED]
    assert len(refusals) == 2
    assert "have not run a check" in refusals[0].detail
    assert "the last check FAILED" in refusals[1].detail


def test_a_passing_check_before_the_edit_does_not_count(tmp_path):
    """Editing again invalidates an earlier pass, so done is refused."""
    root = hello_repo(tmp_path)  # starts out passing
    h = make_harness(root, [
        {"tool": "run_check", "arguments": {}},          # passes
        {"tool": "read_file", "arguments": {"path": "hello.py"}},
        {"tool": "edit_file", "arguments": {"path": "hello.py", "content": WRONG_HELLO}},
        {"tool": "done", "arguments": {}, "summary": "relying on the earlier pass"},
        {"tool": "run_check", "arguments": {}},          # now fails
        {"tool": "edit_file", "arguments": {"path": "hello.py", "content": RIGHT_HELLO}},
        {"tool": "run_check", "arguments": {}},
        {"tool": "done", "arguments": {}, "summary": "checked after the last edit"},
    ])

    result = h.controller.run("Change greet")

    assert result.status == DONE
    assert result.summary == "checked after the last edit"


# ===== 2. The task specification ==============================================

def test_spec_records_everything_the_task_needs(tmp_path):
    config = Config(root=tmp_path / "repo", task="Add expiry to reservations", mode="edit",
                    model="fake", allowed_changes=("service_layer/*.py",),
                    check_command="pytest", final_checks=("pytest",))
    (tmp_path / "repo").mkdir()

    spec = spec_from_config(config, project="cosmic", starting_commit="97267473",
                            acceptance_commands=("/outside/acceptance_expiry.py",))

    assert spec.request == "Add expiry to reservations"
    assert spec.project == "cosmic"
    assert spec.starting_commit == "97267473"
    assert spec.allowed_changes == ("service_layer/*.py",)
    assert spec.checks == ("pytest",)
    assert spec.acceptance_commands == ("/outside/acceptance_expiry.py",)
    assert spec.limits["max_actions"] == config.max_actions
    assert spec.limits["max_retries"] == config.max_retries
    assert spec.limits["command_timeout_seconds"] == config.command_timeout_seconds

    text = spec.summary()
    for expected in ("Add expiry to reservations", "cosmic", "97267473",
                     "service_layer/*.py", "pytest", "acceptance_expiry.py", "max_actions"):
        assert expected in text


def test_record_survives_a_restart(tmp_path):
    """Requirement 1 and 2: the task and its actions can be read back from disk."""
    root = broken_repo(tmp_path)
    spec = TaskSpec(request="Make the test pass", project="demo", root=str(root),
                    starting_commit="deadbeef", allowed_changes=("hello.py",),
                    checks=("unittest",), limits={"max_actions": 20})
    h = make_harness(root, [
        {"tool": "read_file", "arguments": {"path": "hello.py"}},
        {"tool": "edit_file", "arguments": {"path": "hello.py", "content": RIGHT_HELLO}},
        {"tool": "run_check", "arguments": {}},
        {"tool": "done", "arguments": {}, "summary": "fixed"},
    ], spec=spec)

    result = h.controller.run(spec.request)
    saved = result.record.save(tmp_path / "records" / "task.json")

    # A new process would only have the file.
    reopened = TaskRecord.load(saved)
    assert reopened.spec.request == "Make the test pass"
    assert reopened.spec.starting_commit == "deadbeef"
    assert reopened.spec.allowed_changes == ("hello.py",)
    assert reopened.status == DONE
    assert reopened.summary == "fixed"
    assert [a.tool for a in reopened.actions] == ["read_file", "edit_file",
                                                   "run_check", "done"]
    assert reopened.actions[2].check.startswith("SUCCEEDED")


def test_record_json_is_readable_and_shortens_long_arguments(tmp_path):
    root = hello_repo(tmp_path)
    huge = "# filler\n" * 5000
    h = make_harness(root, [
        {"tool": "edit_file", "arguments": {"path": "big.py", "content": huge}},
        {"tool": "run_check", "arguments": {}},
        {"tool": "done", "arguments": {}, "summary": "wrote a big file"},
    ], task="Write a big file")

    result = h.controller.run("Write a big file")
    data = json.loads(result.record.save(tmp_path / "r.json").read_text())

    assert data["spec"]["request"] == "Write a big file"
    assert data["status"] == DONE
    recorded = data["actions"][0]["arguments"]["content"]
    assert len(recorded) < 400          # the whole file is not in the log
    assert recorded.endswith(f"({len(huge)} characters)")


def test_malformed_and_denied_replies_are_recorded_too(tmp_path):
    """What the agent was stopped from doing is recorded, not only what it did."""
    root = hello_repo(tmp_path)
    h = make_harness(root, [
        "this is not JSON at all",
        {"tool": "read_file", "arguments": {"path": "../outside.txt"}},
        {"tool": "done", "arguments": {}, "summary": "gave up"},
    ], mode="readonly")

    result = h.controller.run("Look around")

    outcomes = [(a.tool, a.outcome) for a in result.record.actions]
    assert outcomes[0] == ("(malformed reply)", "malformed")
    assert outcomes[1] == ("read_file", DENIED)
    assert "outside the repository" in result.record.actions[1].detail
    assert "(malformed reply)" in result.record.actions_text()


# ===== 3. File scope, enforced in the harness =================================

@pytest.mark.parametrize("patterns, path, allowed", [
    ((), "anything.py", True),                       # no scope = Stage 1 behaviour
    (("hello.py",), "hello.py", True),
    (("hello.py",), "other.py", False),
    (("hello.py",), "./hello.py", True),             # same file, written differently
    (("domain/*.py",), "domain/model.py", True),
    (("domain/*.py",), "service/app.py", False),
    (("domain/*.py", "service/*.py"), "service/app.py", True),
    (("src/*",), "src/a/b/c.py", True),              # fnmatch "*" spans folders
])
def test_scope_policy_matching(patterns, path, allowed):
    assert ScopePolicy(patterns).allows(path) is allowed


def test_edit_outside_the_allowed_paths_is_denied_before_the_write(tmp_path):
    """Requirements 5 and 6: the harness refuses, and the file is never touched."""
    root = write_repo(tmp_path / "repo", {"hello.py": RIGHT_HELLO, "secret.py": "KEEP = 1\n",
                                          "tests/test_hello.py": PASSING_TEST})
    h = make_harness(root, [
        {"tool": "edit_file", "arguments": {"path": "secret.py", "content": "KEEP = 666\n"}},
        {"tool": "replace_in_file", "arguments": {"path": "secret.py",
                                                  "old": "KEEP = 1", "new": "KEEP = 2"}},
        {"tool": "done", "arguments": {}, "summary": "could not change secret.py"},
    ], allowed_changes=("hello.py", "tests/*"))

    result = h.controller.run("Try to change a file outside the scope")

    assert result.status == DONE
    assert result.changed_files == []
    assert (root / "secret.py").read_text() == "KEEP = 1\n"      # never written
    assert ("edit_file", "secret.py") not in h.tools.calls       # never reached the file tools

    denied = [a for a in result.record.actions if a.outcome == DENIED]
    assert len(denied) == 2
    for action in denied:
        assert "outside the allowed changes" in action.detail
        assert "hello.py, tests/*" in action.detail


def test_edits_inside_the_allowed_paths_still_work(tmp_path):
    root = broken_repo(tmp_path)
    h = make_harness(root, [
        {"tool": "read_file", "arguments": {"path": "hello.py"}},
        {"tool": "edit_file", "arguments": {"path": "hello.py", "content": RIGHT_HELLO}},
        {"tool": "run_check", "arguments": {}},
        {"tool": "done", "arguments": {}, "summary": "fixed the allowed file"},
    ], allowed_changes=("hello.py",))

    result = h.controller.run("Fix hello.py")

    assert result.status == DONE
    assert result.changed_files == ["hello.py"]


def test_scope_does_not_restrict_reading(tmp_path):
    """Scope is about writing. The agent may still read the whole repository."""
    root = write_repo(tmp_path / "repo", {"hello.py": RIGHT_HELLO, "secret.py": "KEEP = 1\n",
                                          "tests/test_hello.py": PASSING_TEST})
    h = make_harness(root, [
        {"tool": "read_file", "arguments": {"path": "secret.py"}},
        {"tool": "done", "arguments": {}, "summary": "read it"},
    ], allowed_changes=("hello.py",))

    result = h.controller.run("Read secret.py")

    assert result.status == DONE
    assert "KEEP = 1" in h.model.received[1][-1]["content"]


def test_scope_is_enforced_on_top_of_the_root_boundary(tmp_path):
    """A path outside the root is still denied, scope or no scope."""
    root = hello_repo(tmp_path)
    h = make_harness(root, [
        {"tool": "edit_file", "arguments": {"path": "../outside.txt", "content": "owned\n"}},
        {"tool": "done", "arguments": {}, "summary": "could not escape"},
    ], allowed_changes=("*",))  # scope allows everything; the root boundary does not

    result = h.controller.run("Try to escape the root")

    assert result.status == DONE
    assert (tmp_path / "outside.txt").read_text() == "secret\n"
    assert "outside the repository" in result.record.actions[0].detail


# ===== 4. Acceptance checks stay outside the writable area ====================

def test_acceptance_inside_the_root_is_refused(tmp_path):
    """Requirement 7: the agent must not be able to edit what judges it."""
    root = tmp_path / "repo"
    root.mkdir()
    inside = root / "acceptance.py"
    inside.write_text("def test_x(): pass\n")

    spec = TaskSpec(request="x", project="p", root=str(root),
                    acceptance_commands=(str(inside),))
    with pytest.raises(ScopeError, match="inside the agent's writable area"):
        spec.validate()


def test_acceptance_outside_the_root_is_accepted(tmp_path):
    root = tmp_path / "repo"
    root.mkdir()
    outside = tmp_path / "acceptance.py"
    outside.write_text("def test_x(): pass\n")

    spec = TaskSpec(request="x", project="p", root=str(root),
                    acceptance_commands=(str(outside),)).validate()
    assert spec.acceptance_commands == (str(outside),)


def test_cli_refuses_an_acceptance_path_inside_the_root(tmp_path):
    from main import parse_config
    root = tmp_path / "repo"
    root.mkdir()
    (root / "acceptance.py").write_text("x = 1\n")

    with pytest.raises(SystemExit) as error:
        parse_config(["--root", str(root), "--task", "t", "--mode", "edit",
                      "--model", "m", "--acceptance", str(root / "acceptance.py")])
    assert error.value.code == 2


def test_cli_stores_allowed_changes_and_acceptance(tmp_path):
    from main import parse_config
    root = tmp_path / "repo"
    root.mkdir()
    outside = tmp_path / "acceptance.py"
    outside.write_text("x = 1\n")

    config = parse_config(["--root", str(root), "--task", "t", "--mode", "edit",
                           "--model", "m", "--allow", "a.py", "--allow", "b/*.py",
                           "--acceptance", str(outside), "--record", str(tmp_path / "r.json")])
    assert config.allowed_changes == ("a.py", "b/*.py")
    assert config.acceptance_commands == (str(outside),)
    assert config.record_path == str(tmp_path / "r.json")


# ===== 5. Stopping conditions =================================================

def test_action_limit_stops_the_run_and_executes_nothing_further(tmp_path):
    root = hello_repo(tmp_path)
    h = make_harness(root, [{"tool": "list_files", "arguments": {}}] * 10, max_actions=3)

    result = h.controller.run("Keep listing files")

    assert result.status == ACTION_LIMIT
    assert result.counters.actions == 3
    assert h.model.calls == 3                      # the model was not asked a 4th time
    assert len(h.tools.calls) == 3
    assert "action limit reached" in result.summary
    assert result.stopping_rule() == STOPPING_RULES[ACTION_LIMIT]


def test_retry_limit_stops_after_repeated_denials(tmp_path):
    root = hello_repo(tmp_path)
    h = make_harness(root, [
        {"tool": "edit_file", "arguments": {"path": "nope.py", "content": "x = 1\n"}},
    ] * 5, allowed_changes=("hello.py",), max_retries=3)

    result = h.controller.run("Try a forbidden edit over and over")

    assert result.status == RETRY_LIMIT
    assert result.counters.denied == 3
    assert result.stopping_rule() == STOPPING_RULES[RETRY_LIMIT]
    assert not (root / "nope.py").exists()


def test_repeating_an_identical_failed_action_is_denied(tmp_path):
    """Bounded retries: an identical failed action cannot succeed, so it is refused.

    A real qwen2.5-coder run repeated the same replace_in_file six times, each
    time a tool error, and burned the whole action limit: tool errors do not
    count towards the retry limit. The repeat is now denied, and denials do
    count, so the run stops instead of spinning.
    """
    root = write_repo(tmp_path / "repo", {"hello.py": RIGHT_HELLO,
                                          "tests/test_hello.py": PASSING_TEST})
    bad_replace = {"tool": "replace_in_file",
                   "arguments": {"path": "hello.py", "old": "not in the file",
                                 "new": "x"}}
    read = {"tool": "read_file", "arguments": {"path": "hello.py"}}
    h = make_harness(root, [
        # The real pattern: the model re-reads the file between attempts, so the
        # denials are never next to each other and the in-a-row counter resets.
        bad_replace, read,                                 # fails: "old" not found
        bad_replace, read,                                 # identical -> denied (1)
        bad_replace, read,                                 # denied (2)
        bad_replace, read,                                 # denied (3) -> retry limit
        {"tool": "done", "arguments": {}, "summary": "never reached"},
    ], max_retries=3)

    result = h.controller.run("Repeat a failing edit")

    assert result.status == RETRY_LIMIT
    assert "not making progress" in result.summary
    assert result.counters.tool_errors == 1       # the edit really only ran once
    assert result.counters.denied == 3
    denied = [a for a in result.record.actions if a.outcome == DENIED]
    assert all("already failed once" in a.detail for a in denied)
    assert (root / "hello.py").read_text() == RIGHT_HELLO   # never changed


def test_a_failed_action_may_be_retried_after_a_file_changes(tmp_path):
    """The repeat rule must not block a retry that can now work."""
    root = write_repo(tmp_path / "repo", {"hello.py": "placeholder\n",
                                          "tests/test_hello.py": PASSING_TEST})
    replace = {"tool": "replace_in_file",
               "arguments": {"path": "hello.py", "old": 'return "goodbye"',
                             "new": 'return "hello"'}}
    h = make_harness(root, [
        replace,                                           # fails: text not there yet
        {"tool": "read_file", "arguments": {"path": "hello.py"}},   # edit_file needs this
        # Writing the file clears the memory of failed actions.
        {"tool": "edit_file", "arguments": {"path": "hello.py", "content": WRONG_HELLO}},
        replace,                                           # now the text exists: works
        {"tool": "run_check", "arguments": {}},
        {"tool": "done", "arguments": {}, "summary": "retried successfully after the edit"},
    ])

    result = h.controller.run("Retry after changing the file")

    assert result.status == DONE
    assert (root / "hello.py").read_text() == RIGHT_HELLO
    outcomes = [a.outcome for a in result.record.actions]
    assert outcomes == [FAILED_OUTCOME, "ok", "ok", "ok", "ok", "ok"]


def test_run_check_may_always_be_repeated(tmp_path):
    """run_check is exempt: the same call legitimately gives a different result."""
    root = broken_repo(tmp_path)
    h = make_harness(root, [
        {"tool": "run_check", "arguments": {}},            # fails
        {"tool": "read_file", "arguments": {"path": "hello.py"}},
        {"tool": "edit_file", "arguments": {"path": "hello.py", "content": RIGHT_HELLO}},
        {"tool": "run_check", "arguments": {}},            # identical call, now passes
        {"tool": "done", "arguments": {}, "summary": "same check, different result"},
    ])

    result = h.controller.run("Run the same check twice")

    assert result.status == DONE
    checks = [a for a in result.record.actions if a.tool == "run_check"]
    assert [c.outcome for c in checks] == ["ok", "ok"]
    assert checks[0].check.startswith("FAILED")
    assert checks[1].check.startswith("SUCCEEDED")


def test_cancellation_stops_before_the_next_action(tmp_path):
    """Requirement 4: a cancelled run executes nothing further."""
    root = hello_repo(tmp_path)
    cancel = CancelToken()
    h = make_harness(root, [
        {"tool": "list_files", "arguments": {}},
        {"tool": "edit_file", "arguments": {"path": "hello.py", "content": WRONG_HELLO}},
    ], cancel=cancel)

    # Cancel as soon as the first action has been carried out.
    original = h.controller.execute

    def execute_then_cancel(action):
        outcome = original(action)
        cancel.cancel("cancelled by the user")
        return outcome

    h.controller.execute = execute_then_cancel
    result = h.controller.run("List files, then get cancelled")

    assert result.status == CANCELLED
    assert h.model.calls == 1                       # never asked for a second action
    assert result.counters.actions == 1
    assert (root / "hello.py").read_text() == RIGHT_HELLO   # the edit never happened
    assert "No further actions were executed" in result.summary
    assert result.stopping_rule() == STOPPING_RULES[CANCELLED]


def test_cancellation_before_the_first_action_runs_nothing(tmp_path):
    root = hello_repo(tmp_path)
    cancel = CancelToken()
    cancel.cancel("stopped before starting")
    h = make_harness(root, [{"tool": "list_files", "arguments": {}}], cancel=cancel)

    result = h.controller.run("Should not start")

    assert result.status == CANCELLED
    assert h.model.calls == 0
    assert h.tools.calls == []


def test_model_error_is_a_fatal_stop(tmp_path):
    root = hello_repo(tmp_path)
    h = make_harness(root, [])   # the scripted model runs out of replies

    result = h.controller.run("Anything")

    assert result.status == ERROR
    assert result.stopping_rule() == STOPPING_RULES[ERROR]


def test_every_status_has_a_stopping_rule():
    for status in (DONE, ACTION_LIMIT, RETRY_LIMIT, CANCELLED, ERROR):
        assert STOPPING_RULES[status]


# ===== 6. Unavailable checks never look like a pass ===========================

def test_unknown_check_is_denied_not_treated_as_a_pass(tmp_path):
    root = broken_repo(tmp_path)
    h = make_harness(root, [
        {"tool": "read_file", "arguments": {"path": "hello.py"}},
        {"tool": "edit_file", "arguments": {"path": "hello.py", "content": RIGHT_HELLO}},
        {"tool": "run_check", "arguments": {"name": "nosuchcheck"}},
        {"tool": "done", "arguments": {}, "summary": "claiming success"},
    ])

    result = h.controller.run("Run a check that does not exist")

    # done is refused: the unknown check never counted as a passing check.
    assert result.status != DONE
    denied = [a for a in result.record.actions if a.outcome == DENIED]
    assert any("Unknown check 'nosuchcheck'" in a.detail for a in denied)
    assert any("have not run a check" in a.detail for a in denied)


def test_a_check_that_cannot_start_is_a_failure(tmp_path):
    """An unavailable check must not satisfy the done gate."""
    root = broken_repo(tmp_path)
    h = make_harness(root, [
        {"tool": "read_file", "arguments": {"path": "hello.py"}},
        {"tool": "edit_file", "arguments": {"path": "hello.py", "content": RIGHT_HELLO}},
        {"tool": "run_check", "arguments": {}},
        {"tool": "done", "arguments": {}, "summary": "claiming success"},
    ])
    # The check command cannot be started at all.
    h.controller.executor.allowed_commands = {"unittest": ["definitely-not-a-program"]}

    result = h.controller.run("Run a check that cannot start")

    assert result.status != DONE
    assert result.last_check is not None
    assert not result.last_check.succeeded
    assert any("the last check" in a.detail
               for a in result.record.actions if a.outcome == DENIED)


def test_no_tests_ran_is_not_a_pass(tmp_path):
    """A check that runs no tests must not satisfy the done gate."""
    # An importable tests package with no test in it: unittest runs 0 tests.
    root = write_repo(tmp_path / "repo", {"hello.py": RIGHT_HELLO, "tests/__init__.py": ""})
    h = make_harness(root, [
        {"tool": "read_file", "arguments": {"path": "hello.py"}},
        {"tool": "edit_file", "arguments": {"path": "hello.py", "content": RIGHT_HELLO}},
        {"tool": "run_check", "arguments": {}},
        {"tool": "done", "arguments": {}, "summary": "claiming success"},
    ])

    result = h.controller.run("Run the tests in a repository with no tests")

    assert result.status != DONE
    assert result.last_check.no_tests_ran
    assert "no tests were run" in result.last_check.status().lower()


# ===== 7. Permission failures are still permission failures ===================

def test_edit_tools_are_denied_in_readonly_mode(tmp_path):
    root = hello_repo(tmp_path)
    h = make_harness(root, [
        {"tool": "edit_file", "arguments": {"path": "hello.py", "content": WRONG_HELLO}},
        {"tool": "done", "arguments": {}, "summary": "readonly, so nothing changed"},
    ], mode="readonly")

    result = h.controller.run("Try to edit in readonly mode")

    assert result.status == DONE
    assert (root / "hello.py").read_text() == RIGHT_HELLO
    assert "not allowed in readonly mode" in result.record.actions[0].detail


def test_the_controller_checks_permissions_even_for_a_handmade_action(tmp_path):
    """The model cannot skip validation by sending an Action object directly."""
    root = hello_repo(tmp_path)
    h = make_harness(root, [
        Action(tool="edit_file", arguments={"path": "secret.py", "content": "x = 1\n"}),
        {"tool": "done", "arguments": {}, "summary": "denied"},
    ], allowed_changes=("hello.py",))

    result = h.controller.run("Bypass the scope check")

    assert not (root / "secret.py").exists()
    assert "outside the allowed changes" in result.record.actions[0].detail


# ===== 8. edit_file may not overwrite a file the model has not read ==========
#
# A real qwen2.5-coder:7b run replaced the whole of service_layer/services.py
# with a seven-line stub ending in the literal comment "# existing code",
# having never read the file. Everything after that failed, because the real
# code was gone. Stage 1 only protected a file read *truncated*.

PLACEHOLDER_STUB = ('class InvalidQuantity(Exception):\n    pass\n\n'
                    '# existing code\n')


def test_overwriting_an_unread_file_is_denied(tmp_path):
    root = write_repo(tmp_path / "repo", {"hello.py": RIGHT_HELLO,
                                          "tests/test_hello.py": PASSING_TEST})
    h = make_harness(root, [
        {"tool": "edit_file", "arguments": {"path": "hello.py",
                                            "content": PLACEHOLDER_STUB}},
        {"tool": "done", "arguments": {}, "summary": "could not overwrite it"},
    ])

    result = h.controller.run("Overwrite a file I have never read")

    assert result.status == DONE
    assert result.changed_files == []
    assert (root / "hello.py").read_text() == RIGHT_HELLO     # untouched
    assert ("edit_file", "hello.py") not in h.tools.calls     # never reached the tools
    assert "you have not read this file" in result.record.actions[0].detail


def test_overwriting_is_allowed_after_reading_the_file(tmp_path):
    root = broken_repo(tmp_path)
    h = make_harness(root, [
        {"tool": "read_file", "arguments": {"path": "hello.py"}},
        {"tool": "edit_file", "arguments": {"path": "hello.py", "content": RIGHT_HELLO}},
        {"tool": "run_check", "arguments": {}},
        {"tool": "done", "arguments": {}, "summary": "read it first, then rewrote it"},
    ])

    result = h.controller.run("Read then overwrite")

    assert result.status == DONE
    assert (root / "hello.py").read_text() == RIGHT_HELLO


def test_creating_a_new_file_needs_no_read(tmp_path):
    """There is nothing to destroy in a file that does not exist yet."""
    root = hello_repo(tmp_path)
    h = make_harness(root, [
        {"tool": "edit_file", "arguments": {"path": "brand_new.py", "content": "x = 1\n"}},
        {"tool": "run_check", "arguments": {}},
        {"tool": "done", "arguments": {}, "summary": "created a new file"},
    ])

    result = h.controller.run("Create a new file")

    assert result.status == DONE
    assert (root / "brand_new.py").read_text() == "x = 1\n"


def test_a_file_the_model_wrote_may_be_rewritten(tmp_path):
    """After writing a whole file the model knows its content, so a second
    edit_file needs no new read."""
    root = hello_repo(tmp_path)
    h = make_harness(root, [
        {"tool": "edit_file", "arguments": {"path": "new.py", "content": "x = 1\n"}},
        {"tool": "edit_file", "arguments": {"path": "new.py", "content": "x = 2\n"}},
        {"tool": "run_check", "arguments": {}},
        {"tool": "done", "arguments": {}, "summary": "wrote it twice"},
    ])

    result = h.controller.run("Write the same new file twice")

    assert result.status == DONE
    assert (root / "new.py").read_text() == "x = 2\n"


def test_replace_in_file_still_works_without_a_read(tmp_path):
    """replace_in_file cannot blind-overwrite: "old" must already match."""
    root = broken_repo(tmp_path)
    h = make_harness(root, [
        {"tool": "replace_in_file", "arguments": {"path": "hello.py",
                                                  "old": 'return "goodbye"',
                                                  "new": 'return "hello"'}},
        {"tool": "run_check", "arguments": {}},
        {"tool": "done", "arguments": {}, "summary": "replaced without reading first"},
    ])

    result = h.controller.run("Replace exact text without reading")

    assert result.status == DONE
    assert (root / "hello.py").read_text() == RIGHT_HELLO


def test_a_partial_replace_does_not_unlock_a_full_overwrite(tmp_path):
    """replace_in_file shows the model only the part it changed, so it still
    has not seen the whole file."""
    root = broken_repo(tmp_path)
    h = make_harness(root, [
        {"tool": "replace_in_file", "arguments": {"path": "hello.py",
                                                  "old": 'return "goodbye"',
                                                  "new": 'return "hello"'}},
        {"tool": "edit_file", "arguments": {"path": "hello.py",
                                            "content": PLACEHOLDER_STUB}},
        {"tool": "run_check", "arguments": {}},
        {"tool": "done", "arguments": {}, "summary": "the overwrite was refused"},
    ])

    result = h.controller.run("Replace, then try to overwrite")

    assert result.status == DONE
    assert (root / "hello.py").read_text() == RIGHT_HELLO     # the stub never landed
    assert "you have not read this file" in result.record.actions[1].detail


def test_a_truncated_read_does_not_unlock_a_full_overwrite(tmp_path):
    """The Stage 1 protection still applies and gives its own message."""
    root = write_repo(tmp_path / "repo", {"big.py": "# filler\n" * 200,
                                          "tests/test_hello.py": PASSING_TEST})
    h = make_harness(root, [
        {"tool": "read_file", "arguments": {"path": "big.py"}},     # cut short
        {"tool": "edit_file", "arguments": {"path": "big.py", "content": "x = 1\n"}},
        {"tool": "done", "arguments": {}, "summary": "the overwrite was refused"},
    ], max_output_chars=50)

    result = h.controller.run("Read a big file, then overwrite it")

    assert result.status == DONE
    assert "only shown truncated" in result.record.actions[1].detail
    assert (root / "big.py").read_text() == "# filler\n" * 200


def test_a_file_selected_by_the_user_counts_as_read(tmp_path):
    """--file sends the whole content, so the model has seen it."""
    root = broken_repo(tmp_path)
    h = make_harness(root, [
        {"tool": "edit_file", "arguments": {"path": "hello.py", "content": RIGHT_HELLO}},
        {"tool": "run_check", "arguments": {}},
        {"tool": "done", "arguments": {}, "summary": "edited a file the user supplied"},
    ])

    result = h.controller.run("Fix hello.py", context_files=("hello.py",))

    assert result.status == DONE
    assert (root / "hello.py").read_text() == RIGHT_HELLO


def test_the_prompt_warns_against_placeholder_rewrites(tmp_path):
    from model_client import build_system_prompt
    prompt = build_system_prompt(mode="edit")
    assert "# existing code" in prompt
    assert "read_file it first" in prompt or "not read in this run" in prompt
