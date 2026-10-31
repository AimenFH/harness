"""Independent reporting, including real tools in the read-only Docker sandbox."""

import json
from pathlib import Path

import pytest

import application
import main
from config import Config, load_settings
from controller import ACTION_LIMIT, CANCELLED, DONE, ERROR, RETRY_LIMIT, Counters, RunResult
from execution import ALLOWED_COMMANDS, CommandResult, DockerSandbox, ExecutionEnvironment
from model_client import ScriptedModelClient
from session import Session, SessionStore, new_session_id
from task_record import TaskSpec
from tests.helpers import HELLO_PY, commit_all, git, hello_repo
from verification import VerificationResult, Verifier


@pytest.fixture
def repo(tmp_path):
    return commit_all(hello_repo(tmp_path))


def assess(result, repo, status=DONE, **kwargs):
    run = RunResult(status, 'Model says everything is finished', Counters(), 20, **kwargs)
    result.assess_task(run, TaskSpec('Fix greeting', 'hello', str(repo)))
    return result


def test_real_tests_lint_and_build_pass_with_actual_output_and_no_repo_writes(repo):
    result = Verifier(repo, ('unittest', 'ruff', 'compileall')).verify()
    assess(result, repo)
    assert result.passed and result.task_completed
    assert [c.name for c in result.checks] == ['unittest', 'ruff', 'compileall']
    assert all('docker' in c.sandbox for c in result.checks)
    assert result.checks[0].tests_run == 1
    assert 'Ran 1 test' in result.checks_text('tests')
    assert 'All checks passed' in result.checks_text('lint')
    assert 'PASS' in result.checks_text('build')
    assert result.task_status == 'completed'
    assert result.problems == []
    assert result.changed_files == []
    assert not list(repo.rglob('__pycache__'))
    assert not (repo / '.ruff_cache').exists()


def test_real_failing_lint_prevents_success_even_if_tests_pass(repo):
    (repo / 'hello.py').write_text('import os\n' + HELLO_PY)
    result = assess(Verifier(repo, ('unittest', 'ruff')).verify(), repo)
    assert result.checks[0].succeeded
    assert not result.checks[1].succeeded
    assert 'F401' in result.checks_text('lint')
    assert result.verdict() == 'FAIL (1/2 checks passed)'
    assert result.task_status == 'needs attention'
    assert not result.task_completed
    assert any('ruff:' in p for p in result.problems)


def test_real_build_failure_is_reported_even_when_the_test_does_not_import_that_file(repo):
    (repo / 'broken.py').write_text('def invalid(:\n')
    result = assess(Verifier(repo, ('unittest', 'compileall')).verify(), repo)
    assert result.checks[0].succeeded
    assert not result.passed
    assert 'SyntaxError' in result.checks_text('build')
    assert 'compileall:' in result.unfinished_text()


def test_missing_linter_is_unavailable_without_host_fallback(repo, monkeypatch):
    monkeypatch.setitem(ALLOWED_COMMANDS, 'ruff', ['missing-linter-harness-test'])
    result = assess(Verifier(repo, ('unittest', 'ruff')).verify(), repo)
    lint = result.checks[1]
    assert lint.unavailable and not lint.succeeded
    assert lint.exit_code != 0
    assert 'unavailable' in result.checks_text('lint')
    assert 'missing-linter-harness-test' in result.checks_text('lint')
    assert result.task_status == 'needs attention'


@pytest.mark.parametrize('runner,source', [
    ('unittest', 'def test_plain():\n    assert True\n'),
    ('pytest', '# no tests defined\n'),
])
def test_zero_collected_tests_never_pass(repo, runner, source):
    (repo / 'tests/test_hello.py').write_text(source)
    result = Verifier(repo, (runner,)).verify()
    assert not result.passed
    assert result.checks[0].no_tests_ran
    assert 'no tests were run' in result.checks_text()


@pytest.mark.parametrize('runner,source', [
    ('unittest', 'import unittest\nclass T(unittest.TestCase):\n'
     '    @unittest.skip("not ready")\n    def test_skip(self): pass\n'),
    ('pytest', 'import pytest\ndef test_skip(): pytest.skip("not ready")\n'),
    ('pytest', 'import pytest\ndef test_ok(): pass\n'
     '@pytest.mark.xfail(reason="unfinished")\ndef test_todo(): assert False\n'),
])
def test_skipped_or_expected_failure_tests_never_pass(repo, runner, source):
    (repo / 'tests/test_hello.py').write_text(source)
    result = Verifier(repo, (runner,)).verify()
    assert result.checks[0].exit_code == 0
    assert result.checks[0].skipped_tests == 1
    assert not result.checks[0].succeeded
    assert not result.passed
    assert 'skipped/expected-failure tests' in result.checks_text()


def test_timeout_is_visible_and_other_checks_still_run(repo, monkeypatch):
    monkeypatch.setitem(ALLOWED_COMMANDS, 'ruff', [ALLOWED_COMMANDS['unittest'][0], '-c',
                                                 'import time; print("lint started", flush=True); time.sleep(20)'])
    result = Verifier(repo, ('ruff', 'unittest'), timeout_seconds=1).verify()
    assert result.checks[0].timed_out
    assert not result.checks[0].succeeded
    assert result.checks[1].succeeded
    assert 'lint started' in result.checks_text('lint')
    assert 'timed out' in result.checks_text('lint')
    assert not result.passed


@pytest.mark.parametrize('name', ['unittest', 'pytest', 'ruff', 'compileall'])
def test_no_containment_means_no_repository_process_can_start(repo, monkeypatch, name):
    def forbidden(*args, **kwargs):
        pytest.fail('uncontained repository process was started')
    monkeypatch.setattr('execution.subprocess.Popen', forbidden)
    result = ExecutionEnvironment(repo, sandbox=None).run(name)
    assert result.unavailable
    assert not result.succeeded
    assert 'contained execution' in result.stderr


def test_verifier_without_containment_preserves_git_diff_but_all_checks_are_unavailable(repo):
    (repo / 'hello.py').write_text('# change\n')
    result = Verifier(repo, ('unittest', 'ruff', 'compileall'), sandbox=None).verify()
    assert all(c.unavailable for c in result.checks)
    assert not result.passed
    assert '+# change' in result.diff


@pytest.mark.parametrize('status', [ACTION_LIMIT, RETRY_LIMIT, ERROR, CANCELLED])
def test_unfinished_task_is_explicit_even_if_independent_checks_pass(repo, status):
    result = assess(Verifier(repo).verify(), repo, status)
    assert result.passed
    assert not result.task_completed
    assert result.task_status == ('cancelled' if status == CANCELLED else 'unfinished')
    assert status in result.unfinished_text()


def test_uncertain_action_remains_unfinished(repo):
    spec = TaskSpec('Inspect', 'hello', str(repo))
    session = Session(new_session_id(), spec, uncertain={'tool': 'edit_file'})
    result = assess(Verifier(repo).verify(), repo, session=session)
    assert not result.task_completed
    assert 'uncertain outcome' in result.unfinished_text()


def test_out_of_scope_changes_and_unrun_acceptance_are_remaining_problems(repo, tmp_path):
    (repo / 'outside.py').write_text('value = 1\n')
    spec = TaskSpec('Inspect', 'hello', str(repo), allowed_changes=('hello.py',),
                    acceptance_commands=(str(tmp_path / 'external.py'),))
    result = Verifier(repo).verify()
    result.assess_task(RunResult(DONE, 'done', Counters(), 20), spec)
    assert not result.task_completed
    assert 'Out-of-scope change: outside.py' in result.unfinished_text()
    assert 'recorded but not run' in result.unfinished_text()


def test_all_staged_unstaged_untracked_deleted_renamed_and_unusual_paths_are_reported(repo):
    (repo / 'hello.py').write_text('# staged change\n')
    git(repo, 'add', 'hello.py')
    (repo / 'hello.py').write_text('# final change\n')
    git(repo, 'mv', 'tests/test_hello.py', 'tests/test_renamed.py')
    (repo / 'new file ü.py').write_text('number = 1\n')
    result = Verifier(repo, check_names=()).verify()
    assert 'hello.py' in result.changed_paths
    assert 'tests/test_renamed.py' in result.changed_paths
    assert 'tests/test_hello.py' in result.changed_paths
    assert 'new file ü.py' in result.changed_paths
    assert '+# final change' in result.diff
    assert '+# staged change' not in result.diff
    assert '+++ b/new file ü.py' in result.diff
    assert '+number = 1' in result.diff


def test_final_diff_includes_staged_only_changes(repo):
    (repo / 'hello.py').write_text('# staged only\n')
    git(repo, 'add', 'hello.py')
    result = Verifier(repo, check_names=()).verify()
    assert '+# staged only' in result.diff


def test_final_status_and_diff_are_inspected_after_checks(repo, monkeypatch):
    verifier = Verifier(repo)
    def check(name, cancel=None):
        (repo / 'produced.txt').write_text('result of check\n')
        return CommandResult('unittest', ['unit'], 0, '', '', False, 0, tests_run=1)
    monkeypatch.setattr(verifier, '_run_check', check)
    result = verifier.verify()
    assert 'produced.txt' in result.changed_paths
    assert '+result of check' in result.diff


def test_unavailable_git_or_unreadable_untracked_content_prevents_a_pass(repo, tmp_path):
    result = Verifier(repo, git='missing-git-harness-test').verify()
    assert result.checks[0].succeeded
    assert not result.passed
    assert 'git status' in result.unfinished_text()
    secret = tmp_path / 'external-secret'
    secret.write_text('MUST NOT BE READ')
    (repo / 'outside-link').symlink_to(secret)
    result = Verifier(repo).verify()
    assert 'outside-link' in result.changed_paths
    assert not result.passed
    assert 'INCOMPLETE' in result.diff_text()
    assert 'MUST NOT BE READ' not in result.diff_text()


def test_git_textconv_helpers_are_never_run(repo, tmp_path):
    marker = tmp_path / 'textconv-ran'
    (repo / '.gitattributes').write_text('hello.py diff=evil\n')
    git(repo, 'config', 'diff.evil.textconv', f'touch {marker}')
    (repo / 'hello.py').write_text('# changed\n')
    result = Verifier(repo, check_names=()).verify()
    assert '+# changed' in result.diff
    assert not marker.exists()


def test_lint_only_cannot_claim_verification_passed(repo):
    result = Verifier(repo, ('ruff',)).verify()
    assert result.checks[0].succeeded
    assert not result.passed
    assert 'No test check' in result.unfinished_text()


@pytest.mark.parametrize('setting,value', [
    ('lint_checks', '"ruff"'), ('lint_checks', '["bash"]'),
    ('build_checks', '["ruff"]'), ('build_checks', '[true]'),
])
def test_check_configuration_rejects_wrong_types_and_arbitrary_commands(tmp_path, setting, value):
    path = tmp_path / 'settings.toml'
    path.write_text(f'{setting} = {value}\n')
    with pytest.raises(ValueError, match='array of check names'):
        load_settings(path)


def test_configured_check_results_and_remaining_problems_survive_restart(repo, tmp_path, monkeypatch, capsys):
    settings = tmp_path / 'settings.toml'
    settings.write_text('lint_checks = ["ruff"]\nbuild_checks = ["compileall"]\n')
    sessions = tmp_path / 'sessions'
    monkeypatch.setattr(main, 'build_model', lambda config: ScriptedModelClient([
        {'tool': 'done', 'arguments': {}, 'summary': 'All finished!'}]))
    (repo / 'hello.py').write_text('import os\n' + HELLO_PY)
    code = main.main(['--root', str(repo), '--task', 'inspect', '--mode', 'readonly',
                      '--model', 'fake', '--session-dir', str(sessions), '--settings', str(settings)])
    assert code == 1
    saved, = SessionStore(sessions).list_sessions()
    assert saved.result['task_status'] == 'needs attention'
    assert [c['name'] for c in saved.result['checks']] == ['unittest', 'ruff', 'compileall']
    assert saved.spec.checks == ('unittest', 'ruff', 'compileall')
    restored = application.config_from_session(saved, sessions)
    assert tuple(restored.lint_checks) == ('ruff',)
    assert tuple(restored.build_checks) == ('compileall',)
    capsys.readouterr()
    repo.rename(repo.with_name('removed'))
    assert main.main(['show', saved.id, '--session-dir', str(sessions)]) == 0
    output = capsys.readouterr().out
    assert 'F401' in output
    assert 'needs attention' in output
    assert 'remaining problems' in output
    assert '+import os' in output
    assert 'All finished!' in output
    assert 'SUCCESS' not in output


def test_cli_lint_build_options_preserve_fixed_checks(repo):
    config = main.parse_config(['--root', str(repo), '--task', 'inspect', '--mode', 'readonly',
                                '--model', 'fake', '--lint', 'ruff', '--build', 'compileall'])
    assert config.lint_checks == ('ruff',)
    assert config.build_checks == ('compileall',)
