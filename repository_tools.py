"""Repository tools: controlled access to files in the target repository.

Every tool goes through resolve_path(), which turns the requested path into a
real absolute path and refuses anything that ends up outside the
repository root or inside the .git folder. These tools only touch files;
they never run commands.
"""

import os
from pathlib import Path

from config import MODES

# Folders that are never useful to the model and can be very large.
IGNORED_DIRS = {".git", ".venv", "venv", "__pycache__", ".pytest_cache", "node_modules"}

# The .git folder is off limits: a changed .git/config can make git itself run
# commands (e.g. core.fsmonitor) when verification calls "git status".
PROTECTED_DIRS = {".git"}

# Files larger than this are not read (protects memory; output is cut anyway).
MAX_FILE_BYTES = 1_000_000


class RepositoryError(Exception):
    """A tool request failed (e.g. file not found). The message explains why."""


class AccessDeniedError(RepositoryError):
    """A tool request was refused for safety (outside the root, or readonly)."""


class RepositoryTools:
    """Safe, limited file operations inside one repository folder."""

    def __init__(self, root, mode="readonly"):
        """Remember the repository root and whether editing is allowed."""
        self.root = Path(root).resolve()
        if not self.root.is_dir():
            raise RepositoryError(f"Repository root is not a directory: {self.root}")
        if mode not in MODES:
            raise RepositoryError(f"Unknown mode {mode!r}, expected one of {MODES}")
        self.mode = mode

    # ----- path safety -----------------------------------------------------

    def resolve_path(self, path):
        """Turn a user-supplied path into a safe absolute path inside the root.

        Raises AccessDeniedError if the path points outside the root or into .git.

        Relative paths are taken relative to the root. resolve() then removes
        any '..' parts and follows symlinks, so we can check where the path
        really points. Anything outside the root is refused.
        """
        try:
            target = (self.root / path).resolve()
        except (OSError, ValueError) as error:
            raise RepositoryError(f"Invalid path {path!r}: {error}") from None

        if not target.is_relative_to(self.root):
            raise AccessDeniedError(f"Access denied: {path!r} is outside the repository")
        parts = {part.lower() for part in target.relative_to(self.root).parts}
        if parts & PROTECTED_DIRS:
            raise AccessDeniedError(f"Access denied: {path!r} is inside the protected .git folder")
        return target

    def _relative(self, target):
        """Return a path relative to the root, using '/' separators."""
        return target.relative_to(self.root).as_posix()

    # ----- tools -----------------------------------------------------------

    def list_files(self, path="."):
        """Return a sorted list of files under `path`, relative to the root."""
        directory = self.resolve_path(path)
        if not directory.is_dir():
            raise RepositoryError(f"Not a directory: {path!r}")

        files = []
        for current, dir_names, file_names in os.walk(directory):
            # Editing dir_names in place tells os.walk not to enter those folders.
            dir_names[:] = [d for d in dir_names if d not in IGNORED_DIRS]
            for name in file_names:
                files.append(self._relative(Path(current) / name))
        return sorted(files)

    def read_file(self, path):
        """Return the text content of one file."""
        target = self.resolve_path(path)
        if not target.is_file():
            raise RepositoryError(f"File not found: {path!r}")
        size = target.stat().st_size
        if size > MAX_FILE_BYTES:
            raise RepositoryError(f"File too large to read: {path!r} ({size} bytes, "
                                  f"limit {MAX_FILE_BYTES}). Use search instead.")
        try:
            return target.read_text(encoding="utf-8")
        except UnicodeDecodeError:
            raise RepositoryError(f"Not a UTF-8 text file: {path!r}") from None
        except OSError as error:
            raise RepositoryError(f"Could not read {path!r}: {error}") from None

    def search(self, query, path="."):
        """Find lines containing `query` (plain text, case-sensitive).

        Returns a list of strings like "src/app.py:12: total = a + b".
        Files that are not UTF-8 text are skipped.
        """
        if not query:
            raise RepositoryError("Search query must not be empty")

        matches = []
        for relative_path in self.list_files(path):
            try:
                text = self.read_file(relative_path)
            except RepositoryError:
                continue
            for line_number, line in enumerate(text.splitlines(), start=1):
                if query in line:
                    matches.append(f"{relative_path}:{line_number}: {line.strip()}")
        return matches

    def replace_in_file(self, path, old, new):
        """Replace the one occurrence of `old` with `new` in a file. Edit mode only.

        `old` must appear exactly once, so the change cannot land in the wrong
        place. Unlike edit_file, the rest of the file is never touched.
        """
        if self.mode != "edit":
            raise AccessDeniedError("replace_in_file is disabled in readonly mode")
        if not old:
            raise RepositoryError('"old" must not be empty')

        text = self.read_file(path)
        count = text.count(old)
        if count == 0:
            raise RepositoryError(
                f"The text in \"old\" was not found in {path!r}. Read the file again and "
                "copy the lines exactly, including spaces and indentation.")
        if count > 1:
            raise RepositoryError(
                f"The text in \"old\" appears {count} times in {path!r}. "
                "Include more surrounding lines so it is unique.")
        self.edit_file(path, text.replace(old, new, 1))
        return f"Replaced 1 occurrence in {self._relative(self.resolve_path(path))}"

    def edit_file(self, path, content):
        """Replace the whole content of a file (or create it). Edit mode only."""
        if self.mode != "edit":
            raise AccessDeniedError("edit_file is disabled in readonly mode")

        target = self.resolve_path(path)
        if target.is_dir():
            raise RepositoryError(f"Cannot write to a directory: {path!r}")
        try:
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text(content, encoding="utf-8")
        except OSError as error:
            raise RepositoryError(f"Could not write {path!r}: {error}") from None
        return f"Wrote {len(content)} characters to {self._relative(target)}"
