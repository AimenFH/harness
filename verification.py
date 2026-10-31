"""Independent, contained checks and a durable report of the actual working copy."""

import difflib
import os
from dataclasses import dataclass, field
from typing import Optional

from execution import (BUILD_COMMANDS, DEFAULT_CONTAINMENT, LINT_COMMANDS, TEST_COMMANDS,
                       CommandResult, ExecutionEnvironment, ExecutionError)
from repository_tools import RepositoryError, RepositoryTools


def git_commands(git='git'):
    """Only read-only Git commands; disable external diff and textconv helpers."""
    safe = [git, '-c', 'core.fsmonitor=false', '-c', 'core.quotePath=false', '--no-pager']
    return {
        'status': safe + ['status', '--short', '-z', '--untracked-files=all', '--', '.'],
        'prefix': safe + ['rev-parse', '--show-prefix'],
        'diff': safe + ['diff', 'HEAD', '--relative', '--no-color', '--no-ext-diff', '--no-textconv', '--binary', '--', '.'],
    }


def check_category(name):
    if name in TEST_COMMANDS:
        return 'tests'
    if name in LINT_COMMANDS:
        return 'lint'
    if name in BUILD_COMMANDS:
        return 'build'
    return 'other'


@dataclass
class VerificationResult:
    checks: list = field(default_factory=list)
    changed_files: list = field(default_factory=list)
    diff: str = ''
    git_problem: Optional[str] = None
    changed_paths: list = field(default_factory=list)
    diff_problems: list = field(default_factory=list)
    run_status: str = ''
    task_status: str = 'not assessed'
    unfinished_work: list = field(default_factory=list)

    @property
    def passed(self):
        return (bool(self.checks) and any(c.name in TEST_COMMANDS for c in self.checks)
                and all(c.succeeded for c in self.checks)
                and not self.git_problem and not self.diff_problems)

    @property
    def problems(self):
        problems = []
        if self.git_problem:
            problems.append(self.git_problem)
        problems.extend(self.diff_problems)
        if not any(c.name in TEST_COMMANDS for c in self.checks):
            problems.append('No test check was configured or run.')
        for check in self.checks:
            if not check.succeeded:
                problems.append(f'{check.name}: {check.status()}')
        return list(dict.fromkeys(problems + self.unfinished_work))

    def verdict(self):
        if not self.checks:
            return 'FAIL (no checks were configured)'
        passed = sum(c.succeeded for c in self.checks)
        return f'{"PASS" if self.passed else "FAIL"} ({passed}/{len(self.checks)} checks passed)'

    def assess_task(self, run, spec):
        """Completion and remaining work come from recorded state and evidence."""
        self.run_status = run.status
        self.unfinished_work = []
        if run.status != 'done':
            self.unfinished_work.append(f'Task stopped with status {run.status}: {run.summary}')
        if run.session and run.session.has_uncertain_action:
            self.unfinished_work.append('An interrupted action has an uncertain outcome; review it before continuing.')
        for path in self.changed_paths:
            if not spec.scope().allows(path):
                self.unfinished_work.append(f'Out-of-scope change: {path}')
        if spec.acceptance_commands:
            self.unfinished_work.append('External acceptance checks were recorded but not run by this verifier: '
                                        + ', '.join(spec.acceptance_commands))
        if run.status == 'cancelled':
            self.task_status = 'cancelled'
        elif run.status != 'done':
            self.task_status = 'unfinished'
        elif not self.passed or self.unfinished_work:
            self.task_status = 'needs attention'
        else:
            self.task_status = 'completed'

    @property
    def task_completed(self):
        return self.task_status == 'completed' and self.passed and not self.unfinished_work

    def changed_files_text(self):
        text = '\n'.join(self.changed_files) or '(no changes)'
        return f'UNAVAILABLE: {self.git_problem}\n{text}' if self.git_problem else text

    def checks_text(self, category=None):
        checks = [c for c in self.checks if category is None or check_category(c.name) == category]
        if not checks:
            return 'FAIL: no checks were configured' if category is None else f'NOT CONFIGURED: {category}'
        return '\n\n'.join(_check_text(c) for c in checks)

    def diff_text(self):
        text = self.diff.rstrip()
        if self.git_problem:
            text += f'\nUNAVAILABLE: {self.git_problem}'
        if self.diff_problems:
            text += '\n' + '\n'.join(f'INCOMPLETE: {p}' for p in self.diff_problems)
        return text.strip() or '(no changes)'

    def unfinished_text(self):
        return '\n'.join(f'- {p}' for p in self.problems) or '(none recorded)'


def _check_text(check):
    command = ' '.join([os.path.basename(check.command[0])] + check.command[1:]) if check.command else '?'
    if check.succeeded:
        verdict = 'PASS'
    elif check.cancelled:
        verdict = 'CANCELLED (not passed)'
    elif check.timed_out:
        verdict = f'FAIL (timed out after {check.duration_seconds:.1f}s)'
    elif check.unavailable or check.exit_code is None:
        verdict = 'FAIL (unavailable: the check could not be run)'
    elif check.no_tests_ran:
        verdict = 'FAIL (no tests were run)'
    elif check.skipped_tests:
        verdict = f'NOT PASSED ({check.skipped_tests} skipped/expected-failure tests)'
    else:
        verdict = 'FAIL'
    lines = [f'{check_category(check.name).upper()}: {check.name}', command,
             f'Sandbox: {check.sandbox or "none (execution refused unless contained)"}',
             f'Exit code: {check.exit_code if check.exit_code is not None else "none"}', verdict]
    if check.name in TEST_COMMANDS:
        lines.append(f'Tests executed: {check.tests_run if check.tests_run is not None else "unknown"}; '
                     f'skipped/expected failures: {check.skipped_tests}')
    lines.extend(['--- stdout ---', check.stdout.rstrip() or '(empty)',
                  '--- stderr ---', check.stderr.rstrip() or '(empty)'])
    return '\n'.join(lines)


class Verifier:
    def __init__(self, root, check_names=('unittest',), timeout_seconds=30, git='git',
                 sandbox=DEFAULT_CONTAINMENT):
        self.check_names = list(dict.fromkeys(check_names))
        self.checks = ExecutionEnvironment(root, timeout_seconds, sandbox=sandbox)
        self.git = ExecutionEnvironment(root, timeout_seconds, allowed_commands=git_commands(git))
        self.files = RepositoryTools(root, mode='readonly')

    def verify(self, cancel=None):
        result = VerificationResult()
        # Inspection follows checks so the report reflects their final filesystem state.
        result.checks = [self._run_check(name, cancel=cancel) for name in self.check_names]
        self._inspect_git(result)
        return result

    def _run_check(self, name, cancel=None):
        try:
            return self.checks.run(name, cancel=cancel)
        except ExecutionError as error:
            return CommandResult(name, [name], None, '', str(error), False, 0.0, unavailable=True)

    def _inspect_git(self, result):
        status = self.git.run('status')
        if not status.succeeded:
            result.git_problem = f'git status {status.status()}: {_first_line(status)}'
            return
        entries = status.stdout.split('\0')
        prefix_result = self.git.run('prefix')
        if not prefix_result.succeeded:
            result.git_problem = f'git prefix {prefix_result.status()}: {_first_line(prefix_result)}'
            return
        prefix = prefix_result.stdout.strip()
        untracked = []
        index = 0
        while index < len(entries):
            entry = entries[index]
            index += 1
            if not entry:
                continue
            code, path = entry[:2], entry[3:]
            path = path.removeprefix(prefix)
            display = path
            if 'R' in code or 'C' in code:
                original = entries[index].removeprefix(prefix)
                index += 1
                result.changed_paths.append(original)
                display = f'{original} -> {path}'
            result.changed_files.append(f'{code.strip()} {display}')
            result.changed_paths.append(path)
            if code == '??':
                untracked.append(path)
        diff = self.git.run('diff')
        if not diff.succeeded:
            result.git_problem = f'git diff {diff.status()}: {_first_line(diff)}'
        parts = [diff.stdout.rstrip()] if diff.succeeded else []
        parts.extend(self._new_file_diff(path, result) for path in untracked)
        result.diff = '\n'.join(part for part in parts if part)

    def _new_file_diff(self, path, result):
        header = f'new file: {path}'
        try:
            text = self.files.read_file(path)
        except RepositoryError as error:
            result.diff_problems.append(f'{path}: content unavailable ({error})')
            return f'{header}\n(content not shown: {error})'
        diff = ''.join(difflib.unified_diff([], text.splitlines(keepends=True),
                                          fromfile='/dev/null', tofile=f'b/{path}'))
        return f'{header}\n{diff}'


def _first_line(result):
    text = (result.stderr or result.stdout).strip()
    return text.splitlines()[0] if text else '(no output)'
