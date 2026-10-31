"""CLI contracts, saved inspection, allowlist validation, and safe continuation."""

import json
import signal
import subprocess
import sys
import time
from pathlib import Path
from types import SimpleNamespace

import pytest

import application
import cli
import main
from cancellation import CancelFile
from model_client import ScriptedModelClient
from navigation import RepositoryNavigator
from projects import HARNESS_DIR, load_projects
from session import Session, SessionStore, new_session_id
from task_record import TaskSpec
from tests.helpers import commit_all, hello_repo, make_harness


@pytest.fixture
def env(tmp_path):
    root = commit_all(hello_repo(tmp_path))
    projects_file = tmp_path / 'projects.toml'
    projects_file.write_text('[projects]\nhello = "repo"\n')
    sessions = tmp_path / 'sessions'
    return SimpleNamespace(root=root, projects_file=projects_file,
                           sessions=sessions, store=SessionStore(sessions))


def invoke(env, *args):
    return main.main(['--projects-file', str(env.projects_file),
                      '--session-dir', str(env.sessions), *args])


def start_args(*extra):
    return ['start', '--project', 'hello', '--task', 'Inspect greeting',
            '--mode', 'readonly', '--model', 'fake', '--offline', '--sandbox', 'docker', *extra]


def saved_task(env, *, replies=None):
    spec = TaskSpec(request='Inspect greeting', project='hello', root=str(env.root),
                    mode='readonly', checks=('unittest',),
                    limits={'max_actions': 20, 'max_retries': 3})
    saved = Session(new_session_id(), spec,
                    run_settings={'model': 'saved-model', 'sandbox': 'docker'})
    model = make_harness(env.root, replies if replies is not None else [
        {'tool': 'read_file', 'arguments': {'path': 'hello.py'}},
    ], mode='readonly', spec=spec, session=saved, store=env.store)
    model.controller.run(spec.request)  # Script exhaustion is a resumable model error.
    return env.store.load(saved.id)


def test_projects_and_start_persist_named_task_and_final_results(env, capsys):
    assert invoke(env, 'projects') == 0
    assert f'hello\t{env.root}' in capsys.readouterr().out
    assert invoke(env, *start_args('--file', 'hello.py')) == 0
    output = capsys.readouterr().out
    saved, = env.store.list_sessions()
    assert saved.spec.project == 'hello'
    assert saved.spec.starting_commit
    assert saved.status == 'done'
    assert saved.result['exit_code'] == 0
    assert saved.result['checks'][0]['exit_code'] == 0
    assert saved.result['verdict'] == 'PASS (1/1 checks passed)'
    assert saved.run_settings['model'] == 'fake'
    assert saved.run_settings['sandbox'] == 'docker'
    assert saved.id in output
    assert 'Result:       SUCCESS' in output
    assert saved.checkpoint.messages


def test_global_options_work_after_command(env):
    assert main.main(['projects', '--projects-file', str(env.projects_file)]) == 0
    assert main.main([*start_args(), '--projects-file', str(env.projects_file),
                      '--session-dir', str(env.sessions)]) == 0


def test_default_session_directory_and_projects_file(env, monkeypatch):
    monkeypatch.setattr(cli, 'DEFAULT_PROJECTS_FILE', env.projects_file)
    monkeypatch.setattr(cli, 'DEFAULT_SESSION_DIR', env.sessions)
    assert main.main(start_args()) == 0
    assert len(env.store.list_sessions()) == 1


def test_show_uses_saved_diff_checks_history_even_after_repo_disappears(env, capsys, monkeypatch):
    replies = [
        {'tool': 'read_file', 'arguments': {'path': 'hello.py'}},
        {'tool': 'edit_file', 'arguments': {'path': 'hello.py',
          'content': 'def greet():\n    return "hello"  # reviewed\n'}},
        {'tool': 'run_check', 'arguments': {}},
        {'tool': 'done', 'arguments': {}, 'summary': 'Added a comment'},
    ]
    monkeypatch.setattr(main, 'build_model', lambda config: ScriptedModelClient(replies))
    args = start_args()
    args[args.index('readonly')] = 'edit'
    assert invoke(env, *args) == 0
    saved, = env.store.list_sessions()
    env.root.rename(env.root.with_name('removed-repo'))
    env.projects_file.unlink()
    capsys.readouterr()
    monkeypatch.setattr(main, 'build_model', lambda config: pytest.fail('inspection contacted model'))
    monkeypatch.setattr(main, 'build_sandbox', lambda config: pytest.fail('inspection started sandbox'))
    assert invoke(env, 'show', saved.id) == 0
    output = capsys.readouterr().out
    for title in ('History', 'Conversation history', 'Results', 'Checks', 'Diff'):
        assert f'=== {title} ===' in output
    assert '+    return "hello"  # reviewed' in output
    assert 'Added a comment' in output
    assert 'read_file' in output
    assert 'PASS' in output
    assert 'SUCCESS' in output
    assert invoke(env, 'status', saved.id) == 0
    assert 'Actions:' in capsys.readouterr().out


def test_old_sessions_without_final_results_are_honestly_reported(env, capsys):
    saved = saved_task(env)
    assert invoke(env, 'show', saved.id) == 0
    output = capsys.readouterr().out
    assert 'UNAVAILABLE (no final result saved)' in output
    assert 'NOT RUN: no final checks saved' in output
    assert '(model error)' in output
    assert 'script' in output.lower()
    assert 'SUCCESS' not in output


def test_list_and_status_are_read_only_and_work_without_allowlist(env, capsys):
    saved = saved_task(env)
    before = env.store.path_for(saved.id).read_bytes()
    env.projects_file.unlink()
    assert invoke(env, 'list') == 0
    assert saved.id in capsys.readouterr().out
    assert invoke(env, 'status', saved.id) == 0
    assert f"{saved.consumed('actions')}/20 used" in capsys.readouterr().out
    assert env.store.path_for(saved.id).read_bytes() == before


def test_empty_list_is_success(env, capsys):
    assert invoke(env, 'list') == 0
    assert 'No sessions' in capsys.readouterr().out


def test_cancel_is_idempotent_and_finished_task_is_noop(env, capsys):
    saved = saved_task(env)
    assert invoke(env, 'cancel', saved.id, '--reason', 'requested from tests') == 0
    assert invoke(env, 'cancel', saved.id, '--reason', 'requested from tests') == 0
    flag = CancelFile(env.sessions, saved.id)
    assert flag.cancelled
    assert flag.reason == 'requested from tests'
    assert 'Cancellation requested' in capsys.readouterr().out
    CancelFile(env.sessions, saved.id).clear()
    saved.status = 'done'
    env.store.save(saved)
    assert invoke(env, 'cancel', saved.id) == 0
    assert 'already finished' in capsys.readouterr().out
    assert not CancelFile(env.sessions, saved.id).path.exists()


def test_resume_dry_run_preserves_checkpoint_and_cancel_flag(env, capsys, monkeypatch):
    saved = saved_task(env)
    before = env.store.path_for(saved.id).read_bytes()
    CancelFile(env.sessions, saved.id).request()
    monkeypatch.setattr(main, 'build_model', lambda config: pytest.fail('dry run contacted model'))
    monkeypatch.setattr(main, 'build_sandbox', lambda config: pytest.fail('dry run started sandbox'))
    assert invoke(env, 'resume', saved.id, '--dry-run') == 0
    assert 'Safe to resume' in capsys.readouterr().out
    assert env.store.path_for(saved.id).read_bytes() == before
    assert CancelFile(env.sessions, saved.id).path.exists()


def test_resume_restores_settings_limits_history_and_clears_cancel(env, monkeypatch):
    saved = saved_task(env)
    CancelFile(env.sessions, saved.id).request()
    captured = []
    model = ScriptedModelClient([{'tool': 'done', 'arguments': {}, 'summary': 'Resumed safely'}])

    def factory(config):
        captured.append(config)
        return model

    monkeypatch.setattr(main, 'build_model', factory)
    assert invoke(env, 'resume', saved.id) == 0
    resumed = env.store.load(saved.id)
    assert captured[0].model == 'saved-model'
    assert captured[0].sandbox == 'docker'
    assert captured[0].max_actions == 20
    assert resumed.consumed('actions') == saved.consumed('actions') + 1
    assert resumed.id == saved.id
    assert resumed.result['exit_code'] == 0
    assert len(resumed.actions) > len(saved.actions)
    assert not CancelFile(env.sessions, saved.id).path.exists()
    assert len(model.received[0]) >= len(saved.checkpoint.messages)


@pytest.mark.parametrize('mismatch', ['commit', 'dirty', 'own-edit', 'uncertain', 'limit', 'retries', 'done', 'git-unavailable'])
def test_resume_pauses_before_model_or_sandbox_on_unsafe_state(env, monkeypatch, capsys, mismatch):
    saved = saved_task(env)
    if mismatch == 'commit':
        saved.checkpoint.commit = '0' * 40
    elif mismatch == 'dirty':
        (env.root / 'unexpected.txt').write_text('external change')
    elif mismatch == 'own-edit':
        saved.checkpoint.file_digests['hello.py'] = 'old-hash'
    elif mismatch == 'uncertain':
        saved.checkpoint.pending = {'step': 2, 'tool': 'edit_file', 'arguments': {'path': 'hello.py'}}
    elif mismatch == 'limit':
        saved.checkpoint.counters['actions'] = 20
    elif mismatch == 'retries':
        saved.checkpoint.repeats = 3
    elif mismatch == 'done':
        saved.status = 'done'
    else:
        monkeypatch.setattr(RepositoryNavigator, 'state_summary', lambda self: ('', '', []))
    env.store.save(saved)
    before = env.store.path_for(saved.id).read_bytes()
    monkeypatch.setattr(main, 'build_model', lambda config: pytest.fail('unsafe resume contacted model'))
    monkeypatch.setattr(main, 'build_sandbox', lambda config: pytest.fail('unsafe resume started sandbox'))
    assert invoke(env, 'resume', saved.id) == 3
    output = capsys.readouterr().out
    assert 'PAUSED' in output
    assert 'Nothing was executed' in output
    assert env.store.path_for(saved.id).read_bytes() == before


def test_resume_rechecks_current_approval_and_never_widens_it(env, capsys, monkeypatch):
    saved = saved_task(env)
    other = env.root.parent / 'other'
    other.mkdir()
    env.projects_file.write_text('[projects]\nother = "other"\n')
    monkeypatch.setattr(main, 'build_model', lambda config: pytest.fail('denied resume contacted model'))
    assert invoke(env, 'resume', saved.id) == 2
    assert 'not in the allowed projects list' in capsys.readouterr().err


def test_live_diff_requires_current_approval_and_runs_no_checks(env, capsys):
    saved = saved_task(env)
    (env.root / 'hello.py').write_text('# current external change\n')
    assert invoke(env, 'show', saved.id, '--live-diff') == 0
    assert '+# current external change' in capsys.readouterr().out
    env.projects_file.unlink()
    assert invoke(env, 'show', saved.id, '--live-diff') == 2


def test_active_session_cannot_be_resumed(env, capsys):
    saved = saved_task(env)
    with env.store.claim(saved.id):
        assert invoke(env, 'resume', saved.id) == 2
    assert 'active in another process' in capsys.readouterr().err


@pytest.mark.parametrize('command', ['status', 'show', 'cancel', 'resume'])
@pytest.mark.parametrize('session_id', ['../../escape', '20261031-120000-aaaaaaaa'])
def test_unknown_or_invalid_ids_have_clear_errors(env, capsys, command, session_id):
    assert invoke(env, command, session_id) == 2
    assert 'error:' in capsys.readouterr().err


@pytest.mark.parametrize('data', ['{', '[]', 'null', '{}', '{"spec": {}}'])
def test_corrupt_sessions_have_errors_without_tracebacks(env, capsys, data):
    session_id = new_session_id()
    env.sessions.mkdir()
    env.store.path_for(session_id).write_text(data)
    assert invoke(env, 'show', session_id) == 2
    error = capsys.readouterr().err
    assert 'error:' in error
    assert 'Traceback' not in error


@pytest.mark.parametrize('bad_name', ['missing', '../repo', '/tmp/repo'])
def test_denied_project_never_constructs_model(env, monkeypatch, capsys, bad_name):
    monkeypatch.setattr(main, 'build_model', lambda config: pytest.fail('denied project contacted model'))
    args = start_args()
    args[args.index('hello')] = bad_name
    with pytest.raises(SystemExit) as error:
        invoke(env, *args)
    assert error.value.code == 2
    assert 'not approved' in capsys.readouterr().err
    assert not env.store.list_sessions()


@pytest.mark.parametrize('content', [
    '', '[projects]', 'projects = []', 'unrelated = true',
    '[projects]\nhello = 12', '[projects]\nhello = false', '[projects]\nhello = ""',
    '[projects]\nhello = {root = "repo"}', '[projects]\n"../bad" = "repo"',
    '[projects]\nhello = "missing"', '[projects]\nhello = "projects.toml"',
    '[projects]\nhello = "repo"\nother = "./repo"', '[projects\nhello = "repo"',
    '[projects]\nhello = "repo"\n[settings]\nsandbox = "none"',
])
def test_project_configuration_validation(env, content, capsys):
    env.projects_file.write_text(content)
    assert invoke(env, 'projects') == 2
    assert 'error:' in capsys.readouterr().err


def test_missing_projects_configuration_fails_closed(env, capsys):
    env.projects_file.unlink()
    assert invoke(env, *start_args()) == 2
    assert 'explicitly approve' in capsys.readouterr().err


@pytest.mark.parametrize('root', [Path('/'), Path.home(), HARNESS_DIR, HARNESS_DIR.parent])
def test_dangerous_approved_roots_are_rejected(env, root):
    env.projects_file.write_text(f'[projects]\nhello = {json.dumps(str(root))}\n')
    with pytest.raises(ValueError):
        load_projects(env.projects_file)


def test_symlinks_are_canonical_and_duplicate_aliases_are_rejected(env):
    link = env.root.with_name('link')
    link.symlink_to(env.root, target_is_directory=True)
    env.projects_file.write_text('[projects]\nhello = "link"\n')
    assert load_projects(env.projects_file)['hello'] == env.root
    env.projects_file.write_text('[projects]\nhello = "link"\nother = "repo"\n')
    with pytest.raises(ValueError, match='duplicate'):
        load_projects(env.projects_file)


@pytest.mark.parametrize('control', ['projects', 'sessions', 'record', 'settings'])
def test_control_files_must_stay_outside_approved_repositories(env, control, capsys):
    if control == 'projects':
        inside = env.root / 'approvals.toml'
        inside.write_text(f'[projects]\nhello = {json.dumps(str(env.root))}\n')
        env.projects_file = inside
        args = start_args()
    elif control == 'sessions':
        env.sessions = env.root / 'sessions'
        args = start_args()
    elif control == 'record':
        args = start_args('--record', str(env.root / 'record.json'))
    else:
        inside = env.root / 'settings.toml'
        inside.write_text('sandbox = "none"')
        args = start_args('--settings', str(inside))
    assert invoke(env, *args) == 2
    assert 'must be outside' in capsys.readouterr().err


@pytest.mark.parametrize('extra', [
    ['--file', '../outside.txt'], ['--file', 'missing'], ['--check', 'bash'],
    ['--allowed-project', '/tmp'], ['--root', '/tmp'], ['--task', '   '],
    ['--model', '   '], ['--mode', 'delete'], ['--settings', '/missing/settings.toml'],
])
def test_invalid_start_options_exit_two(env, extra):
    with pytest.raises(SystemExit) as error:
        invoke(env, *start_args(*extra))
    assert error.value.code == 2


def test_failed_checks_are_saved_and_show_never_reports_success(env, capsys):
    (env.root / 'hello.py').write_text('def greet():\n    return "WRONG"\n')
    assert invoke(env, *start_args()) == 1
    saved, = env.store.list_sessions()
    capsys.readouterr()
    assert invoke(env, 'show', saved.id) == 0
    output = capsys.readouterr().out
    assert 'FAIL (0/1 checks passed)' in output
    assert 'FAILURE' in output
    assert 'AssertionError' in output
    assert 'SUCCESS' not in output


def test_io_errors_and_keyboard_interrupt_have_process_exit_codes(env, monkeypatch, capsys):
    monkeypatch.setattr(main, 'build_sandbox', lambda config: (_ for _ in ()).throw(OSError('storage unavailable')))
    assert invoke(env, *start_args()) == 2
    assert 'storage unavailable' in capsys.readouterr().err
    monkeypatch.setattr(main, 'build_sandbox', lambda config: (_ for _ in ()).throw(KeyboardInterrupt()))
    assert invoke(env, *start_args()) == 130


def test_sigint_handler_is_restored_for_embedded_callers(env):
    before = signal.getsignal(signal.SIGINT)
    assert invoke(env, *start_args()) == 0
    assert signal.getsignal(signal.SIGINT) is before


def test_stage_one_session_flags_remain_compatible(env):
    saved = saved_task(env)
    assert main.main(['--list-sessions', '--session-dir', str(env.sessions)]) == 0
    assert main.main(['--resume', saved.id, '--session-dir', str(env.sessions), '--dry-run']) == 0
    assert main.main(['--cancel', saved.id, '--session-dir', str(env.sessions)]) == 0


def test_help_and_unknown_commands_have_standard_exit_codes():
    for argv, code in [(['--help'], 0), (['start', '--help'], 0), (['unknown'], 2), ([], 2)]:
        with pytest.raises(SystemExit) as error:
            main.main(argv)
        assert error.value.code == code


def test_real_cli_process_start_status_show_and_failures(env):
    def run(*args):
        return subprocess.run([sys.executable, str(HARNESS_DIR / 'main.py'),
                               '--projects-file', str(env.projects_file),
                               '--session-dir', str(env.sessions), *args],
                              capture_output=True, text=True, timeout=15)
    assert run(*start_args()).returncode == 0
    saved, = env.store.list_sessions()
    assert saved.id in run('status', saved.id).stdout
    assert 'PASS (1/1 checks passed)' in run('show', saved.id).stdout
    assert run('resume', saved.id).returncode == 3
    error = run('show', 'invalid')
    assert error.returncode == 2
    assert 'Traceback' not in error.stderr


def test_new_subcommands_cancel_real_process_then_resume_same_task(env, tmp_path):
    """Separate processes exercise addressability during the very first model wait."""
    runner = tmp_path / 'blocked_runner.py'
    runner.write_text(
        'import sys\n'
        f'sys.path.insert(0, {str(HARNESS_DIR)!r})\n'
        'import main\n'
        'from model_client import Blocked, ScriptedModelClient\n'
        'main.build_model = lambda config: ScriptedModelClient([Blocked(timeout=30)])\n'
        f'sys.exit(main.main({["--projects-file", str(env.projects_file), "--session-dir", str(env.sessions), *start_args()]!r}))\n'
    )
    process = subprocess.Popen([sys.executable, str(runner)], stdout=subprocess.PIPE,
                               stderr=subprocess.PIPE, text=True)
    try:
        deadline = time.monotonic() + 10
        saved = None
        while time.monotonic() < deadline:
            sessions = env.store.list_sessions()
            if sessions and sessions[0].checkpoint.messages:
                saved = sessions[0]
                break
            if process.poll() is not None:
                pytest.fail(f'runner exited early: {process.communicate()}')
            time.sleep(0.02)
        assert saved is not None, 'first model wait never became addressable'
        base = [sys.executable, str(HARNESS_DIR / 'main.py'),
                '--projects-file', str(env.projects_file), '--session-dir', str(env.sessions)]
        status = subprocess.run([*base, 'status', saved.id], capture_output=True, text=True, timeout=10)
        assert status.returncode == 0 and 'running' in status.stdout
        cancel = subprocess.run([*base, 'cancel', saved.id, '--reason', 'integration cancel'],
                                capture_output=True, text=True, timeout=10)
        assert cancel.returncode == 0, cancel.stderr
        output, error = process.communicate(timeout=10)
        assert process.returncode == 130, (output, error)
        assert 'integration cancel' in output
        assert env.store.load(saved.id).status == 'cancelled'
        resumed = subprocess.run([*base, 'resume', saved.id, '--offline'],
                                 capture_output=True, text=True, timeout=10)
        assert resumed.returncode == 0, (resumed.stdout, resumed.stderr)
        assert env.store.load(saved.id).status == 'done'
    finally:
        if process.poll() is None:
            process.kill()
            process.communicate(timeout=5)


def test_non_repository_approval_cannot_start_a_task(env, tmp_path, capsys):
    plain = tmp_path / 'plain'
    plain.mkdir()
    env.projects_file.write_text('[projects]\nhello = "plain"\n')
    assert invoke(env, *start_args()) == 2
    assert 'Git repository with at least one commit' in capsys.readouterr().err


def test_approval_is_exact_not_parent_or_child(env, tmp_path, capsys):
    saved = saved_task(env)
    child = env.root / 'nested'
    child.mkdir()
    env.projects_file.write_text('[projects]\nchild = "repo/nested"\n')
    assert invoke(env, 'resume', saved.id, '--dry-run') == 2
    assert 'not in the allowed projects list' in capsys.readouterr().err


def test_different_id_inside_session_file_is_rejected(env, capsys):
    saved = saved_task(env)
    wrong_id = new_session_id()
    env.store.path_for(wrong_id).write_text(env.store.path_for(saved.id).read_text())
    assert invoke(env, 'show', wrong_id) == 2
    assert 'does not match filename' in capsys.readouterr().err


def test_cancel_during_final_verification_is_persisted_as_cancelled(env, monkeypatch, capsys):
    original = application.Verifier.verify

    def cancel_then_verify(verifier, cancel=None):
        saved, = env.store.list_sessions()
        assert saved.status == 'verifying'
        assert invoke(env, 'cancel', saved.id, '--reason', 'cancel final verification') == 0
        return original(verifier, cancel=cancel)

    monkeypatch.setattr(application.Verifier, 'verify', cancel_then_verify)
    assert invoke(env, *start_args()) == 130
    saved, = env.store.list_sessions()
    assert saved.status == 'cancelled'
    assert saved.result['exit_code'] == 130
    assert saved.result['checks'][0]['cancelled']
    assert 'CANCELLED (not passed)' in capsys.readouterr().out


def legacy_start_args(env):
    return ['--root', str(env.root), '--task', 'inspect', '--mode', 'readonly',
            '--model', 'fake', '--offline', '--sandbox', 'docker']


def test_legacy_start_applies_default_approvals(env, tmp_path, monkeypatch):
    monkeypatch.setattr(cli, 'DEFAULT_PROJECTS_FILE', env.projects_file)
    assert main.main(legacy_start_args(env)) == 0
    other = tmp_path / 'other'
    other.mkdir()
    env.projects_file.write_text('[projects]\nother = "other"\n')
    with pytest.raises(SystemExit) as error:
        main.main(legacy_start_args(env))
    assert error.value.code == 2


def test_legacy_start_accepts_explicit_projects_file(env):
    assert main.main([*legacy_start_args(env), '--projects-file', str(env.projects_file)]) == 0


def test_legacy_resume_cannot_bypass_approvals_for_a_modern_task(env, monkeypatch, capsys):
    assert invoke(env, *start_args()) == 0
    saved, = env.store.list_sessions()
    assert saved.run_settings['approval_required']
    monkeypatch.setattr(cli, 'DEFAULT_PROJECTS_FILE', env.projects_file)
    env.projects_file.unlink()
    assert main.main(['--resume', saved.id, '--session-dir', str(env.sessions), '--dry-run']) == 2
    assert 'explicitly approve' in capsys.readouterr().err
