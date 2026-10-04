"""Tests for RepositoryTools.

Every test builds its own small repository inside pytest's tmp_path,
so no real user files are ever touched. The layout is:

    tmp_path/
    ├── outside.txt          <- must never be readable or writable
    └── work/
        └── repo/            <- the repository root
            ├── README.md
            └── src/
                └── calc.py
"""

import pytest

from repository_tools import MAX_FILE_BYTES, AccessDeniedError, RepositoryError, RepositoryTools


@pytest.fixture
def repo(tmp_path):
    """Create the example repository and return its root path."""
    (tmp_path / "outside.txt").write_text("secret\n")

    root = tmp_path / "work" / "repo"
    (root / "src").mkdir(parents=True)
    (root / "README.md").write_text("# Demo\nComputes a total.\n")
    (root / "src" / "calc.py").write_text(
        "def total(items):\n    return sum(items) + 1\n"
    )
    return root


# ----- the seven required cases ---------------------------------------------

def test_read_valid_file(repo):
    tools = RepositoryTools(repo)
    assert tools.read_file("src/calc.py").startswith("def total(items):")


def test_list_valid_files(repo):
    tools = RepositoryTools(repo)
    assert tools.list_files() == ["README.md", "src/calc.py"]
    assert tools.list_files("src") == ["src/calc.py"]


def test_search_text(repo):
    tools = RepositoryTools(repo)
    assert tools.search("sum(") == ["src/calc.py:2: return sum(items) + 1"]
    assert tools.search("not in any file") == []


def test_edit_valid_file_in_edit_mode(repo):
    tools = RepositoryTools(repo, mode="edit")
    new_code = "def total(items):\n    return sum(items)\n"

    message = tools.edit_file("src/calc.py", new_code)

    assert "src/calc.py" in message
    assert (repo / "src" / "calc.py").read_text() == new_code


def test_edit_fails_in_readonly_mode(repo):
    tools = RepositoryTools(repo, mode="readonly")
    original = (repo / "src" / "calc.py").read_text()

    with pytest.raises(RepositoryError, match="readonly"):
        tools.edit_file("src/calc.py", "changed")

    assert (repo / "src" / "calc.py").read_text() == original


def test_path_traversal_is_rejected(repo):
    tools = RepositoryTools(repo, mode="edit")

    with pytest.raises(RepositoryError, match="outside the repository"):
        tools.read_file("../../outside.txt")
    with pytest.raises(RepositoryError, match="outside the repository"):
        tools.edit_file("../../outside.txt", "hacked")
    with pytest.raises(RepositoryError, match="outside the repository"):
        tools.list_files("../..")

    assert (repo.parent.parent / "outside.txt").read_text() == "secret\n"


def test_absolute_path_outside_is_rejected(repo):
    tools = RepositoryTools(repo, mode="edit")
    outside = repo.parent.parent / "outside.txt"

    with pytest.raises(RepositoryError, match="outside the repository"):
        tools.read_file(str(outside))
    with pytest.raises(RepositoryError, match="outside the repository"):
        tools.edit_file(str(outside), "hacked")

    assert outside.read_text() == "secret\n"


# ----- a few extra edge cases ------------------------------------------------

def test_absolute_path_inside_is_allowed(repo):
    tools = RepositoryTools(repo)
    assert "Demo" in tools.read_file(str(repo / "README.md"))


def test_missing_file_gives_clear_error(repo):
    tools = RepositoryTools(repo)
    with pytest.raises(RepositoryError, match="File not found"):
        tools.read_file("nope.py")


def test_symlink_pointing_outside_is_rejected(repo):
    link = repo / "sneaky.txt"
    try:
        link.symlink_to(repo.parent.parent / "outside.txt")
    except OSError:
        pytest.skip("symlinks not supported on this system")

    tools = RepositoryTools(repo)
    with pytest.raises(RepositoryError, match="outside the repository"):
        tools.read_file("sneaky.txt")


def test_edit_can_create_new_file(repo):
    tools = RepositoryTools(repo, mode="edit")
    tools.edit_file("tests/test_calc.py", "# new test\n")
    assert (repo / "tests" / "test_calc.py").read_text() == "# new test\n"


def test_sibling_folder_with_same_prefix_is_rejected(repo):
    # "repo-backup" starts with the same letters as "repo" but is outside it.
    sibling = repo.parent / "repo-backup"
    sibling.mkdir()
    (sibling / "data.txt").write_text("not yours\n")

    tools = RepositoryTools(repo)
    with pytest.raises(RepositoryError, match="outside the repository"):
        tools.read_file(str(sibling / "data.txt"))


def test_git_folder_is_protected(tmp_path):
    root = tmp_path / "repo"
    (root / ".git").mkdir(parents=True)
    (root / ".git" / "config").write_text("[core]\n")
    tools = RepositoryTools(root, mode="edit")

    for action in (lambda: tools.read_file(".git/config"),
                   lambda: tools.edit_file(".git/config", "[core]\n\tfsmonitor = evil\n"),
                   lambda: tools.edit_file("sub/../.git/hooks/pre-commit", "evil"),
                   lambda: tools.list_files(".git")):
        with pytest.raises(AccessDeniedError, match="protected .git"):
            action()
    assert (root / ".git" / "config").read_text() == "[core]\n"
    assert not (root / ".git" / "hooks").exists()


def test_too_large_file_is_not_read(repo):
    (repo / "big.txt").write_text("x" * (MAX_FILE_BYTES + 1))
    with pytest.raises(RepositoryError, match="too large"):
        RepositoryTools(repo).read_file("big.txt")


# ----- replace_in_file ----------------------------------------------------------

def test_replace_changes_only_the_matching_text(repo):
    tools = RepositoryTools(repo, mode="edit")
    message = tools.replace_in_file("src/calc.py", "sum(items) + 1", "sum(items)")
    assert message == "Replaced 1 occurrence in src/calc.py"
    assert (repo / "src" / "calc.py").read_text() == "def total(items):\n    return sum(items)\n"


def test_replace_text_not_found(repo):
    tools = RepositoryTools(repo, mode="edit")
    with pytest.raises(RepositoryError, match="not found.*indentation"):
        tools.replace_in_file("src/calc.py", "return sum(items)+1", "x")
    assert "+ 1" in (repo / "src" / "calc.py").read_text()


def test_replace_text_must_be_unique(repo):
    (repo / "dup.py").write_text("a = 1\na = 1\n")
    tools = RepositoryTools(repo, mode="edit")
    with pytest.raises(RepositoryError, match="appears 2 times"):
        tools.replace_in_file("dup.py", "a = 1", "a = 2")
    assert (repo / "dup.py").read_text() == "a = 1\na = 1\n"


def test_replace_refused_in_readonly_mode_and_outside_root(repo):
    with pytest.raises(AccessDeniedError, match="readonly"):
        RepositoryTools(repo, mode="readonly").replace_in_file("src/calc.py", "1", "2")
    with pytest.raises(AccessDeniedError, match="outside"):
        RepositoryTools(repo, mode="edit").replace_in_file("../../outside.txt", "secret", "x")
    assert (repo.parent.parent / "outside.txt").read_text() == "secret\n"
