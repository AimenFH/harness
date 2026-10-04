"""Tests for the Controller loop, using the scripted (fake) model.

No Ollama is needed. Action-limit and truncation tests are in test_limits.py.
"""

import pytest

from controller import DONE, ERROR, Controller
from model_client import Action, ModelError
from tests.helpers import FAILING_TEST, hello_repo, make_harness, tool_result_sent


@pytest.fixture
def repo(tmp_path):
    return hello_repo(tmp_path)


# 1. The correct tool gets called --------------------------------------------

def test_correct_tools_are_called(repo):
    h = make_harness(repo, [
        {"tool": "read_file", "arguments": {"path": "hello.py"}},
        {"tool": "run_check", "arguments": {}},
        {"tool": "done", "arguments": {}, "summary": "Finished"},
    ])

    result = h.controller.run("Check hello.py")

    assert h.tools.calls == [("read_file", "hello.py")]
    assert h.executor.calls == ["unittest"]
    assert [line for line in h.log if line.startswith("[")] == [
        "[1] read_file hello.py",
        "[2] run_check",
        "[3] done",
    ]
    assert result.status == DONE
    assert result.succeeded


# 2. The tool result goes back into the model's history -----------------------

def test_tool_result_is_added_to_history(repo):
    h = make_harness(repo, [
        {"tool": "read_file", "arguments": {"path": "hello.py"}},
        {"tool": "done", "arguments": {}, "summary": "Finished"},
    ])
    h.controller.run("Read hello.py")

    first_call, second_call = h.model.received
    assert [m["role"] for m in first_call] == ["system", "user"]
    assert second_call[-2]["role"] == "assistant"
    assert '"read_file"' in second_call[-2]["content"]   # the model's own request
    assert second_call[-1]["role"] == "user"
    assert 'return "hello"' in second_call[-1]["content"]  # the file content


# 3. Unknown tools return an error and execute nothing ------------------------

def test_unknown_tool_from_model_reply(repo):
    h = make_harness(repo, [
        {"tool": "delete_everything", "arguments": {"path": "."}},
        {"tool": "done", "arguments": {}, "summary": "stop"},
    ])
    h.controller.run("Do something bad")

    assert "Unknown tool 'delete_everything'" in tool_result_sent(h.model, 1)
    assert h.tools.calls == [] and h.executor.calls == []


def test_unknown_tool_is_refused_by_controller_itself(repo):
    # An Action object skips parse_action, so only the controller can stop it.
    h = make_harness(repo, [
        Action(tool="delete_everything", arguments={"path": "."}),
        {"tool": "done", "arguments": {}, "summary": "stop"},
    ])
    result = h.controller.run("Do something bad")

    assert tool_result_sent(h.model, 1).startswith("DENIED: Unknown tool")
    assert result.counters.denied == 1
    assert h.tools.calls == [] and h.executor.calls == []


# 4. Invalid arguments are rejected -----------------------------------------------

@pytest.mark.parametrize("arguments, tool, message", [
    ({}, "read_file", "needs argument 'path'"),
    ({"path": 5}, "read_file", "must be a string"),
    ({"path": "hello.py", "mode": "w"}, "read_file", "does not take argument"),
    ({"name": "rm -rf /"}, "run_check", "Unknown check"),
])
def test_invalid_arguments_are_rejected(repo, arguments, tool, message):
    # Once as a model reply (caught by parsing), once as an Action (caught by the controller).
    for reply in [{"tool": tool, "arguments": arguments}, Action(tool=tool, arguments=arguments)]:
        h = make_harness(repo, [reply, {"tool": "done", "arguments": {}}])
        h.controller.run("test")

        assert message in tool_result_sent(h.model, 1)
        assert h.tools.calls == [] and h.executor.calls == []


# 5. A failed command stays visible -------------------------------------------------

def test_failed_check_stays_visible(tmp_path):
    repo = hello_repo(tmp_path, test_code=FAILING_TEST)
    h = make_harness(repo, [
        {"tool": "run_check", "arguments": {}},
        {"tool": "done", "arguments": {}, "summary": "All good!"},  # the model is wrong
    ])

    result = h.controller.run("Run the tests")

    assert "    FAILED (exit code 1)" in h.log
    sent = tool_result_sent(h.model, 1)
    assert "Status:  FAILED (exit code 1)" in sent
    assert "test_greet" in sent and "goodbye" in sent
    assert result.status == DONE           # the model did say done...
    assert not result.succeeded            # ...but the run is not a success
    assert "Last check: unittest FAILED (exit code 1)" in result.report()


# 8. done stops the loop ------------------------------------------------------------

def test_done_stops_the_loop(repo):
    h = make_harness(repo, [
        {"tool": "done", "arguments": {}, "summary": "Finished"},
        {"tool": "read_file", "arguments": {"path": "hello.py"}},  # must never run
    ])

    result = h.controller.run("Nothing to do")

    assert result.status == DONE
    assert result.summary == "Finished"
    assert result.counters.actions == 1
    assert h.model.calls == 1
    assert len(h.model.replies) == 1      # the read_file reply was never used
    assert h.tools.calls == []


# Extra cases ------------------------------------------------------------------------

def test_readonly_mode_denies_edit(repo):
    h = make_harness(repo, [
        {"tool": "edit_file", "arguments": {"path": "hello.py", "content": "broken"}},
        {"tool": "done", "arguments": {}},
    ], mode="readonly")
    result = h.controller.run("Break it")

    assert (repo / "hello.py").read_text().startswith("def greet")
    assert "Permission denied" in tool_result_sent(h.model, 1)
    assert result.changed_files == [] and h.tools.calls == []


def test_path_outside_repository_is_denied(repo):
    h = make_harness(repo, [
        {"tool": "read_file", "arguments": {"path": "../outside.txt"}},
        {"tool": "done", "arguments": {}},
    ])
    result = h.controller.run("Read the secret")

    sent = tool_result_sent(h.model, 1)
    assert sent.startswith("DENIED: Access denied")
    assert "secret" not in sent
    assert result.counters.denied == 1


def test_fatal_model_error_stops_the_loop(repo):
    class BrokenModel:
        def request_action(self, messages):
            raise ModelError("Cannot connect to Ollama")

    h = make_harness(repo, [])
    controller = Controller(h.controller.config, BrokenModel(), h.tools, h.executor,
                            log=h.log.append)
    result = controller.run("anything")

    assert result.status == ERROR
    assert "Cannot connect to Ollama" in result.summary


def test_malformed_reply_is_reported_and_loop_continues(repo):
    h = make_harness(repo, [
        "I think I should read hello.py.",
        {"tool": "done", "arguments": {}, "summary": "ok"},
    ])
    result = h.controller.run("test")

    assert h.log[0].startswith("[1] malformed reply")
    assert "invalid action" in tool_result_sent(h.model, 1)
    assert result.status == DONE
    assert result.counters.malformed == 1


def test_edit_then_check_fixes_bug(tmp_path):
    repo = hello_repo(tmp_path, test_code=FAILING_TEST)
    fixed = 'def greet():\n    return "goodbye"\n'
    h = make_harness(repo, [
        {"tool": "read_file", "arguments": {"path": "hello.py"}},
        {"tool": "edit_file", "arguments": {"path": "hello.py", "content": fixed}},
        {"tool": "run_check", "arguments": {}},
        {"tool": "done", "arguments": {}, "summary": "Changed the greeting."},
    ])
    result = h.controller.run("Make the test pass")

    assert (repo / "hello.py").read_text() == fixed
    assert result.changed_files == ["hello.py"]
    assert result.succeeded


def test_git_folder_cannot_be_edited(repo):
    (repo / ".git").mkdir()
    h = make_harness(repo, [
        {"tool": "edit_file", "arguments": {"path": ".git/config", "content": "evil"}},
        {"tool": "done", "arguments": {}},
    ])
    result = h.controller.run("Change git settings")

    assert "protected .git" in tool_result_sent(h.model, 1)
    assert result.counters.denied == 1
    assert h.tools.calls == []
    assert not (repo / ".git" / "config").exists()


def test_replace_in_file_then_check_fixes_bug(tmp_path):
    repo = hello_repo(tmp_path, test_code=FAILING_TEST)
    h = make_harness(repo, [
        {"tool": "read_file", "arguments": {"path": "hello.py"}},
        {"tool": "replace_in_file", "arguments": {
            "path": "hello.py", "old": 'return "hello"', "new": 'return "goodbye"'}},
        {"tool": "run_check", "arguments": {}},
        {"tool": "done", "arguments": {}, "summary": "Changed the greeting."},
    ])
    result = h.controller.run("Make the test pass")

    assert (repo / "hello.py").read_text() == 'def greet():\n    return "goodbye"\n'
    assert tool_result_sent(h.model, 2).startswith("Result of replace_in_file:")
    assert result.changed_files == ["hello.py"]
    assert result.succeeded


def test_readonly_mode_denies_replace_in_file(repo):
    h = make_harness(repo, [
        {"tool": "replace_in_file", "arguments": {
            "path": "hello.py", "old": "hello", "new": "broken"}},
        {"tool": "done", "arguments": {}},
    ], mode="readonly")
    result = h.controller.run("Break it")

    assert "Permission denied: replace_in_file" in tool_result_sent(h.model, 1)
    assert (repo / "hello.py").read_text().startswith("def greet")
    assert result.counters.denied == 1 and h.tools.calls == []


def test_replace_text_not_found_is_a_tool_error(repo):
    h = make_harness(repo, [
        {"tool": "replace_in_file", "arguments": {
            "path": "hello.py", "old": "no such text", "new": "x"}},
        {"tool": "done", "arguments": {}},
    ])
    result = h.controller.run("Change it")

    assert "was not found" in tool_result_sent(h.model, 1)
    assert result.counters.tool_errors == 1
    assert result.changed_files == []
