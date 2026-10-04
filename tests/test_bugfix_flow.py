"""The whole Stage 1 bug-fix flow, end to end, without Ollama.

A small two-module target (orders.py uses pricing.py) has a real bug.
An acceptance check lives OUTSIDE the target folder, where the agent
cannot read or change it. The flow is:

    acceptance fails -> harness run (scripted model) -> acceptance passes
                     -> existing regression tests still pass
"""

import shutil
import subprocess
import sys

import pytest

import main
from execution import command_env
from model_client import ScriptedModelClient
from tests.helpers import commit_all, write_repo

pytestmark = pytest.mark.skipif(shutil.which("git") is None, reason="git is not installed")

PRICING = '''def line_total(price_cents, qty):
    return price_cents * qty


def apply_discount(total_cents, percent):
    return total_cents - total_cents * percent // 100
'''

BUGGY_ORDERS = '''from shop import pricing


def order_total(items, discount_percent=0):
    """items: list of (price_cents, qty)."""
    total = sum(pricing.line_total(price, qty) for price, qty in items)
    return pricing.apply_discount(total, 0)
'''

FIXED_ORDERS = BUGGY_ORDERS.replace("apply_discount(total, 0)", "apply_discount(total, discount_percent)")

REGRESSION_TESTS = '''import unittest

from shop import orders, pricing


class TestPricing(unittest.TestCase):
    def test_line_total(self):
        self.assertEqual(pricing.line_total(250, 2), 500)

    def test_apply_discount(self):
        self.assertEqual(pricing.apply_discount(1000, 10), 900)


class TestOrders(unittest.TestCase):
    def test_total_without_discount(self):
        self.assertEqual(orders.order_total([(250, 2), (100, 1)]), 600)
'''

ACCEPTANCE = '''from shop import orders


def test_discount_is_applied_to_order_total():
    assert orders.order_total([(250, 2), (100, 1)], discount_percent=10) == 540
'''


@pytest.fixture
def setup(tmp_path):
    root = commit_all(write_repo(tmp_path / "target", {
        "shop/__init__.py": "",
        "shop/pricing.py": PRICING,
        "shop/orders.py": BUGGY_ORDERS,
        "tests/test_shop.py": REGRESSION_TESTS,
    }))
    acceptance = write_repo(tmp_path / "acceptance", {"acceptance_discount.py": ACCEPTANCE})
    return root, acceptance / "acceptance_discount.py"


def run_acceptance(root, acceptance_file):
    """Run the acceptance check as a separate process inside the target copy."""
    done = subprocess.run(
        [sys.executable, "-m", "pytest", "-q", "-p", "no:cacheprovider", str(acceptance_file)],
        cwd=root, capture_output=True, text=True, timeout=60,
        env=command_env(),  # same reduced environment the harness uses
    )
    return done.returncode, done.stdout + done.stderr


def test_bug_fix_flow(setup, monkeypatch, capsys):
    root, acceptance_file = setup

    # 1. The acceptance check fails on the starting code.
    code, output = run_acceptance(root, acceptance_file)
    assert code != 0 and "600 == 540" in output

    # 2. The harness runs. The scripted model first tries to peek at the
    #    acceptance check (denied), then makes the fix.
    replies = [
        {"tool": "read_file", "arguments": {"path": "../acceptance/acceptance_discount.py"}},
        {"tool": "search", "arguments": {"query": "apply_discount"}},
        {"tool": "read_file", "arguments": {"path": "shop/orders.py"}},
        {"tool": "edit_file", "arguments": {"path": "shop/orders.py", "content": FIXED_ORDERS}},
        {"tool": "run_check", "arguments": {}},
        {"tool": "done", "arguments": {}, "summary": "order_total now passes discount_percent."},
    ]
    monkeypatch.setattr(main, "build_model", lambda config: ScriptedModelClient(replies))
    exit_code = main.main(["--root", str(root), "--task", "Apply the order discount",
                           "--mode", "edit", "--model", "scripted"])
    report = capsys.readouterr().out

    assert "denied: Access denied: '../acceptance/acceptance_discount.py' is outside" in report
    assert "=== Changed Files ===\nM shop/orders.py\n" in report       # only the intended file
    assert "+    return pricing.apply_discount(total, discount_percent)" in report
    assert "Verification: PASS (1/1 checks passed)" in report         # regression tests
    assert exit_code == 0

    # 3. The acceptance check now passes, and it was never modified.
    code, output = run_acceptance(root, acceptance_file)
    assert code == 0, output
    assert acceptance_file.read_text() == ACCEPTANCE
