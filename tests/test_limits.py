"""Tests for safety/resource limits: actions, retries and output size.

The model is scripted, so every run is the same every time.
"""

import pytest

from config import MAX_ACTIONS, MAX_OUTPUT_CHARS, MAX_RETRIES
from controller import ACTION_LIMIT, DONE, RETRY_LIMIT, TRUNCATION_MARKER, truncate
from model_client import Action
from tests.helpers import hello_repo, make_harness, tool_result_sent, write_repo

LIST = {"tool": "list_files", "arguments": {}}
DONE_REPLY = {"tool": "done", "arguments": {}, "summary": "Finished"}


@pytest.fixture
def repo(tmp_path):
    return hello_repo(tmp_path)


def test_default_limits():
    assert (MAX_ACTIONS, MAX_RETRIES, MAX_OUTPUT_CHARS) == (20, 3, 10000)


# 1. A repeating model reaches the action limit -------------------------------------

def test_repeating_model_reaches_action_limit(repo):
    h = make_harness(repo, [LIST] * 50)   # a model that never says done

    result = h.controller.run("Loop forever")

    assert result.status == ACTION_LIMIT
    assert result.counters.actions == MAX_ACTIONS == 20
    assert result.counters.executed == 20
    assert h.model.calls == 20
    assert h.log[-1] == ("Stopped: action limit reached (20 actions). "
                         "No further actions were executed.")
    assert "actions 20/20" in result.report()


def test_every_reply_counts_as_an_action(repo):
    # malformed + denied + valid replies all use up the action budget
    h = make_harness(repo, [
        "not json",
        Action(tool="nope", arguments={}),
        LIST,
        LIST,
    ], max_actions=3)

    result = h.controller.run("test")

    assert result.status == ACTION_LIMIT
    c = result.counters
    assert (c.actions, c.malformed, c.denied, c.executed) == (3, 1, 1, 1)


# 2. Large command/file output is truncated -------------------------------------------

def test_large_file_output_is_truncated(tmp_path):
    repo = write_repo(tmp_path / "repo", {"big.txt": "x" * 50_000})
    h = make_harness(repo, [
        {"tool": "read_file", "arguments": {"path": "big.txt"}},
        DONE_REPLY,
    ])

    result = h.controller.run("Read big.txt")

    sent = tool_result_sent(h.model, 1)
    assert TRUNCATION_MARKER in sent
    assert "showed the first 10000 of" in sent
    assert sent.count("x") <= MAX_OUTPUT_CHARS
    assert result.counters.truncated == 1


def test_large_command_output_is_truncated(tmp_path):
    noisy_test = (
        "import unittest\n\n"
        "class TestNoisy(unittest.TestCase):\n"
        "    def test_noise(self):\n"
        "        print('y' * 30000)\n"
    )
    repo = write_repo(tmp_path / "repo", {"tests/test_noisy.py": noisy_test})
    h = make_harness(repo, [{"tool": "run_check", "arguments": {}}, DONE_REPLY],
                     max_output_chars=2000)

    result = h.controller.run("Run tests")

    sent = tool_result_sent(h.model, 1)
    assert TRUNCATION_MARKER in sent
    assert "Status:  SUCCEEDED" in sent          # the status line survives the cut
    assert len(result.last_check.stdout) > 30000  # the full output is still kept
    assert result.counters.truncated == 1


def test_small_output_is_not_truncated(repo):
    h = make_harness(repo, [{"tool": "read_file", "arguments": {"path": "hello.py"}},
                            DONE_REPLY])
    result = h.controller.run("Read hello.py")

    assert TRUNCATION_MARKER not in tool_result_sent(h.model, 1)
    assert result.counters.truncated == 0


def test_truncate_function():
    assert truncate("abc", 5) == ("abc", False)
    text, was_cut = truncate("abcdefgh", 5)
    assert was_cut
    assert text.startswith("abcde\n" + TRUNCATION_MARKER)


def test_file_seen_truncated_cannot_be_edited(tmp_path):
    repo = write_repo(tmp_path / "repo", {"big.py": "# line\n" * 5000})
    h = make_harness(repo, [
        {"tool": "read_file", "arguments": {"path": "big.py"}},
        {"tool": "edit_file", "arguments": {"path": "./big.py", "content": "oops"}},
        DONE_REPLY,
    ])

    h.controller.run("Edit big.py")

    assert "only shown truncated" in tool_result_sent(h.model, 2)
    assert (repo / "big.py").read_text() == "# line\n" * 5000
    assert ("edit_file", "./big.py") not in h.tools.calls


# 3. Denied actions are counted -------------------------------------------------------

def test_denied_actions_are_counted(repo):
    h = make_harness(repo, [
        {"tool": "edit_file", "arguments": {"path": "hello.py", "content": "x"}},  # readonly
        LIST,
        {"tool": "read_file", "arguments": {"path": "../outside.txt"}},           # outside root
        LIST,
        {"tool": "run_check", "arguments": {"name": "bash"}},                     # not whitelisted
        LIST,
        Action(tool="shell", arguments={"cmd": "ls"}),                            # unknown tool
        "this is not json",                                                       # malformed
        DONE_REPLY,
    ], mode="readonly")

    result = h.controller.run("test")

    c = result.counters
    assert result.status == DONE
    assert c.denied == 4
    assert c.malformed == 1
    assert c.executed == 3                      # only the three list_files
    assert h.tools.calls == [("list_files", ".")] * 3
    assert "denied 4" in result.report()
    assert "malformed 1" in result.report()


def test_too_many_rejections_in_a_row_stop_the_run(repo):
    h = make_harness(repo, [
        {"tool": "read_file", "arguments": {"path": "/etc/passwd"}},
        "garbage",
        {"tool": "read_file", "arguments": {"path": "../outside.txt"}},
        {"tool": "read_file", "arguments": {"path": "hello.py"}},  # never reached
    ])

    result = h.controller.run("test")

    assert result.status == RETRY_LIMIT
    assert result.counters.actions == MAX_RETRIES == 3
    assert "3 malformed or denied requests in a row" in result.summary
    assert h.tools.calls == []


def test_a_good_action_resets_the_retry_count(repo):
    bad = {"tool": "read_file", "arguments": {"path": "../outside.txt"}}
    h = make_harness(repo, [bad, bad, LIST, bad, bad, LIST, DONE_REPLY])

    result = h.controller.run("test")

    assert result.status == DONE
    assert result.counters.denied == 4


# 4. No actions execute after the limit ---------------------------------------------

def test_no_actions_execute_after_the_limit(repo):
    h = make_harness(repo, [
        LIST, LIST, LIST,
        {"tool": "edit_file", "arguments": {"path": "new.py", "content": "x"}},
        {"tool": "run_check", "arguments": {}},
    ], max_actions=3)

    result = h.controller.run("test")

    assert result.status == ACTION_LIMIT
    assert h.model.calls == 3                  # the model was not asked a 4th time
    assert len(h.model.replies) == 2           # edit_file and run_check never used
    assert h.tools.calls == [("list_files", ".")] * 3
    assert h.executor.calls == []
    assert not (repo / "new.py").exists()
