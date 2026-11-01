"""Shared helpers for controller tests.

Everything here is deterministic: the model is scripted, the repository
is written fresh into a temporary folder, and the "spy" tools record
exactly which tool calls really happened.
"""

import subprocess
from types import SimpleNamespace

from config import Config
from controller import Controller
from execution import ExecutionEnvironment
from model_client import ScriptedModelClient
from repository_tools import RepositoryTools

HELLO_PY = 'def greet():\n    return "hello"\n'

PASSING_TEST = '''import unittest

from hello import greet


class TestHello(unittest.TestCase):
    def test_greet(self):
        self.assertEqual(greet(), "hello")
'''

FAILING_TEST = PASSING_TEST.replace('"hello")', '"goodbye")')


def write_repo(root, files):
    """Create `files` ({relative path: text}) under `root` and return root."""
    for path, text in files.items():
        target = root / path
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(text)
    return root


def hello_repo(tmp_path, test_code=PASSING_TEST):
    """A tiny repository with hello.py and one unittest test."""
    (tmp_path / "outside.txt").write_text("secret\n")
    return write_repo(tmp_path / "repo", {"hello.py": HELLO_PY,
                                          "tests/test_hello.py": test_code})


class SpyTools(RepositoryTools):
    """RepositoryTools that records every call that actually reaches it."""

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.calls = []

    def list_files(self, path="."):
        self.calls.append(("list_files", path))
        return super().list_files(path)

    def read_file(self, path):
        self.calls.append(("read_file", path))
        return super().read_file(path)

    def search(self, query, path="."):
        self.calls.append(("search", query))
        return super().search(query, path)

    def edit_file(self, path, content):
        self.calls.append(("edit_file", path))
        return super().edit_file(path, content)


class SpyExecutor(ExecutionEnvironment):
    """ExecutionEnvironment that records which commands were run."""

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.calls = []

    def run(self, name, cancel=None):
        self.calls.append(name)
        return super().run(name, cancel=cancel)


def make_harness(root, replies, mode="edit", spec=None, cancel=None,
                 session=None, store=None, **settings):
    """Build a Controller wired to a scripted model and spy tools.

    `settings` may override any Config field, e.g. max_actions, max_retries,
    max_output_chars or allowed_changes. `spec` and `cancel` are passed to the
    Controller: a TaskSpec saved with the run, and a CancelToken to stop it.
    Returns a namespace with: controller, model, tools, executor, log.

    `context_map` defaults to False here, unlike a real run: building the
    repository map and the file suggestions reads the whole repository through
    `tools`, which would show up in SpyTools.calls and hide the tool calls a
    test is actually about. Tests for context management pass context_map=True
    explicitly (see tests/test_context.py).
    """
    settings.setdefault("context_map", False)
    task = settings.pop("task", "test task")
    config = Config(root=root, task=task, mode=mode, model="fake", **settings)
    model = ScriptedModelClient(replies)
    tools = SpyTools(root, mode)
    executor = SpyExecutor(root, timeout_seconds=30)
    log = []
    controller = Controller(config, model, tools, executor, log=log.append,
                            spec=spec, cancel=cancel, session=session, store=store)
    return SimpleNamespace(controller=controller, model=model, tools=tools,
                           executor=executor, log=log)


def tool_result_sent(model, call_index):
    """The last message the model saw on call number `call_index` (0-based)."""
    return model.received[call_index][-1]["content"]


GIT_IDENTITY = ["-c", "user.name=Test", "-c", "user.email=test@example.com",
                "-c", "commit.gpgsign=false"]


def git(root, *args):
    """Run a git command in `root` for test setup (fails loudly on error)."""
    subprocess.run(["git", *GIT_IDENTITY, *args], cwd=root, check=True, capture_output=True)


def commit_all(root):
    """Turn `root` into a git repository with everything committed."""
    git(root, "init", "-q")
    git(root, "add", "-A")
    git(root, "commit", "-q", "-m", "initial")
    return root
