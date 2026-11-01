"""Navigation: find modules, symbols, dependencies and the Git state.

The model uses these tools to find the code a task needs before it reads
whole files. All of them are read-only:

- find_files:          files and Python modules whose path matches a pattern
- list_symbols:        the classes, functions and methods defined in one file
- find_symbol:         where a class, function or method is defined
- module_dependencies: what a module imports, and which modules import it
- git_state:           branch, commit, and changed/untracked files

Python files are *parsed* with the standard `ast` module, never imported
or run, so repository code cannot execute here. Every file is read through
RepositoryTools, so the root boundary, the .git protection and the file
size limit apply exactly as for read_file. A file with invalid syntax is
reported, never fatal: single-file tools raise a RepositoryError, and
repository-wide tools skip it and say so.

Git runs only as a fixed set of read-only commands (no checkout, commit,
push, ...), with the same protections as verification: no pager, no
fsmonitor helper, and --no-optional-locks so even `git status` does not
write to the repository's .git folder.
"""

import ast
import fnmatch
import re
from dataclasses import dataclass

from execution import ExecutionEnvironment
from repository_tools import RepositoryError

# Result lists longer than this are cut (the controller also cuts long output).
MAX_RESULTS = 200

# A symbol name: Python identifiers, optionally qualified ("Batch.allocate").
SYMBOL_NAME = re.compile(r"[A-Za-z_][A-Za-z0-9_]*(\.[A-Za-z_][A-Za-z0-9_]*)*")

# Folders that commonly hold importable packages ("src layout").
SOURCE_ROOTS = ("", "src/")

# Signatures longer than this are shortened in the output.
MAX_SIGNATURE_CHARS = 120

# Statements that can hold definitions in a nested block. A def or class inside
# one of these still belongs to the file, so the walk has to look inside them.
# ast.TryStar ("except*") only exists from Python 3.11.
BLOCK_NODES = tuple(node for node in (
    ast.If, ast.Try, getattr(ast, "TryStar", None), ast.With, ast.AsyncWith,
    ast.For, ast.AsyncFor, ast.While, ast.Match,
) if node is not None)


def git_state_commands(git="git"):
    """The fixed, read-only git commands git_state may run (only ever these)."""
    safe = [git, "--no-pager", "--no-optional-locks", "-c", "core.fsmonitor=false"]
    return {
        "branch": safe + ["symbolic-ref", "--quiet", "--short", "HEAD"],
        "commit": safe + ["rev-parse", "--verify", "--quiet", "HEAD"],
        "prefix": safe + ["rev-parse", "--show-prefix"],
        "status": safe + ["status", "--porcelain=v1", "-z", "--untracked-files=all", "--", "."],
    }


@dataclass
class Symbol:
    """One definition found in a Python file."""

    kind: str        # "class", "function", "method", "property" ("async " prefix if async)
    name: str        # the plain name, e.g. "allocate"
    qualname: str    # the name with its enclosing classes/functions, e.g. "Batch.allocate"
    path: str        # file, relative to the root
    line: int
    end_line: int
    signature: str = ""  # "(self, line: OrderLine)" for functions, "(Base)" for classes

    def text(self):
        return f"{self.path}:{self.line}-{self.end_line}  {self.kind} {self.qualname}{self.signature}"


@dataclass
class Import:
    """One static import statement in a module."""

    line: int
    statement: str          # e.g. "from domain import model"
    module: str             # the absolute module name it refers to, e.g. "domain.model"
    path: str = ""          # the repository file it resolves to; "" = external


class RepositoryNavigator:
    """Read-only, language-aware navigation of one repository."""

    def __init__(self, tools, timeout_seconds=30, git="git"):
        """
        tools: RepositoryTools for the repository (all file access goes through it)
        git:   the git program (changeable for tests)
        """
        self.tools = tools
        self.root = tools.root
        self.git = ExecutionEnvironment(self.root, timeout_seconds,
                                        allowed_commands=git_state_commands(git))

    # ----- files and modules -------------------------------------------------

    def find_files(self, pattern, path="."):
        """Files under `path` whose path matches `pattern`.

        `pattern` can be a glob ("*.py", "tests/test_*"), part of a path
        ("services"), or a dotted module name ("service_layer.services").
        Plain text matches case-insensitively. Python files are shown with
        their module name.
        """
        pattern = (pattern or "").strip()
        if not pattern:
            raise RepositoryError("pattern must not be empty")

        files = self.tools.list_files(path)
        if any(char in pattern for char in "*?["):
            matches = [f for f in files
                       if fnmatch.fnmatch(f, pattern) or fnmatch.fnmatch(f.rsplit("/", 1)[-1], pattern)]
        else:
            needles = {pattern.lower(), pattern.replace(".", "/").lower()}
            matches = [f for f in files if any(n in f.lower() for n in needles)]
        if not matches:
            return [f"No files match {pattern!r}"]

        lines = []
        for file in matches:
            module = module_name(file)
            lines.append(f"{file}  (module {module})" if module else file)
        return limit(lines, "files")

    # ----- symbols -----------------------------------------------------------

    def list_symbols(self, path):
        """All classes, functions and methods defined in one Python file."""
        symbols = self._symbols(path)
        if not symbols:
            return [f"No classes or functions defined in {path}"]
        return limit([s.text() for s in symbols], "symbols")

    def find_symbol(self, name, path="."):
        """Where a class, function or method called `name` is defined.

        `name` is a plain name ("allocate", matching every definition with
        that name) or a qualified one ("Batch.allocate").
        """
        name = (name or "").strip()
        if not SYMBOL_NAME.fullmatch(name):
            raise RepositoryError(f"Invalid symbol name {name!r}: use a Python name such as "
                                  "'allocate' or 'Batch.allocate'")

        found, skipped, searched = [], [], 0
        for file in self._python_files(path):
            searched += 1
            try:
                symbols = self._symbols(file)
            except RepositoryError as error:
                skipped.append(str(error))
                continue
            found += [s for s in symbols if name in (s.name, s.qualname)]

        notes = [f"skipped: {problem}" for problem in skipped]
        if not found:
            raise RepositoryError(
                f"Symbol {name!r} not found in {searched} Python file(s). "
                "Check the spelling, or use search to look for the text."
                + "".join(f"\n{note}" for note in notes))
        return limit([s.text() for s in found], "definitions") + notes

    def _symbols(self, path):
        """Parse one file and return its Symbols. Raises RepositoryError if impossible."""
        tree = self._parse(path)
        relative = self.tools._relative(self.tools.resolve_path(path))
        symbols = []
        _collect_symbols(tree.body, relative, [], False, symbols)
        return symbols

    # ----- dependencies ------------------------------------------------------

    def module_dependencies(self, path):
        """What a Python module imports, and which repository modules import it."""
        target = self.tools._relative(self.tools.resolve_path(path))
        index = self._module_index()
        imports = self._imports(target, index)

        internal = [i for i in imports if i.path]
        external = [i for i in imports if not i.path]
        lines = [f"Module: {module_name(target) or target}  ({target})", "",
                 f"Imports from this repository ({len(internal)}):"]
        lines += [f"  line {i.line}: {i.statement}  -> {i.path}" for i in internal] or ["  (none)"]
        lines += ["", f"External imports ({len(external)}):"]
        lines += [f"  line {i.line}: {i.statement}" for i in external] or ["  (none)"]

        importers, skipped = [], []
        for file in self._python_files("."):
            if file == target:
                continue
            try:
                found = self._imports(file, index)
            except RepositoryError as error:
                skipped.append(str(error))
                continue
            importers += [f"  {file}:{i.line}: {i.statement}" for i in found if i.path == target]
        lines += ["", f"Imported by ({len(importers)}):"]
        lines += limit(importers, "importers") or ["  (none)"]
        lines += [f"skipped: {problem}" for problem in skipped]
        return lines

    def _imports(self, path, index):
        """Static imports of one module, resolved against the repository's modules."""
        tree = self._parse(path)
        package = _package_of(path)
        result = []
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                for alias in node.names:
                    result.append(Import(node.lineno, f"import {alias.name}", alias.name,
                                         _resolve(alias.name, index)))
            elif isinstance(node, ast.ImportFrom):
                base = _absolute_module(node.module, node.level, package)
                dots = "." * node.level
                names = ", ".join(a.name for a in node.names)
                statement = f"from {dots}{node.module or ''} import {names}"
                if base is None:  # a relative import that goes above the root
                    result.append(Import(node.lineno, statement, f"{dots}{node.module or ''}"))
                    continue
                # "from pkg import mod" may name a submodule: prefer it when it exists.
                resolved = False
                for alias in node.names:
                    sub = f"{base}.{alias.name}" if base else alias.name
                    if alias.name != "*" and sub in index:
                        result.append(Import(node.lineno, statement, sub, index[sub]))
                        resolved = True
                if not resolved:
                    result.append(Import(node.lineno, statement, base, _resolve(base, index)))
        result.sort(key=lambda i: i.line)
        return result

    # ----- structured results for the context manager ------------------------

    def symbols(self, path):
        """The Symbols defined in one Python file, as objects rather than text.

        Same parsing as list_symbols (never imports or runs the file); the
        repository map in context.py needs the fields, not the formatted lines.
        """
        return self._symbols(path)

    def import_graph(self, path="."):
        """{file: {repository files it imports}} for the Python files under `path`.

        Only imports that resolve to a file inside this repository are kept, so
        the result is the internal dependency graph. A file that cannot be
        parsed is left out instead of failing the whole graph.
        """
        index = self._module_index()
        graph = {}
        for file in self._python_files(path):
            try:
                graph[file] = {i.path for i in self._imports(file, index) if i.path}
            except RepositoryError:
                continue
        return graph

    def _module_index(self):
        """{module name: file} for every Python file in the repository."""
        index = {}
        for file in self._python_files("."):
            for source_root in SOURCE_ROOTS:
                if source_root and not file.startswith(source_root):
                    continue
                name = module_name(file[len(source_root):])
                if name:
                    index.setdefault(name, file)
        return index

    # ----- git ---------------------------------------------------------------

    def git_state(self):
        """Branch, commit and changed/untracked files of the repository (read-only)."""
        status = self.git.run("status")
        if status.timed_out or status.exit_code != 0:
            reason = (status.stderr or status.stdout).strip().splitlines()
            raise RepositoryError("Git state unavailable: "
                                  + (reason[0] if reason else status.status()))

        branch = self.git.run("branch")
        branch_text = branch.stdout.strip() if branch.exit_code == 0 else "(detached HEAD)"
        commit = self.git.run("commit")
        commit_text = commit.stdout.strip() if commit.exit_code == 0 else "(no commits yet)"
        prefix = self.git.run("prefix")
        prefix_text = prefix.stdout.strip() if prefix.exit_code == 0 else ""

        changed, untracked = parse_porcelain(status.stdout, prefix_text)
        lines = [f"Branch: {branch_text}", f"Commit: {commit_text}",
                 f"Changed files ({len(changed)}):"]
        lines += [f"  {code} {path}" for code, path in changed] or ["  (none)"]
        lines += [f"Untracked files ({len(untracked)}):"]
        lines += [f"  {path}" for path in untracked] or ["  (none)"]
        return lines

    def state_summary(self):
        """(branch, commit, dirty lines) for a checkpoint or a resume check.

        Same read-only commands as git_state, but as plain values instead of
        report lines. Returns ("", "", []) when git cannot be used at all,
        because a missing git must not stop a run.
        """
        try:
            lines = self.git_state()
        except RepositoryError:
            return "", "", []
        branch = commit = ""
        dirty = []
        for line in lines:
            if line.startswith("Branch: "):
                branch = line[len("Branch: "):].strip()
            elif line.startswith("Commit: "):
                commit = line[len("Commit: "):].strip()
            elif line.startswith("  ") and line.strip() != "(none)":
                dirty.append(line.strip())
        return branch, commit, dirty

    def head_commit(self):
        """The current commit as a plain SHA, or "" if there is none.

        Used to pin a task to its starting commit. Read-only, like git_state.
        """
        commit = self.git.run("commit")
        return commit.stdout.strip() if commit.exit_code == 0 else ""

    # ----- helpers -----------------------------------------------------------

    def _python_files(self, path):
        """The Python files under `path`, which may also name one Python file.

        Searching a single file is a reasonable way to narrow find_symbol, so
        a file path is accepted instead of a folder.
        """
        target = self.tools.resolve_path(path)
        if target.is_file():
            relative = self.tools._relative(target)
            if not relative.endswith(".py"):
                raise RepositoryError(f"{path!r} is not a Python file (.py); "
                                      "only Python files can be analysed")
            return [relative]
        return [f for f in self.tools.list_files(path) if f.endswith(".py")]

    def _parse(self, path):
        """Read (through RepositoryTools) and parse a Python file. Never runs it."""
        if not str(path).endswith(".py"):
            raise RepositoryError(f"{path!r} is not a Python file (.py); "
                                  "only Python files can be analysed")
        text = self.tools.read_file(path)
        try:
            return ast.parse(text, filename=str(path))
        except SyntaxError as error:
            raise RepositoryError(f"Cannot parse {path}: invalid syntax at line "
                                  f"{error.lineno}: {error.msg}") from None
        except (ValueError, RecursionError, MemoryError) as error:
            raise RepositoryError(f"Cannot parse {path}: {type(error).__name__}") from None


# ----- functions without state (easy to test on their own) --------------------

def module_name(path):
    """'service_layer/services.py' -> 'service_layer.services'; 'pkg/__init__.py' -> 'pkg'.

    Returns '' for files that are not importable Python modules.
    """
    if not path.endswith(".py"):
        return ""
    parts = path[:-3].split("/")
    if parts[-1] == "__init__":
        parts = parts[:-1]
    if not parts or not all(part.isidentifier() for part in parts):
        return ""
    return ".".join(parts)


def limit(lines, what):
    """Cut a list of result lines to MAX_RESULTS, saying how many were left out."""
    if len(lines) <= MAX_RESULTS:
        return lines
    return lines[:MAX_RESULTS] + [f"... {len(lines) - MAX_RESULTS} more {what} not shown"]


def parse_porcelain(output, prefix=""):
    """Turn `git status --porcelain=v1 -z` output into (changed, untracked).

    changed is a list of (two-letter code, path); untracked a list of paths.
    Paths are made relative to the repository root folder by removing
    `prefix` (git reports them relative to the top of the whole Git repository).
    """
    changed, untracked = [], []
    entries = output.split("\0")
    i = 0
    while i < len(entries):
        entry = entries[i]
        i += 1
        if len(entry) < 4:
            continue
        code, path = entry[:2], entry[3:]
        if code[0] in "RC":
            i += 1  # renames/copies are followed by the original path
        if prefix and path.startswith(prefix):
            path = path[len(prefix):]
        if code == "??":
            untracked.append(path)
        else:
            changed.append((code, path))
    return changed, untracked


def _collect_symbols(body, path, parents, in_class, out):
    """Walk class/function definitions; nested ones get a qualified name."""
    for node in body:
        if isinstance(node, ast.ClassDef):
            qualname = ".".join(parents + [node.name])
            bases = ", ".join(_unparse(b) for b in node.bases)
            out.append(Symbol("class", node.name, qualname, path, node.lineno,
                              node.end_lineno or node.lineno, f"({bases})" if bases else ""))
            _collect_symbols(node.body, path, parents + [node.name], True, out)
        elif isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            kind = "method" if in_class else "function"
            decorators = [_unparse(d) for d in node.decorator_list]
            if in_class and any(d == "property" or d.endswith((".setter", ".getter", ".deleter"))
                                for d in decorators):
                kind = "property"
            if isinstance(node, ast.AsyncFunctionDef):
                kind = "async " + kind
            qualname = ".".join(parents + [node.name])
            signature = _shorten(f"({_unparse(node.args)})")
            out.append(Symbol(kind, node.name, qualname, path, node.lineno,
                              node.end_lineno or node.lineno, signature))
            _collect_symbols(node.body, path, parents + [node.name], False, out)
        elif isinstance(node, BLOCK_NODES):
            # Definitions inside "if TYPE_CHECKING:", "try: ... except ImportError:",
            # "match ...: case ...:", "async with ...:", ...
            for block in ("body", "orelse", "finalbody"):
                _collect_symbols(getattr(node, block, []), path, parents, in_class, out)
            for handler in getattr(node, "handlers", []):
                _collect_symbols(handler.body, path, parents, in_class, out)
            for case in getattr(node, "cases", []):
                _collect_symbols(case.body, path, parents, in_class, out)


def _unparse(node):
    try:
        return ast.unparse(node)
    except (ValueError, TypeError, AttributeError, RecursionError):
        # very unusual syntax; the name and line are still useful
        return "..."


def _shorten(text):
    if len(text) <= MAX_SIGNATURE_CHARS:
        return text
    return text[:MAX_SIGNATURE_CHARS - 4] + "...)"


def _package_of(path):
    """The package a module belongs to, for resolving relative imports."""
    name = module_name(path)
    for source_root in SOURCE_ROOTS[1:]:
        if path.startswith(source_root):
            name = module_name(path[len(source_root):])
    if path.endswith("__init__.py"):
        return name
    return name.rpartition(".")[0]


def _absolute_module(module, level, package):
    """Resolve 'from ..x import y' to an absolute module name; None if impossible."""
    if level == 0:
        return module or ""
    parts = package.split(".") if package else []
    if level - 1 > len(parts):
        return None
    base = parts[:len(parts) - (level - 1)]
    if module:
        base.append(module)
    return ".".join(base)


def _resolve(module, index):
    """The repository file for a module name ('' if it is not in the repository).

    'a.b.c' that is not a file itself still counts as importing package 'a.b'
    (or 'a') if that exists in the repository.
    """
    parts = module.split(".") if module else []
    while parts:
        candidate = ".".join(parts)
        if candidate in index:
            return index[candidate]
        parts.pop()
    return ""
