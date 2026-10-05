"""Tests for verification.py, plus end-to-end runs through main().

Each test builds a real, small git repository in tmp_path. The model is
scripted, so no Ollama is needed.
"""

import shutil

import pytest

import main
from model_client import ScriptedModelClient
from tests.helpers import FAILING_TEST, HELLO_PY, PASSING_TEST, commit_all, git, hello_repo
from verification import Verifier

pytestmark = pytest.mark.skipif(shutil.which("git") is None, reason="git is not installed")

@pytest.fixture
def repo(tmp_path):
    return commit_all(hello_repo(tmp_path))


# ----- Verifier on its own ------------------------------------------------------

def test_clean_repository_passes(repo):
    result = Verifier(repo).verify()

    assert result.passed
    assert result.changed_files == []
    assert result.diff == ""
    assert result.verdict() == "PASS (1/1 checks passed)"
    assert "Exit code: 0\nPASS" in result.checks_text()
    assert "unittest discover -s tests -v" in result.checks_text()


def test_changed_and_new_files_are_reported_with_diff(repo):
    (repo / "hello.py").write_text(HELLO_PY.replace('"hello"', '"hello!"'))
    (repo / "tests" / "test_student.py").write_text("# a new test file\n")

    result = Verifier(repo).verify()

    assert result.changed_files == ["M hello.py", "?? tests/test_student.py"]
    assert '-    return "hello"' in result.diff
    assert '+    return "hello!"' in result.diff
    assert "+++ b/tests/test_student.py\n+# a new test file" in result.diff


def test_failed_check_is_shown_with_its_output(repo):
    (repo / "tests" / "test_hello.py").write_text(FAILING_TEST)

    result = Verifier(repo).verify()
    text = result.checks_text()

    assert not result.passed
    assert result.verdict() == "FAIL (0/1 checks passed)"
    assert "Exit code: 1\nFAIL" in text
    assert "test_greet" in text and "goodbye" in text   # the real failure output


def test_running_checks_does_not_create_changed_files(repo):
    Verifier(repo).verify()
    result = Verifier(repo).verify()   # second run sees what the first one left behind

    assert result.changed_files == []  # no __pycache__ or .pytest_cache


def test_not_a_git_repository_is_unavailable_but_checks_still_run(tmp_path):
    repo = hello_repo(tmp_path)   # no git init

    result = Verifier(repo).verify()

    assert result.changed_files_text().startswith("UNAVAILABLE: git status FAILED")
    assert result.diff_text().startswith("UNAVAILABLE")
    assert result.checks[0].succeeded


def test_missing_git_program_is_unavailable(repo):
    result = Verifier(repo, git="git-program-that-does-not-exist").verify()

    assert "UNAVAILABLE" in result.changed_files_text()
    assert "could not be started" in result.changed_files_text()


def test_unknown_check_is_reported_as_failed_not_skipped(repo):
    result = Verifier(repo, check_names=["unittest", "mystery"]).verify()

    assert not result.passed
    assert result.verdict() == "FAIL (1/2 checks passed)"
    assert "FAIL (unavailable" in result.checks_text()
    assert "'mystery' is not allowed" in result.checks_text()


def test_no_checks_means_fail(repo):
    result = Verifier(repo, check_names=[]).verify()

    assert not result.passed
    assert "no checks were configured" in result.checks_text()


def test_only_changes_inside_root_are_reported(tmp_path):
    outer = tmp_path / "outer"
    outer.mkdir()
    hello_repo(outer)                     # creates outer/repo/...
    (outer / "other.txt").write_text("v1\n")
    commit_all(outer)
    (outer / "other.txt").write_text("v2\n")           # outside --root
    (outer / "repo" / "hello.py").write_text("# changed\n")

    result = Verifier(outer / "repo", check_names=[]).verify()

    assert result.changed_files == ["M hello.py"]
    assert "other.txt" not in result.diff


# ----- End to end: verification is independent of the model's claim -------------

def run_main(monkeypatch, capsys, repo, replies):
    monkeypatch.setattr(main, "build_model", lambda config: ScriptedModelClient(replies))
    exit_code = main.main(["--root", str(repo), "--task", "Fix it",
                           "--mode", "edit", "--model", "fake"])
    return exit_code, capsys.readouterr().out


def test_model_claims_success_but_verification_fails(tmp_path, monkeypatch, capsys):
    # The model changes nothing (so it may call done without a check), but
    # the repository's test fails. Verification must not believe the claim.
    repo = commit_all(hello_repo(tmp_path, test_code=FAILING_TEST))
    exit_code, output = run_main(monkeypatch, capsys, repo, [
        {"tool": "read_file", "arguments": {"path": "hello.py"}},
        {"tool": "done", "arguments": {}, "summary": "All tests pass!"},   # not true
    ])

    assert "Model says:   All tests pass!" in output
    assert "Verification: FAIL (0/1 checks passed)" in output
    assert "Result:       FAILURE" in output
    assert exit_code == 1


def test_honest_fix_passes_end_to_end(tmp_path, monkeypatch, capsys):
    repo = commit_all(hello_repo(tmp_path, test_code=FAILING_TEST))
    fixed = HELLO_PY.replace('"hello"', '"goodbye"')
    exit_code, output = run_main(monkeypatch, capsys, repo, [
        {"tool": "read_file", "arguments": {"path": "hello.py"}},
        {"tool": "edit_file", "arguments": {"path": "hello.py", "content": fixed}},
        {"tool": "run_check", "arguments": {}},
        {"tool": "done", "arguments": {}, "summary": "greet() now returns goodbye."},
    ])

    for title in ["Task", "Progress", "Changed Files", "Checks", "Diff", "Summary"]:
        assert f"=== {title} ===" in output
    assert "=== Changed Files ===\nM hello.py\n" in output
    assert "Exit code: 0\nPASS" in output
    assert '+    return "goodbye"' in output
    assert "Result:       SUCCESS" in output
    assert exit_code == 0


def test_model_that_never_finishes_is_a_failure_even_if_checks_pass(repo, monkeypatch, capsys):
    exit_code, output = run_main(monkeypatch, capsys, repo,
                                 [{"tool": "list_files", "arguments": {}}] * 25)

    assert "Verification: PASS" in output           # the repo itself is fine...
    assert "(the model did not finish)" in output
    assert "Result:       FAILURE" in output        # ...but the task was not completed
    assert exit_code == 1


def test_check_that_ran_zero_tests_fails(repo):
    (repo / "tests" / "test_hello.py").write_text("def test_plain():\n    assert True\n")

    result = Verifier(repo).verify()

    assert not result.passed
    assert "FAIL (no tests were run)" in result.checks_text()


def test_git_helper_programs_are_not_run(repo, tmp_path):
    marker = tmp_path / "fsmonitor_ran.txt"
    git(repo, "config", "core.fsmonitor", f"touch {marker}; false")

    Verifier(repo, check_names=[]).verify()

    assert not marker.exists()
