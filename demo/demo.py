"""Helper for the Stage 1 demo. Run it with the same Python as the harness.

    python demo/demo.py prepare      # fresh disposable copy at the pinned commit
    python demo/demo.py acceptance   # run the acceptance check against the copy
    python demo/demo.py regression   # run the existing unit + integration tests
    python demo/demo.py scope        # check that only allowed files changed
    python demo/demo.py verify       # acceptance + regression + scope together
    python demo/demo.py rehearse     # full harness run with a SCRIPTED model (no Ollama)

All commands run without a shell, with a timeout, inside the copy.
"""

import argparse
import os
import shutil
import subprocess
import sys
from pathlib import Path

REPO_URL = "https://github.com/cosmicpython/code.git"
BRANCH = "chapter_05_high_gear_low_gear"
COMMIT = "97267473c8accfd998bb9f756034eac48fa154f6"

# Files the agent is allowed to change for this task.
ALLOWED_FILES = {"service_layer/services.py", "domain/model.py"}

# Existing tests that need no web server or Postgres.
REGRESSION_TESTS = ["tests/unit", "tests/integration"]

DEMO_DIR = Path(__file__).resolve().parent
HARNESS_DIR = DEMO_DIR.parent
ACCEPTANCE = DEMO_DIR / "acceptance" / "acceptance_allocation_quantity.py"
TASK = (DEMO_DIR / "TASK.txt").read_text().strip()

WORKSPACE = Path.home() / "harness-demo"   # outside every git repository
CACHE = WORKSPACE / "cache"                # pristine clone, so resets work offline
TARGET = WORKSPACE / "cosmic"              # the disposable copy the agent works on

sys.path.insert(0, str(HARNESS_DIR))
from execution import command_env  # noqa: E402  (same reduced environment as the harness)


def run(command, cwd, timeout=120):
    """Run a command (list, no shell); return (exit_code, output)."""
    env = command_env()
    try:
        done = subprocess.run(command, cwd=cwd, capture_output=True, text=True,
                              timeout=timeout, env=env)
    except subprocess.TimeoutExpired:
        return None, f"TIMED OUT after {timeout}s"
    return done.returncode, (done.stdout + done.stderr).rstrip()  # keep leading spaces


def git(*args, cwd=TARGET):
    code, output = run(["git", *args], cwd=cwd)
    if code != 0:
        sys.exit(f"git {' '.join(args)} failed:\n{output}")
    return output


def banner(title):
    print(f"\n=== {title} ===")


# ----- commands ---------------------------------------------------------------

def prepare():
    """Make a fresh disposable copy of the target at the pinned commit."""
    WORKSPACE.mkdir(exist_ok=True)
    if not CACHE.exists():
        print(f"Cloning {REPO_URL} (one time) ...")
        git("clone", "--quiet", "--branch", BRANCH, REPO_URL, str(CACHE), cwd=WORKSPACE)

    if TARGET.exists():
        shutil.rmtree(TARGET)
    git("clone", "--quiet", "--no-hardlinks", str(CACHE), str(TARGET), cwd=WORKSPACE)
    git("checkout", "--quiet", "--detach", COMMIT)
    git("remote", "remove", "origin")   # nothing can be pushed from the copy

    head = git("rev-parse", "HEAD")
    assert head == COMMIT, f"expected {COMMIT}, got {head}"
    banner("Target prepared")
    print(f"Repository: {REPO_URL} (branch {BRANCH})")
    print(f"Commit:     {head}")
    print(f"Copy:       {TARGET}")


def acceptance():
    banner("Acceptance check (outside the agent's writable area)")
    print(f"File: {ACCEPTANCE}")
    code, output = run([sys.executable, "-m", "pytest", "-p", "no:cacheprovider",
                        "-q", "--tb=line", "--rootdir", str(ACCEPTANCE.parent),
                        str(ACCEPTANCE)], cwd=TARGET)
    return report(code, output)


def regression():
    banner("Regression tests (existing tests in the target)")
    code, output = run([sys.executable, "-m", "pytest", "-p", "no:cacheprovider",
                        "-q", "--tb=short", *REGRESSION_TESTS], cwd=TARGET)
    return report(code, output)


def scope():
    banner("Scope check (only allowed files may change)")
    status = git("status", "--porcelain", "--untracked-files=all")
    changed = [line[3:] for line in status.splitlines() if line.strip()]
    if not changed:
        print("No files changed.")
        return True
    ok = True
    for path in changed:
        allowed = path in ALLOWED_FILES
        ok = ok and allowed
        print(f"{'allowed    ' if allowed else 'NOT ALLOWED'}  {path}")
    print("PASS" if ok else "FAIL")
    return ok


def verify():
    results = {"acceptance": acceptance(), "regression": regression(), "scope": scope()}
    banner("Demo verdict")
    for name, ok in results.items():
        print(f"{name:<11} {'PASS' if ok else 'FAIL'}")
    return all(results.values())


def rehearse():
    """Run the real harness end to end, with a scripted model doing the reference fix."""
    import main
    from model_client import ScriptedModelClient

    services = "service_layer/services.py"
    script = [
        {"tool": "search", "arguments": {"query": "def allocate"}},
        {"tool": "read_file", "arguments": {"path": services}},
        {"tool": "replace_in_file", "arguments": {
            "path": services,
            "old": "class InvalidSku(Exception):\n    pass\n",
            "new": "class InvalidSku(Exception):\n    pass\n\n\n"
                   "class InvalidQuantity(Exception):\n    pass\n"}},
        {"tool": "replace_in_file", "arguments": {
            "path": services,
            "old": "    line = OrderLine(orderid, sku, qty)\n",
            "new": "    if qty <= 0:\n"
                   "        raise InvalidQuantity(f\"Invalid quantity {qty}\")\n"
                   "    line = OrderLine(orderid, sku, qty)\n"}},
        {"tool": "run_check", "arguments": {}},
        {"tool": "done", "arguments": {}, "summary": "Rejected quantities <= 0 with InvalidQuantity."},
    ]
    main.build_model = lambda config: ScriptedModelClient(script)
    os.environ["PYTEST_ADDOPTS"] = "--ignore=tests/e2e"
    return main.main(["--root", str(TARGET), "--task", TASK, "--mode", "edit",
                      "--model", "scripted", "--check", "pytest"]) == 0


def report(code, output):
    print(output)
    ok = code == 0
    print(f"Exit code: {code}\n{'PASS' if ok else 'FAIL'}")
    return ok


COMMANDS = {"prepare": prepare, "acceptance": acceptance, "regression": regression,
            "scope": scope, "verify": verify, "rehearse": rehearse}

if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("command", choices=COMMANDS)
    result = COMMANDS[parser.parse_args().command]()
    sys.exit(0 if result in (None, True) else 1)
