"""Policy tests for the demo preparer, without executing target code on the host."""

import hashlib
import json

import pytest

from demo import stage2
from task_record import TaskSpec


def expected_baseline():
    return {'counts_valid': True,
            'result': {'exit_code': 1, 'timed_out': False, 'cancelled': False},
            'report': {'tests': 23, 'passed': 1, 'failed': 22, 'errors': 0, 'skipped': 0,
                       'cases': [
                           {'name': 'test_legacy_allocation_control_still_works_on_starting_code',
                            'result': 'passed', 'detail': ''},
                           *[{'name': f'feature-{i}', 'result': 'failed',
                              'detail': 'AssertionError: MISSING FEATURE: expiry API absent'}
                             for i in range(22)]]}}


def test_baseline_proof_requires_feature_absence_not_just_a_nonzero_exit():
    payload = expected_baseline()
    assert stage2.expected_missing_feature(payload)
    payload['report']['cases'][1]['detail'] = 'ImportError: cannot import sqlalchemy'
    assert not stage2.expected_missing_feature(payload)


@pytest.mark.parametrize('problem', ['timeout', 'cancelled', 'setup-error', 'missing-cases',
                                      'wrong-control', 'bad-counts'])
def test_baseline_proof_rejects_infrastructure_failure_and_missing_evidence(problem):
    payload = expected_baseline()
    if problem == 'timeout':
        payload['result']['timed_out'] = True
    elif problem == 'cancelled':
        payload['result']['cancelled'] = True
    elif problem == 'setup-error':
        payload['result']['exit_code'] = 2
    elif problem == 'missing-cases':
        payload['report']['cases'] = []
    elif problem == 'wrong-control':
        payload['report']['cases'][0]['name'] = 'some_other_test'
    else:
        payload['counts_valid'] = False
    assert not stage2.expected_missing_feature(payload)


def test_protected_regression_snapshots_and_manifests_are_intact():
    stage2.check_integrity()
    source = json.loads((stage2.PROTECTED / 'regression/SOURCE.json').read_text())
    assert source['commit'] == stage2.COMMIT
    assert source['expected_tests'] == 24
    assert len(source['sha256']) == 7
    for name, digest in source['sha256'].items():
        assert hashlib.sha256((stage2.PROTECTED / 'regression' / name).read_bytes()).hexdigest() == digest


def test_saved_task_pins_commit_scope_and_external_acceptance_commands(tmp_path):
    target = tmp_path / 'cosmic'
    target.mkdir()
    spec = stage2.write_spec(tmp_path, target)
    reopened = TaskSpec.from_dict(json.loads((tmp_path / 'task.json').read_text())).validate()
    assert reopened.to_dict() == spec.to_dict()
    assert reopened.starting_commit == stage2.COMMIT
    assert reopened.allowed_changes == stage2.ALLOWED
    assert not reopened.scope().allows('tests/unit/test_services.py')
    assert not reopened.scope().allows('entrypoints/flask_app.py')
    assert reopened.checks == ('pytest',)
    assert len(reopened.acceptance_commands) == 3
    assert 'release_expired' in reopened.request
    assert 'now' in reopened.request
    assert '--workspace' in reopened.acceptance_commands[0]
    assert stage2.PROTECTED.resolve().is_relative_to(target.resolve()) is False


def test_prepare_refuses_to_erase_a_changed_target(tmp_path, monkeypatch):
    target = tmp_path / 'cosmic'
    target.mkdir()
    marker = target / 'work.py'
    marker.write_text('existing work\n')
    monkeypatch.setattr(stage2, 'CACHE', tmp_path)
    monkeypatch.setattr(stage2, 'checked_target', lambda workspace: target)
    monkeypatch.setattr(stage2, 'git', lambda root, *args: ' M work.py')
    with pytest.raises(ValueError, match='refuses to overwrite'):
        stage2.prepare(tmp_path)
    assert marker.read_text() == 'existing work\n'


def test_host_harness_collection_does_not_collect_target_checks(tmp_path):
    # A pytest subprocess only collects: none of the target modules is imported.
    import subprocess
    import sys
    result = subprocess.run([sys.executable, '-m', 'pytest', '--collect-only', '-q',
                             str(stage2.PROTECTED)], cwd=stage2.HARNESS,
                            capture_output=True, text=True, timeout=15)
    assert result.returncode == 5
    assert 'no tests collected' in result.stdout
    assert 'ImportError' not in result.stdout + result.stderr
