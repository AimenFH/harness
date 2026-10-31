"""Prepare/check the Stage 2 expiry demo. Never implements or launches the feature."""

import argparse
from dataclasses import asdict
from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path
import shlex
import subprocess
import sys

HARNESS = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(HARNESS))
from execution import DockerSandbox, ExecutionEnvironment, ExecutionError, PYTHON, command_env
from session import SessionError
from task_record import TaskSpec

COMMIT = '97267473c8accfd998bb9f756034eac48fa154f6'
REPOSITORY = 'https://github.com/cosmicpython/code.git'
BRANCH = 'chapter_05_high_gear_low_gear'
PROTECTED = Path(__file__).resolve().parent / 'stage2'
DEFAULT_WORKSPACE = Path.home() / 'harness-demo' / 'stage2'
CACHE = Path.home() / 'harness-demo' / 'cache'
ALLOWED = ('domain/model.py', 'service_layer/services.py', 'adapters/orm.py')
EXPECTED = {'acceptance': 23, 'regression': 24}


def git(root, *args):
    command = ['git', '-c', 'core.fsmonitor=false', '-c', 'core.hooksPath=/dev/null',
               '-c', 'core.quotePath=false', *args]
    result = subprocess.run(command, cwd=root, env=command_env(), capture_output=True,
                            text=True, timeout=120, check=False)
    if result.returncode:
        raise ValueError(f'Git command failed ({result.returncode}): {result.stderr.strip()}')
    return result.stdout.rstrip('\n')


def checked_target(workspace):
    target = workspace / 'cosmic'
    if target.is_symlink():
        raise ValueError('the disposable target must not be a symlink')
    if not target.is_dir() or git(target, 'rev-parse', '--show-toplevel') != str(target.resolve()):
        raise ValueError(f'prepare a separate disposable Git copy first: {target}')
    if git(target, 'rev-parse', 'HEAD') != COMMIT:
        raise ValueError(f'target HEAD must remain at {COMMIT}; inspect changes before proceeding')
    if git(target, 'remote'):
        raise ValueError('the disposable copy must have no Git remotes')
    if PROTECTED.is_relative_to(target.resolve()) or workspace == target:
        raise ValueError('protected checks must remain outside the agent writable root')
    return target


def check_integrity():
    manifest = json.loads((PROTECTED / 'CHECKS.sha256.json').read_text())
    for relative, expected in manifest.items():
        path = PROTECTED / relative
        if not path.resolve().is_relative_to(PROTECTED.resolve()):
            raise ValueError('invalid protected manifest path')
        if hashlib.sha256(path.read_bytes()).hexdigest() != expected:
            raise ValueError(f'protected check changed: {relative}; baseline evidence is no longer valid')


def write_spec(workspace, target):
    task = (PROTECTED / 'TASK.txt').read_text().strip()
    commands = tuple(shlex.join([sys.executable, str(Path(__file__).resolve()), name,
                                 '--workspace', str(workspace)])
                     for name in ('acceptance', 'regression', 'scope'))
    spec = TaskSpec(request=task, project='cosmic-expiry', root=str(target),
                    starting_commit=COMMIT, mode='edit', allowed_changes=ALLOWED,
                    checks=('pytest',), acceptance_commands=commands,
                    limits={'max_actions': 60, 'max_retries': 3, 'max_output_chars': 10000,
                            'command_timeout_seconds': 120}).validate()
    (workspace / 'task.json').write_text(json.dumps(spec.to_dict(), indent=2) + '\n')
    # Separate, explicit approval; do not change the user's global project list.
    (workspace / 'approved-projects.toml').write_text('[projects]\ncosmic-expiry = '
                                                     + json.dumps(str(target)) + '\n')
    (workspace / 'demo-settings.toml').write_text('max_actions = 60\ncommand_timeout_seconds = 120\n')
    return spec


def prepare(workspace):
    workspace.mkdir(parents=True, exist_ok=True)
    target = workspace / 'cosmic'
    if not CACHE.exists():
        CACHE.parent.mkdir(parents=True, exist_ok=True)
        git(CACHE.parent, 'clone', '--quiet', '--branch', BRANCH, REPOSITORY, str(CACHE))
    if not target.exists():
        git(workspace, 'clone', '--quiet', '--no-hardlinks', '--no-checkout', str(CACHE), str(target))
        git(target, 'checkout', '--quiet', '--detach', COMMIT)
        git(target, 'remote', 'remove', 'origin')
    target = checked_target(workspace)
    if git(target, 'status', '--porcelain', '--untracked-files=all'):
        raise ValueError('prepare refuses to overwrite a changed target; choose a new --workspace')
    check_integrity()
    source = json.loads((PROTECTED / 'regression/SOURCE.json').read_text())
    for path, expected in source['sha256'].items():
        original = subprocess.check_output(['git', '-C', str(CACHE), 'show', f'{COMMIT}:{path}'])
        if hashlib.sha256(original).hexdigest() != expected:
            raise ValueError(f'regression snapshot does not match starting commit: {path}')
    write_spec(workspace, target)
    print(f'Prepared {target}\nHEAD: {COMMIT}\nNo target code was changed.')
    print(f'Task: {workspace / "task.json"}\nApprovals: {workspace / "approved-projects.toml"}')
    return True


def run_suite(workspace, suite):
    target = checked_target(workspace)
    check_integrity()
    sandbox = DockerSandbox(mounts=[(PROTECTED, '/protected')])
    sandbox.check()
    command = [PYTHON, '-I', '/protected/run_checks.py', suite]
    result = ExecutionEnvironment(target, timeout_seconds=120, sandbox=sandbox,
                                  allowed_commands={f'protected-{suite}': command}).run(f'protected-{suite}')
    reports = [line.partition('=')[2] for line in result.stdout.splitlines()
               if line.startswith('HARNESS_STAGE2_REPORT=')]
    report = json.loads(reports[-1]) if len(reports) == 1 else {}
    counts_ok = (report.get('tests') == EXPECTED[suite] and report.get('errors') == 0
                 and report.get('skipped') == 0 and report.get('exit_code') == result.exit_code)
    passed = (result.succeeded and counts_ok and report.get('passed') == EXPECTED[suite]
              and report.get('failed') == 0)
    workspace.mkdir(parents=True, exist_ok=True)
    (workspace / f'{suite}.log').write_text(result.stdout + '\n' + result.stderr)
    payload = {'command': command, 'result': asdict(result), 'report': report,
               'counts_valid': counts_ok, 'passed': passed}
    (workspace / f'{suite}.json').write_text(json.dumps(payload, indent=2) + '\n')
    # Keep the useful pytest output, but omit the duplicate machine report line.
    print('\n'.join(line for line in result.stdout.splitlines()
                    if not line.startswith('HARNESS_STAGE2_REPORT=')))
    if result.stderr:
        print(result.stderr)
    print(f'{suite}: {"PASS" if passed else "FAIL"}; exit={result.exit_code}; '
          f'counts={report.get("tests", "unavailable")}; sandbox={result.sandbox}')
    return payload


def changed_files(target):
    raw = git(target, 'status', '--porcelain=v1', '-z', '--untracked-files=all', '--no-renames')
    return [entry[3:] for entry in raw.split('\0') if entry]


def scope(workspace, require_multi_module=True):
    target = checked_target(workspace)
    paths = changed_files(target)
    allowed = all(path in ALLOWED for path in paths)
    modules_ok = not require_multi_module or len(set(paths) & set(ALLOWED)) >= 2
    ok = allowed and modules_ok
    print(f'Scope: {"PASS" if ok else "FAIL"}; changed={paths or "none"}')
    if not modules_ok:
        print('The feature must change at least two allowed interacting modules.')
    return ok


def expected_missing_feature(payload):
    report = payload['report']
    failed = [c for c in report.get('cases', []) if c['result'] == 'failed']
    passed = [c for c in report.get('cases', []) if c['result'] == 'passed']
    return (payload['counts_valid'] and payload['result']['exit_code'] == 1
            and not payload['result']['timed_out'] and not payload['result']['cancelled']
            and report.get('failed') == 22 and report.get('passed') == 1
            and len(failed) == 22 and len(passed) == 1
            and passed[0]['name'] == 'test_legacy_allocation_control_still_works_on_starting_code'
            and all('MISSING FEATURE:' in c['detail'] for c in failed))


def baseline(workspace):
    target = checked_target(workspace)
    if changed_files(target):
        raise ValueError('baseline requires the untouched starting commit; target has changes')
    acceptance = run_suite(workspace, 'acceptance')
    regression = run_suite(workspace, 'regression')
    unchanged = not changed_files(target)
    expected_failure = expected_missing_feature(acceptance)
    ok = expected_failure and regression['passed'] and unchanged
    record = {
        'recorded_at_utc': datetime.now(timezone.utc).isoformat(),
        'repository': REPOSITORY, 'starting_commit': COMMIT, 'target': str(target),
        'feature_implemented': False, 'target_unchanged': unchanged,
        'feature_acceptance_passed': acceptance['passed'],
        'expected_missing_feature_failure': expected_failure,
        'feature_counts': {k: acceptance['report'].get(k) for k in EXPECTED_COUNT_KEYS},
        'regression_passed': regression['passed'],
        'regression_counts': {k: regression['report'].get(k) for k in EXPECTED_COUNT_KEYS},
        'protected_sha256': json.loads((PROTECTED / 'CHECKS.sha256.json').read_text()),
        'demo_helper_sha256': hashlib.sha256(Path(__file__).read_bytes()).hexdigest(),
        'task_specification': str(workspace / 'task.json'),
        'sandbox': 'harness-sandbox (Docker; no network; read-only /work and /protected)',
        'baseline_proof_passed': ok,
    }
    (workspace / 'baseline.json').write_text(json.dumps(record, indent=2) + '\n')
    print(f'Baseline proof: {"PASS" if ok else "FAIL"}. '
          f'Feature acceptance: FAIL (expected={expected_failure}); '
          f'legacy regression: {"PASS" if regression["passed"] else "FAIL"}; '
          f'target unchanged={unchanged}.')
    return ok


EXPECTED_COUNT_KEYS = ('tests', 'passed', 'failed', 'errors', 'skipped')


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('command', choices=('prepare', 'baseline', 'acceptance', 'regression', 'scope', 'verify'))
    parser.add_argument('--workspace', type=Path, default=DEFAULT_WORKSPACE,
                        help='External workspace; target is its cosmic/ child. Existing changes are never reset.')
    args = parser.parse_args(argv)
    workspace = args.workspace.expanduser().resolve()
    if workspace.is_relative_to(HARNESS) or HARNESS.is_relative_to(workspace):
        parser.error('use a separate workspace outside the harness repository')
    try:
        if args.command == 'prepare':
            ok = prepare(workspace)
        elif args.command == 'baseline':
            ok = baseline(workspace)
        elif args.command in ('acceptance', 'regression'):
            ok = run_suite(workspace, args.command)['passed']
        elif args.command == 'scope':
            ok = scope(workspace)
        else:
            acceptance = run_suite(workspace, 'acceptance')['passed']
            regression = run_suite(workspace, 'regression')['passed']
            ok = scope(workspace, require_multi_module=True) and acceptance and regression
        return 0 if ok else 1
    except (ValueError, OSError, subprocess.SubprocessError, SessionError, ExecutionError) as error:
        print(f'error: {error}', file=sys.stderr)
        return 2


if __name__ == '__main__':
    sys.exit(main())
