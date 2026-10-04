"""Tests for ExecutionEnvironment.

Each test writes a tiny repository with a tests/ folder into pytest's
tmp_path and runs the real whitelisted commands against it.
"""

import time

import pytest

from execution import ExecutionEnvironment, ExecutionError

PASSING_TEST = """
import os
import unittest

class TestSample(unittest.TestCase):
    def test_ok(self):
        print("cwd=" + os.getcwd())
        self.assertEqual(1 + 1, 2)
"""

FAILING_TEST = """
import unittest

class TestSample(unittest.TestCase):
    def test_wrong_total(self):
        self.assertEqual(1 + 1, 3)
"""

SLOW_TEST = """
import time
import unittest

class TestSample(unittest.TestCase):
    def test_slow(self):
        time.sleep(30)
"""


def make_repo(tmp_path, test_code):
    """Create a repository with one test file and return its root."""
    root = tmp_path / "repo"
    (root / "tests").mkdir(parents=True)
    (root / "tests" / "test_sample.py").write_text(test_code)
    return root


def test_successful_command(tmp_path):
    root = make_repo(tmp_path, PASSING_TEST)
    result = ExecutionEnvironment(root).run("unittest")

    assert result.succeeded
    assert result.exit_code == 0
    assert result.timed_out is False
    assert "OK" in result.stderr  # unittest writes its summary to stderr
    assert f"cwd={root.resolve()}" in result.stdout  # ran inside the repo
    assert result.status() == "SUCCEEDED (exit code 0)"


def test_nonzero_exit_code_is_a_failure(tmp_path):
    root = make_repo(tmp_path, FAILING_TEST)
    result = ExecutionEnvironment(root).run("unittest")

    assert not result.succeeded
    assert result.exit_code == 1
    assert "FAILED" in result.stderr
    assert "test_wrong_total" in result.stderr
    assert result.status() == "FAILED (exit code 1)"
    assert "FAILED (exit code 1)" in result.report()


def test_timeout(tmp_path):
    root = make_repo(tmp_path, SLOW_TEST)
    result = ExecutionEnvironment(root, timeout_seconds=1).run("unittest")

    assert not result.succeeded
    assert result.timed_out is True
    assert result.exit_code is None
    assert result.duration_seconds < 30
    assert result.status().startswith("TIMED OUT")


def test_disallowed_command_is_rejected(tmp_path):
    root = make_repo(tmp_path, PASSING_TEST)
    env = ExecutionEnvironment(root)

    for request in ["rm -rf /", "bash", "python3 -c 'print(1)'", "unittest; ls"]:
        with pytest.raises(ExecutionError, match="not allowed"):
            env.run(request)


def test_pytest_command(tmp_path):
    root = make_repo(tmp_path, PASSING_TEST)
    result = ExecutionEnvironment(root).run("pytest")

    assert result.succeeded
    assert "1 passed" in result.stdout


def test_missing_program_is_reported_as_failure(tmp_path):
    root = make_repo(tmp_path, PASSING_TEST)
    env = ExecutionEnvironment(
        root, allowed_commands={"broken": ["this-program-does-not-exist-123"]}
    )
    result = env.run("broken")

    assert not result.succeeded
    assert result.exit_code is None
    assert "Could not start command" in result.stderr


def test_timeout_also_kills_child_processes(tmp_path):
    marker = tmp_path / "written_after_timeout.txt"
    root = make_repo(tmp_path, f"""
import subprocess, sys, time, unittest

class TestSample(unittest.TestCase):
    def test_slow(self):
        # a child process that would write a file after the timeout
        subprocess.Popen([sys.executable, "-c",
                          "import time; time.sleep(1.5); open(r'{marker}', 'w').write('x')"])
        time.sleep(30)
""")
    result = ExecutionEnvironment(root, timeout_seconds=0.5).run("unittest")

    assert result.timed_out
    time.sleep(2.5)
    assert not marker.exists(), "a child process kept running after the timeout"


def test_background_processes_are_cleaned_up_after_success(tmp_path):
    marker = tmp_path / "written_later.txt"
    root = make_repo(tmp_path, f"""
import subprocess, sys, unittest

class TestSample(unittest.TestCase):
    def test_starts_background_job(self):
        subprocess.Popen([sys.executable, "-c",
                          "import time; time.sleep(1); open(r'{marker}', 'w').write('x')"],
                         stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
""")
    result = ExecutionEnvironment(root).run("unittest")

    assert result.succeeded
    time.sleep(2)
    assert not marker.exists(), "a background process kept running after the command"


def test_secrets_in_environment_are_not_passed(tmp_path, monkeypatch):
    monkeypatch.setenv("MY_API_KEY", "sk-secret")
    monkeypatch.setenv("PYTEST_ADDOPTS", "-q")
    root = make_repo(tmp_path, """
import os, unittest

class TestSample(unittest.TestCase):
    def test_env(self):
        print("KEY=" + str(os.environ.get("MY_API_KEY")))
        print("ADDOPTS=" + str(os.environ.get("PYTEST_ADDOPTS")))
""")
    result = ExecutionEnvironment(root).run("unittest")

    assert "KEY=None" in result.stdout
    assert "ADDOPTS=-q" in result.stdout


def test_zero_tests_is_not_a_success(tmp_path):
    # pytest-style test functions: unittest finds nothing to run
    root = make_repo(tmp_path, "def test_plain():\n    assert False\n")
    result = ExecutionEnvironment(root).run("unittest")

    assert result.no_tests_ran
    assert not result.succeeded
    assert result.status().startswith("FAILED (no tests were run")
