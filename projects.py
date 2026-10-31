"""Explicit project approvals, independent of the command-line interface."""

import re
import tomllib
from pathlib import Path

HARNESS_DIR = Path(__file__).resolve().parent


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
        if root in projects.values():
            raise ValueError(f"duplicate approved repository: {root}")
        projects[name] = root
    require_external(path, projects.values(), "projects configuration")
    return projects
