"""Verification: check the repository's real state after a run.

This runs on its own at the end of every run, no matter what the model
said. It does three things:

1. asks git which files changed (git status --short),
2. asks git what exactly changed (git diff),
3. runs the configured final checks (e.g. unittest).

Nothing is hidden: a failed check, a check that could not start, or a
git command that failed (e.g. not a git repository) is reported as such
and makes the verification fail or shows as UNAVAILABLE.
"""

import os
from dataclasses import dataclass, field
from typing import Optional

from execution import CommandResult, ExecutionEnvironment, ExecutionError
from repository_tools import RepositoryError, RepositoryTools


def git_commands(git="git"):
    """The fixed git commands verification may run (only ever these).

    "-- ." limits git to the --root folder, even if it is inside a bigger repository.
    core.fsmonitor=false and --no-ext-diff stop git from running any helper
    program a repository's settings might name.
    """
    safe = [git, "-c", "core.fsmonitor=false", "--no-pager"]
    return {
        "status": safe + ["status", "--short", "--untracked-files=all", "--", "."],
        "diff": safe + ["diff", "--no-color", "--no-ext-diff", "--", "."],
    }


@dataclass
class VerificationResult:
    """Everything verification found out."""

    checks: list = field(default_factory=list)          # list of CommandResult
    changed_files: list = field(default_factory=list)   # lines like "M orders/pricing.py"
    diff: str = ""
    git_problem: Optional[str] = None   # set if git could not be used

    @property
    def passed(self):
        """True only if at least one check ran and every check passed."""
        return bool(self.checks) and all(check.succeeded for check in self.checks)

    def verdict(self):
        """One line, e.g. 'PASS (1/1 checks passed)'."""
        if not self.checks:
            return "FAIL (no checks were configured)"
        passed = sum(check.succeeded for check in self.checks)
        word = "PASS" if self.passed else "FAIL"
        return f"{word} ({passed}/{len(self.checks)} checks passed)"

    # ----- text for the CLI sections ---------------------------------------

    def changed_files_text(self):
        if self.git_problem:
            return f"UNAVAILABLE: {self.git_problem}"
        return "\n".join(self.changed_files) or "(no changes)"

    def checks_text(self):
        if not self.checks:
            return "FAIL: no checks were configured"
        return "\n\n".join(_check_text(check) for check in self.checks)

    def diff_text(self):
        if self.git_problem:
            return f"UNAVAILABLE: {self.git_problem}"
        return self.diff.rstrip() or "(no changes)"


def _check_text(check: CommandResult):
    """Describe one check: command, exit code, verdict, and output if it failed."""
    program = os.path.basename(check.command[0]) if check.command else "?"
    command = " ".join([program] + check.command[1:])
    exit_code = "none" if check.exit_code is None else check.exit_code

    if check.succeeded:
        verdict = "PASS"
    elif check.timed_out:
        verdict = f"FAIL (timed out after {check.duration_seconds:.0f}s)"
    elif check.no_tests_ran:
        verdict = "FAIL (no tests were run)"
    elif check.exit_code is None:
        verdict = "FAIL (unavailable: the check could not be run)"
    else:
        verdict = "FAIL"

    sandbox = check.sandbox or "none (ran directly on this machine)"
    lines = [command, f"Sandbox: {sandbox}", f"Exit code: {exit_code}", verdict]
    if not check.succeeded:
        output = "\n".join(part.rstrip() for part in (check.stdout, check.stderr) if part.strip())
        lines += ["--- output ---", output or "(no output)"]
    return "\n".join(lines)


class Verifier:
    """Runs the final checks and inspects the repository with git."""

    def __init__(self, root, check_names=("unittest",), timeout_seconds=30, git="git",
                 sandbox=None):
        """
        root:        the target repository
        check_names: names from execution.ALLOWED_COMMANDS to run at the end
        git:         the git program (changeable for tests)
        sandbox:     where the checks run (e.g. a DockerSandbox); None = this machine.
                     git always runs here: it is the harness's tool, not repository code.
        """
        self.check_names = list(check_names)
        self.checks = ExecutionEnvironment(root, timeout_seconds, sandbox=sandbox)
        self.git = ExecutionEnvironment(root, timeout_seconds, allowed_commands=git_commands(git))
        self.files = RepositoryTools(root, mode="readonly")  # to show new files safely

    def verify(self):
        """Inspect the repository and run all checks. Never raises for a failed check."""
        result = VerificationResult()
        self._inspect_git(result)
        result.checks = [self._run_check(name) for name in self.check_names]
        return result

    def _run_check(self, name):
        try:
            return self.checks.run(name)
        except ExecutionError as error:
            # Not a configured check: report it as a failed check, don't skip it.
            return CommandResult(name=name, command=[name], exit_code=None, stdout="",
                                 stderr=str(error), timed_out=False, duration_seconds=0.0)

    def _inspect_git(self, result):
        status = self.git.run("status")
        if not status.succeeded:
            result.git_problem = f"git status {status.status()}: {_first_line(status)}"
            return
        result.changed_files = [line.strip() for line in status.stdout.splitlines() if line.strip()]

        diff = self.git.run("diff")
        if not diff.succeeded:
            result.git_problem = f"git diff {diff.status()}: {_first_line(diff)}"
            return

        # git diff does not include files git does not track yet ("??"), so add them.
        new_files = [line[3:].strip('"') for line in result.changed_files if line.startswith("??")]
        parts = [diff.stdout.rstrip()] + [self._new_file_diff(path) for path in new_files]
        result.diff = "\n".join(part for part in parts if part)

    def _new_file_diff(self, path):
        """Show an untracked file in diff style (every line added)."""
        header = f"new file: {path}\n--- /dev/null\n+++ b/{path}"
        try:
            text = self.files.read_file(path)
        except RepositoryError as error:
            return f"{header}\n(content not shown: {error})"
        return header + "\n" + "\n".join("+" + line for line in text.splitlines())


def _first_line(result: CommandResult):
    text = (result.stderr or result.stdout).strip()
    return text.splitlines()[0] if text else "(no output)"
