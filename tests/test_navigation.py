"""Tests for repository navigation (navigation.py) and its use by the controller.

Everything is deterministic: repositories are written into temporary
folders, git repositories are created by the test, and the model is scripted.
Python files are only parsed, never imported, so a file that would do
something when run cannot do anything here.
"""

import os
import subprocess

import pytest

from controller import DONE, Controller
from model_client import NAVIGATION_TOOLS, Action, build_system_prompt
from navigation import (
    MAX_RESULTS,
    RepositoryNavigator,
    limit,
    module_name,
    parse_porcelain,
)
from repository_tools import AccessDeniedError, RepositoryError, RepositoryTools
from tests.helpers import commit_all, git, make_harness, tool_result_sent, write_repo

# A small layered application: service -> domain <- adapters, plus a src-layout
# package, relative imports, a test and an entry point.
APP = {
    "domain/__init__.py": "",
    "domain/model.py": (
        "from dataclasses import dataclass\n"
        "from typing import TYPE_CHECKING\n"
        "\n"
        "if TYPE_CHECKING:\n"
        "    class Hint:\n"
        "        pass\n"
        "\n"
        "\n"
        "class OutOfStock(Exception):\n"
        "    pass\n"
        "\n"
        "\n"
        "@dataclass\n"
        "class OrderLine:\n"
        "    sku: str\n"
        "    qty: int\n"
        "\n"
        "\n"
        "class Batch:\n"
        "    def __init__(self, ref, qty):\n"
        "        self.ref = ref\n"
        "\n"
        "    def allocate(self, line: OrderLine) -> None:\n"
        "        def helper():\n"
        "            return 1\n"
        "        return helper()\n"
        "\n"
        "    @property\n"
        "    def available(self):\n"
        "        return 0\n"
        "\n"
        "    class Audit:\n"
        "        def record(self):\n"
        "            pass\n"
        "\n"
        "\n"
        "def allocate(line, batches):\n"
        "    return batches[0].allocate(line)\n"
        "\n"
        "\n"
        "async def refresh(batch):\n"
        "    return batch\n"
    ),
    "service_layer/__init__.py": "",
    "service_layer/services.py": (
        "import os\n"
        "import json as js\n"
        "from domain import model\n"
        "from domain.model import OrderLine, OutOfStock\n"
        "from adapters.repository import AbstractRepository\n"
        "from . import helpers\n"
        "from .helpers import clean\n"
        "\n"
        "\n"
        "def allocate(orderid, sku, qty, repo, session):\n"
        "    def check():\n"
        "        import requests\n"
        "    return model.allocate(OrderLine(sku, qty), repo.list())\n"
    ),
    "service_layer/helpers.py": "def clean(text):\n    return text.strip()\n",
    "adapters/__init__.py": "",
    "adapters/repository.py": (
        "import abc\n"
        "from domain import model\n"
        "from ..outside import thing\n"
        "\n"
        "\n"
        "class AbstractRepository(abc.ABC):\n"
        "    @abc.abstractmethod\n"
        "    def add(self, batch: model.Batch):\n"
        "        raise NotImplementedError\n"
    ),
    "src/shop/__init__.py": "",
    "src/shop/cart.py": "from shop import prices\nfrom .prices import total\n",
    "src/shop/prices.py": "def total(items):\n    return sum(items)\n",
    "tests/test_services.py": "from service_layer import services\n",
    "entrypoints/flask_app.py": "from service_layer.services import allocate\n",
    "README.md": "# Shop\n",
}

BROKEN = "def broken(:\n    pass\n"


@pytest.fixture
def repo(tmp_path):
    (tmp_path / "outside.py").write_text("def secret_outside():\n    pass\n")
    return write_repo(tmp_path / "repo", APP)


@pytest.fixture
def nav(repo):
    return RepositoryNavigator(RepositoryTools(repo, "readonly"))


# ===== 1. Finding modules and files ==========================================

def test_find_files_by_part_of_the_path(nav):
    assert nav.find_files("services") == [
        "service_layer/services.py  (module service_layer.services)",
        "tests/test_services.py  (module tests.test_services)",
    ]


def test_find_files_by_module_name(nav):
    assert nav.find_files("service_layer.services") == [
        "service_layer/services.py  (module service_layer.services)"]


def test_find_files_by_glob(nav):
    assert nav.find_files("test_*.py") == ["tests/test_services.py  (module tests.test_services)"]
    assert nav.find_files("*.md") == ["README.md"]


def test_find_files_is_case_insensitive_and_reports_no_match(nav):
    assert nav.find_files("READ") == ["README.md"]
    assert nav.find_files("nothing-like-this") == ["No files match 'nothing-like-this'"]


def test_find_files_inside_a_folder(nav):
    assert nav.find_files("model", path="domain") == ["domain/model.py  (module domain.model)"]


def test_find_files_needs_a_pattern(nav):
    with pytest.raises(RepositoryError, match="must not be empty"):
        nav.find_files("  ")


@pytest.mark.parametrize("path, name", [
    ("service_layer/services.py", "service_layer.services"),
    ("domain/__init__.py", "domain"),
    ("top.py", "top"),
    ("README.md", ""),
    ("my-scripts/run.py", ""),   # not importable: "-" in a folder name
])
def test_module_name(path, name):
    assert module_name(path) == name


# ===== 2. Symbols (AST) ======================================================

def test_list_symbols_finds_classes_functions_and_methods(nav):
    lines = nav.list_symbols("domain/model.py")
    text = "\n".join(lines)

    assert "domain/model.py:9-10  class OutOfStock(Exception)" in lines
    assert "domain/model.py:14-16  class OrderLine" in lines
    assert "method Batch.__init__(self, ref, qty)" in text
    assert "method Batch.allocate(self, line: OrderLine)" in text
    assert "function Batch.allocate.helper()" in text      # nested function
    assert "property Batch.available(self)" in text
    assert "class Batch.Audit" in text                       # nested class
    assert "method Batch.Audit.record(self)" in text
    assert "function allocate(line, batches)" in text
    assert "async function refresh(batch)" in text
    assert "class Hint" in text                              # inside "if TYPE_CHECKING:"


def test_find_symbol_by_plain_name_finds_every_definition(nav):
    lines = nav.find_symbol("allocate")
    assert lines == [
        "domain/model.py:23-26  method Batch.allocate(self, line: OrderLine)",
        "domain/model.py:37-38  function allocate(line, batches)",
        "service_layer/services.py:10-13  function allocate(orderid, sku, qty, repo, session)",
    ]


def test_find_symbol_by_qualified_name(nav):
    assert nav.find_symbol("Batch.allocate") == [
        "domain/model.py:23-26  method Batch.allocate(self, line: OrderLine)"]


def test_find_symbol_inside_a_folder_and_in_src_layout(nav):
    assert nav.find_symbol("allocate", path="service_layer") == [
        "service_layer/services.py:10-13  function allocate(orderid, sku, qty, repo, session)"]
    assert nav.find_symbol("total") == ["src/shop/prices.py:1-2  function total(items)"]


def test_find_symbol_can_be_narrowed_to_one_file(nav):
    """`path` may name a single file, not only a folder."""
    assert nav.find_symbol("allocate", path="domain/model.py") == [
        "domain/model.py:23-26  method Batch.allocate(self, line: OrderLine)",
        "domain/model.py:37-38  function allocate(line, batches)"]

    # The same name in another module is then not reported.
    assert nav.find_symbol("clean", path="service_layer/helpers.py") == [
        "service_layer/helpers.py:1-2  function clean(text)"]
    with pytest.raises(RepositoryError, match="Symbol 'clean' not found in 1 Python file"):
        nav.find_symbol("clean", path="service_layer/services.py")


def test_find_symbol_in_a_non_python_file_is_a_clear_error(nav):
    with pytest.raises(RepositoryError, match="not a Python file"):
        nav.find_symbol("allocate", path="README.md")


def test_definitions_in_nested_blocks_are_found(repo, nav):
    """A def or class inside match/async with/except* still belongs to the file."""
    (repo / "blocks.py").write_text(
        "import sys\n"
        "match sys.argv:\n"
        "    case [_]:\n"
        "        def in_match():\n"
        "            pass\n"
        "    case _:\n"
        "        class InOtherCase:\n"
        "            pass\n"
        "\n"
        "\n"
        "class Holder:\n"
        "    match sys.argv:\n"
        "        case [_]:\n"
        "            def method_in_match(self):\n"
        "                pass\n"
        "\n"
        "\n"
        "async def outer():\n"
        "    async with sys.stdin as handle:\n"
        "        def in_async_with():\n"
        "            pass\n"
        "    async for line in handle:\n"
        "        def in_async_for():\n"
        "            pass\n"
    )

    assert nav.list_symbols("blocks.py") == [
        "blocks.py:4-5  function in_match()",
        "blocks.py:7-8  class InOtherCase",
        "blocks.py:11-15  class Holder",
        "blocks.py:14-15  method Holder.method_in_match(self)",
        "blocks.py:18-24  async function outer()",
        "blocks.py:20-21  function outer.in_async_with()",
        "blocks.py:23-24  function outer.in_async_for()",
    ]
    assert nav.find_symbol("Holder.method_in_match") == [
        "blocks.py:14-15  method Holder.method_in_match(self)"]


def test_missing_symbol_is_a_clear_error(nav):
    with pytest.raises(RepositoryError, match=r"Symbol 'Nowhere' not found in \d+ Python file"):
        nav.find_symbol("Nowhere")


@pytest.mark.parametrize("name", ["", "1abc", "a b", "os.system('x')", "../x", "a..b"])
def test_invalid_symbol_names_are_rejected(nav, name):
    with pytest.raises(RepositoryError, match="Invalid symbol name"):
        nav.find_symbol(name)


def test_syntax_error_file_is_reported_not_fatal(repo, nav):
    (repo / "domain" / "broken.py").write_text(BROKEN)

    with pytest.raises(RepositoryError, match=r"Cannot parse domain/broken.py: invalid syntax at line 1"):
        nav.list_symbols("domain/broken.py")

    # Searching the whole repository skips the broken file and says so.
    lines = nav.find_symbol("Batch")
    assert lines[0].startswith("domain/model.py:19-")
    assert any(line.startswith("skipped: Cannot parse domain/broken.py") for line in lines)


def test_odd_files_are_errors_not_crashes(repo, nav):
    (repo / "nulls.py").write_text("x = 1\0\n")
    (repo / "latin.py").write_bytes("name = 'caf\xe9'\n".encode("latin-1"))

    with pytest.raises(RepositoryError, match="Cannot parse nulls.py"):
        nav.list_symbols("nulls.py")
    with pytest.raises(RepositoryError, match="Not a UTF-8 text file"):
        nav.list_symbols("latin.py")
    with pytest.raises(RepositoryError, match="not a Python file"):
        nav.list_symbols("README.md")
    with pytest.raises(RepositoryError, match="File not found"):
        nav.list_symbols("missing.py")


def test_parsing_never_runs_repository_code(repo, nav, tmp_path):
    marker = tmp_path / "code_ran.txt"
    (repo / "evil.py").write_text(
        f"open({str(marker)!r}, 'w').write('ran')\n"
        "def harmless():\n    pass\n")

    nav.list_symbols("evil.py")
    nav.find_symbol("harmless")
    nav.module_dependencies("evil.py")

    assert not marker.exists()


def test_file_with_no_definitions(nav):
    assert nav.list_symbols("tests/test_services.py") == [
        "No classes or functions defined in tests/test_services.py"]


# ===== 3. Imports and dependencies ===========================================

def test_dependencies_of_a_service_module(nav):
    text = "\n".join(nav.module_dependencies("service_layer/services.py"))

    assert "Module: service_layer.services  (service_layer/services.py)" in text
    # Internal: absolute, "from pkg import module", relative, and "from .x import name".
    assert "line 3: from domain import model  -> domain/model.py" in text
    assert "line 4: from domain.model import OrderLine, OutOfStock  -> domain/model.py" in text
    assert "line 5: from adapters.repository import AbstractRepository  -> adapters/repository.py" in text
    assert "line 6: from . import helpers  -> service_layer/helpers.py" in text
    assert "line 7: from .helpers import clean  -> service_layer/helpers.py" in text
    # External, including an alias and an import inside a function.
    assert "External imports (3):" in text
    assert "line 1: import os" in text
    assert "line 2: import json" in text
    assert "line 12: import requests" in text
    # Reverse dependencies.
    assert "Imported by (2):" in text
    assert "entrypoints/flask_app.py:1: from service_layer.services import allocate" in text
    assert "tests/test_services.py:1: from service_layer import services" in text


def test_domain_module_is_imported_by_service_and_adapter(nav):
    text = "\n".join(nav.module_dependencies("domain/model.py"))

    assert "Imports from this repository (0):" in text
    assert "service_layer/services.py:3: from domain import model" in text
    assert "service_layer/services.py:4: from domain.model import OrderLine, OutOfStock" in text
    assert "adapters/repository.py:2: from domain import model" in text


def test_relative_import_above_the_root_is_external_not_an_error(nav):
    text = "\n".join(nav.module_dependencies("adapters/repository.py"))
    assert "line 3: from ..outside import thing" in text
    assert "-> " not in text.split("line 3:")[1].splitlines()[0]


def test_src_layout_imports_are_resolved(nav):
    text = "\n".join(nav.module_dependencies("src/shop/cart.py"))
    assert "Module: src.shop.cart" in text
    assert "line 1: from shop import prices  -> src/shop/prices.py" in text
    assert "line 2: from .prices import total  -> src/shop/prices.py" in text


def test_broken_importer_is_skipped_and_reported(repo, nav):
    (repo / "broken_user.py").write_text("from domain import model\n" + BROKEN)

    lines = nav.module_dependencies("domain/model.py")

    assert any(line.startswith("skipped: Cannot parse broken_user.py") for line in lines)
    with pytest.raises(RepositoryError, match="Cannot parse broken_user.py"):
        nav.module_dependencies("broken_user.py")


# ===== 4. Path restrictions ==================================================

@pytest.mark.parametrize("call", [
    lambda nav: nav.list_symbols("../outside.py"),
    lambda nav: nav.module_dependencies("../outside.py"),
    lambda nav: nav.find_symbol("secret_outside", path=".."),
    lambda nav: nav.find_files("outside", path=".."),
    lambda nav: nav.list_symbols("/etc/passwd.py"),
])
def test_paths_outside_the_root_are_denied(nav, call):
    with pytest.raises(AccessDeniedError):
        call(nav)


def test_git_folder_is_not_analysed(repo, nav):
    commit_all(repo)
    (repo / ".git" / "hooks" / "hook.py").write_text("def in_git():\n    pass\n")

    with pytest.raises(AccessDeniedError, match=".git"):
        nav.list_symbols(".git/hooks/hook.py")
    with pytest.raises(RepositoryError, match="not found"):
        nav.find_symbol("in_git")
    assert nav.find_files("hook") == ["No files match 'hook'"]


def test_symlink_to_a_file_outside_does_not_leak(repo, nav, tmp_path):
    (repo / "link.py").symlink_to(tmp_path / "outside.py")

    with pytest.raises(AccessDeniedError):
        nav.list_symbols("link.py")
    with pytest.raises(RepositoryError) as error:
        nav.find_symbol("secret_outside")
    assert "outside the repository" in str(error.value)   # reported as skipped
    assert "outside.py:1" not in str(error.value)          # but its content is not shown


def test_result_lists_are_limited():
    assert limit(["x"] * MAX_RESULTS, "things") == ["x"] * MAX_RESULTS
    cut = limit(["x"] * (MAX_RESULTS + 5), "things")
    assert len(cut) == MAX_RESULTS + 1
    assert cut[-1] == "... 5 more things not shown"


# ===== 5. Git state ==========================================================

def head(root):
    return subprocess.run(["git", "rev-parse", "HEAD"], cwd=root, capture_output=True,
                          text=True, check=True).stdout.strip()


def test_git_state_of_a_clean_repository(repo, nav):
    commit_all(repo)
    git(repo, "checkout", "-q", "-b", "feature/expiry")

    assert nav.git_state() == [
        "Branch: feature/expiry",
        f"Commit: {head(repo)}",
        "Changed files (0):", "  (none)",
        "Untracked files (0):", "  (none)",
    ]


def test_git_state_shows_modified_staged_deleted_renamed_and_untracked(repo, nav):
    commit_all(repo)
    (repo / "domain" / "model.py").write_text("# changed\n")             # modified
    (repo / "README.md").write_text("# staged\n")
    git(repo, "add", "README.md")                                         # staged
    (repo / "service_layer" / "helpers.py").unlink()                      # deleted
    git(repo, "mv", "src/shop/cart.py", "src/shop/basket.py")             # renamed
    (repo / "new file.py").write_text("x = 1\n")                          # untracked, space

    text = "\n".join(nav.git_state())

    assert " M domain/model.py" in text
    assert "M  README.md" in text
    assert " D service_layer/helpers.py" in text
    assert "R  src/shop/basket.py" in text
    assert "src/shop/cart.py" not in text  # the old name of a rename is not a separate entry
    assert "Untracked files (1):\n  new file.py" in text


def test_git_state_paths_are_relative_to_the_root(tmp_path):
    outer = write_repo(tmp_path / "outer", {"other/x.py": "", "project/app.py": ""})
    commit_all(outer)
    (outer / "project" / "app.py").write_text("changed = True\n")
    (outer / "other" / "x.py").write_text("changed = True\n")   # outside --root

    nav = RepositoryNavigator(RepositoryTools(outer / "project"))
    text = "\n".join(nav.git_state())

    assert "Changed files (1):\n   M app.py" in text
    assert "other/x.py" not in text


def test_git_state_detached_head_and_no_commits(tmp_path):
    empty = write_repo(tmp_path / "empty", {"a.py": ""})
    git(empty, "init", "-q")
    text = "\n".join(RepositoryNavigator(RepositoryTools(empty)).git_state())
    assert "Commit: (no commits yet)" in text
    assert "Untracked files (1):\n  a.py" in text

    detached = commit_all(write_repo(tmp_path / "detached", {"a.py": ""}))
    git(detached, "checkout", "-q", "--detach")
    text = "\n".join(RepositoryNavigator(RepositoryTools(detached)).git_state())
    assert "Branch: (detached HEAD)" in text
    assert f"Commit: {head(detached)}" in text


def test_git_state_outside_a_git_repository_is_an_error(nav):
    with pytest.raises(RepositoryError, match="Git state unavailable: fatal: not a git repository"):
        nav.git_state()


def test_missing_git_program_is_an_error(repo):
    nav = RepositoryNavigator(RepositoryTools(repo), git="git-does-not-exist")
    with pytest.raises(RepositoryError, match="Git state unavailable: Could not start command"):
        nav.git_state()


def test_git_state_is_read_only(repo, nav):
    commit_all(repo)
    (repo / "domain" / "model.py").write_text("# changed\n")
    index = repo / ".git" / "index"
    before = (index.read_bytes(), os.stat(index).st_mtime_ns)

    nav.git_state()

    assert (index.read_bytes(), os.stat(index).st_mtime_ns) == before
    assert not (repo / ".git" / "index.lock").exists()


def test_git_state_does_not_run_repository_helper_programs(repo, nav, tmp_path):
    commit_all(repo)
    marker = tmp_path / "fsmonitor_ran.txt"
    git(repo, "config", "core.fsmonitor", f"touch {marker}; false")

    nav.git_state()

    assert not marker.exists()


def test_parse_porcelain():
    output = " M a.py\0?? new dir/b.py\0R  new.py\0old.py\0A  sub/c.py\0"
    changed, untracked = parse_porcelain(output, prefix="")
    assert changed == [(" M", "a.py"), ("R ", "new.py"), ("A ", "sub/c.py")]
    assert untracked == ["new dir/b.py"]

    changed, _ = parse_porcelain(" M project/a.py\0", prefix="project/")
    assert changed == [(" M", "a.py")]


# ===== 6. The controller offers the tools to the model ======================

def test_navigation_tools_are_in_the_prompt_in_both_modes():
    for mode in ("edit", "readonly"):
        prompt = build_system_prompt(mode=mode)
        for tool in NAVIGATION_TOOLS:
            assert f"- {tool}  arguments:" in prompt


def test_model_navigates_then_reads_the_right_file(repo):
    commit_all(repo)
    h = make_harness(repo, [
        {"tool": "find_files", "arguments": {"pattern": "services"}},
        {"tool": "find_symbol", "arguments": {"name": "Batch.allocate"}},
        {"tool": "list_symbols", "arguments": {"path": "domain/model.py"}},
        {"tool": "module_dependencies", "arguments": {"path": "domain/model.py"}},
        {"tool": "git_state", "arguments": {}},
        {"tool": "done", "arguments": {}, "summary": "Found it"},
    ], mode="readonly")

    result = h.controller.run("Where is allocation implemented?")

    assert result.status == DONE
    assert result.counters.executed == 5
    assert [line for line in h.log if line.startswith("[")] == [
        "[1] find_files services",
        "[2] find_symbol Batch.allocate",
        "[3] list_symbols domain/model.py",
        "[4] module_dependencies domain/model.py",
        "[5] git_state",
        "[6] done",
    ]
    assert "service_layer/services.py  (module service_layer.services)" in tool_result_sent(h.model, 1)
    assert "domain/model.py:23-26  method Batch.allocate" in tool_result_sent(h.model, 2)
    assert "property Batch.available" in tool_result_sent(h.model, 3)
    assert "adapters/repository.py:2: from domain import model" in tool_result_sent(h.model, 4)
    assert "Branch: " in tool_result_sent(h.model, 5)


def test_controller_denies_navigation_outside_the_root(repo):
    h = make_harness(repo, [
        {"tool": "list_symbols", "arguments": {"path": "../outside.py"}},
        {"tool": "find_symbol", "arguments": {"name": "x", "path": "../"}},
        {"tool": "module_dependencies", "arguments": {"path": ".git/config"}},
        {"tool": "done", "arguments": {}},
    ], max_retries=5)

    result = h.controller.run("Look outside")

    assert result.counters.denied == 3
    assert result.counters.executed == 0
    for call in (1, 2, 3):
        assert tool_result_sent(h.model, call).startswith("DENIED: Access denied")
    assert h.tools.calls == []   # nothing reached the file tools


def test_navigation_errors_are_tool_errors_and_the_loop_continues(repo):
    (repo / "broken.py").write_text(BROKEN)
    h = make_harness(repo, [
        {"tool": "find_symbol", "arguments": {"name": "DoesNotExist"}},
        {"tool": "list_symbols", "arguments": {"path": "broken.py"}},
        {"tool": "git_state", "arguments": {}},                 # not a git repository
        {"tool": "find_symbol", "arguments": {"name": "Batch"}},
        {"tool": "done", "arguments": {}},
    ], max_retries=2)

    result = h.controller.run("Find things")

    assert result.status == DONE                     # errors do not hit the retry limit
    assert result.counters.tool_errors == 3
    assert result.counters.denied == 0
    assert "ERROR: Symbol 'DoesNotExist' not found" in tool_result_sent(h.model, 1)
    assert "ERROR: Cannot parse broken.py: invalid syntax at line 1" in tool_result_sent(h.model, 2)
    assert "ERROR: Git state unavailable" in tool_result_sent(h.model, 3)
    assert "class Batch" in tool_result_sent(h.model, 4)


def test_bad_navigation_arguments_are_rejected(repo):
    h = make_harness(repo, [
        Action("find_symbol", {}),                                    # missing name
        Action("git_state", {"path": "."}),                           # takes no arguments
        Action("find_files", {"pattern": 5}),                         # not a string
        {"tool": "done", "arguments": {}},
    ], max_retries=5)

    result = h.controller.run("Bad arguments")

    assert result.counters.denied == 3
    assert "find_symbol needs argument 'name'" in tool_result_sent(h.model, 1)
    assert "git_state does not take argument 'path'" in tool_result_sent(h.model, 2)
    assert "must be a string" in tool_result_sent(h.model, 3)


def test_navigation_output_respects_the_output_limit(repo):
    many = "".join(f"def function_{i}():\n    pass\n" for i in range(300))
    (repo / "many.py").write_text(many)
    h = make_harness(repo, [
        {"tool": "list_symbols", "arguments": {"path": "many.py"}},
        {"tool": "done", "arguments": {}},
    ], max_output_chars=500)

    result = h.controller.run("List")

    sent = tool_result_sent(h.model, 1)
    assert result.counters.truncated == 1
    assert "[OUTPUT TRUNCATED]" in sent
    assert len(sent) < 700


def test_navigation_is_allowed_in_readonly_mode_and_changes_nothing(repo):
    commit_all(repo)
    h = make_harness(repo, [
        {"tool": "find_files", "arguments": {"pattern": "py"}},
        {"tool": "list_symbols", "arguments": {"path": "domain/model.py"}},
        {"tool": "find_symbol", "arguments": {"name": "allocate"}},
        {"tool": "module_dependencies", "arguments": {"path": "domain/model.py"}},
        {"tool": "git_state", "arguments": {}},
        {"tool": "done", "arguments": {}},
    ], mode="readonly")
    result = h.controller.run("Look around")

    assert result.counters.executed == 5 and result.counters.denied == 0
    status = subprocess.run(["git", "status", "--porcelain"], cwd=repo, capture_output=True,
                            text=True, check=True).stdout
    assert status == ""


def test_controller_uses_a_given_navigator(repo):
    class FakeNavigator:
        def git_state(self):
            return ["Branch: fake"]

    from config import Config
    from execution import ExecutionEnvironment
    from model_client import ScriptedModelClient
    model = ScriptedModelClient([{"tool": "git_state", "arguments": {}},
                                 {"tool": "done", "arguments": {}}])
    controller = Controller(Config(root=repo, task="t", mode="readonly", model="m"), model,
                            RepositoryTools(repo, "readonly"), ExecutionEnvironment(repo),
                            log=lambda _: None, navigator=FakeNavigator())
    controller.run("t")
    # The navigator's own output reaches the model, inside the untrusted markers
    # every tool result now carries (see context.py).
    assert tool_result_sent(model, 1) == (
        "Result of git_state:\n"
        "----- BEGIN UNTRUSTED REPOSITORY DATA: git_state -----\n"
        "Branch: fake\n"
        "----- END UNTRUSTED REPOSITORY DATA: git_state -----")

