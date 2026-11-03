"""Explicit project approvals, independent of the command-line interface."""

import re
import subprocess
import tomllib
from pathlib import Path

HARNESS_DIR = Path(__file__).resolve().parent

# Shown when an approved project is not a throwaway copy.
DISPOSABLE_HINT = ("Approve a disposable copy instead:\n"
                   "    git clone --no-hardlinks <repository> <copy>\n"
                   "    git -C <copy> remote remove origin")


def git_remotes(root):
    """The Git remotes configured in `root`; () if it has none or is not a repository.

    Fixed arguments, no shell, hooks disabled, with a timeout. This only reads.
    """
    try:
        done = subprocess.run(
            ["git", "-C", str(root), "--no-pager", "--no-optional-locks",
             "-c", "core.fsmonitor=false", "-c", "core.hooksPath=/dev/null", "remote"],
            capture_output=True, text=True, timeout=30, check=False)
    except (OSError, subprocess.SubprocessError):
        return ()  # git missing or unusable: other checks still apply
    if done.returncode != 0:
        return ()  # not a Git repository
    return tuple(line.strip() for line in done.stdout.splitlines() if line.strip())


def require_disposable(root):
    """Refuse an approved project that is not a throwaway copy.

    The agent edits an approved project in place, so it must never be someone's
    working repository. A configured Git remote is the signal that it is one: a
    copy prepared for the harness has its remote removed (see `demo/demo.py
    prepare`). This protects the user's own work, not the remote -- pushing is
    impossible either way, because there is no Git write command at all.
    """
    remotes = git_remotes(root)
    if remotes:
        raise ValueError(
            f"approved project {root} still has Git remote(s): {', '.join(remotes)}. "
            "An approved project must be a disposable copy, because the agent edits "
            f"it in place. {DISPOSABLE_HINT}")
    return root


def validate_root(root):
    root = Path(root).expanduser().resolve()
    if not root.is_dir():
        raise ValueError(f"project path does not exist or is not a directory: {root}")
    if root == Path(root.anchor) or root == Path.home().resolve():
        raise ValueError(f"root must be a project folder, not {root}")
    if HARNESS_DIR.is_relative_to(root):
        raise ValueError(f"root must not contain the harness itself ({HARNESS_DIR})")
    return root


def require_external(path, roots, description):
    """Control files must not be writable by an agent in an approved project."""
    resolved = Path(path).expanduser().resolve()
    for root in roots:
        if resolved.is_relative_to(Path(root).resolve()):
            raise ValueError(f"{description} must be outside approved repositories: {resolved}")
    return resolved


def load_projects(path):
    """Read [projects] name = path, resolving relative paths beside the TOML file.

    Missing/empty approvals fail closed. Canonical paths prevent an alias,
    symlink, parent, or child directory from widening an approval.
    """
    path = Path(path).expanduser().resolve()
    try:
        with path.open("rb") as handle:
            data = tomllib.load(handle)
    except (OSError, ValueError) as error:
        raise ValueError(f"cannot read projects configuration {path}: {error}. "
                         "Copy projects.example.toml and explicitly approve a repository.") from None
    if set(data) != {"projects"} or not isinstance(data["projects"], dict):
        raise ValueError("projects configuration must contain only a [projects] table")
    if not data["projects"]:
        raise ValueError("no approved projects; add name = '/path/to/repository' under [projects]")
    projects = {}
    for name, value in data["projects"].items():
        if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_-]*", name):
            raise ValueError(f"invalid project name {name!r}; use letters, numbers, '_' or '-'")
        if not isinstance(value, str) or not value.strip():
            raise ValueError(f"project {name!r} must be a non-empty repository path")
        target = Path(value).expanduser()
        root = validate_root(target if target.is_absolute() else path.parent / target)
        require_disposable(root)
        if root in projects.values():
            raise ValueError(f"duplicate approved repository: {root}")
        projects[name] = root
    require_external(path, projects.values(), "projects configuration")
    return projects
