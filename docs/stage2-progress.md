# Stage 2 progress log

Stage 2 brief: *Build a capable coding harness* (FH Technikum Wien, delivery 2 of 3).
This file records each phase: what was checked or built, test results, and open decisions.

| Phase | Topic | Status |
|---|---|---|
| 1 | Baseline, architecture review, requirement checklist, proposal | **done** |
| 2 | Navigation (symbols, dependencies, Git state) | **done** |
| 3 | Context management (repo map, file selection, bounded history) | planned |
| 4 | Task record, scope enforcement, session store, checkpoints | planned |
| 5 | Cancellation (model request + command) | planned |
| 6 | Safe resume (revalidation, pause on mismatch / uncertain action) | planned |
| 7 | Verification extras (lint/build checks, unfinished work) + check loop | planned |
| 8 | CLI subcommands, allowed projects, interface/core split | planned |
| 9 | Stage 2 feature demo (task, acceptance + regression checks, rehearsal) | planned |
| 10 | README, diagrams, real-model run, results | planned |

---

## Phase 1 — Baseline (2026-10-31)

### Environment

| Item | Value |
|---|---|
| Machine | **Cloud container** (Linux x86_64), not the developer's Mac |
| Python | 3.13.18 |
| Branch | `claude/zen-cannon-oxn8f8` |
| HEAD | `0518ac225f697a8c7177e849e1e7c1f4053795c2` ("Merge pull request #4 …"), identical to `main` |
| Working tree | clean before this phase |
| Docker | Engine 29.9.2 available after starting `dockerd` in the container. `harness-sandbox` image built (pytest 9.2.0, SQLAlchemy 1.4.54) |
| Ollama | **Not available** in this container (nothing on `localhost:11434`). Real-model runs must happen on the Mac |
| Linters on host | `ruff`, `flake8` installed |

Cloud-only build note: the container's HTTPS proxy uses its own CA, so `sandbox/Dockerfile`
could not `pip install` here. A scratchpad-only copy of the Dockerfile that trusts the proxy CA
(TLS verification left on) was used to build the image. **`sandbox/Dockerfile` in the repository
is unchanged** and is still what to use on the Mac.

### Baseline test results

```
python -m pytest -q            ->  152 passed in 11.12s   (0 skipped)
tests/test_sandbox.py          ->  10/10 passed, including the real-Docker tests
```

Stage 1 demo, end to end, with the scripted model and the real Docker sandbox:

```
demo.py prepare      -> fresh copy of cosmicpython/code @ 97267473 (remote removed)
demo.py acceptance   -> 3 failed, 1 passed   FAIL (expected on the starting code)
demo.py rehearse     -> done in 6/20 actions, Verification PASS, Result SUCCESS
demo.py verify       -> acceptance PASS | regression PASS (24 tests) | scope PASS
demo.py prepare      -> target reset to a clean copy afterwards
```

All target-repository code (tests, acceptance check) ran only inside the Docker sandbox.

### Existing architecture (Stage 1)

```text
main.py (CLI) ──> Controller ──> ModelClient ──> Ollama
                     │
       ┌─────────────┼───────────────────┐
       ▼             ▼                   ▼
RepositoryTools  ExecutionEnvironment  selected_files_text (basic context)
 (path-checked)   + DockerSandbox
                     │
                     ▼
                 Verifier (git status/diff + final checks)
```

| Module | Lines | Role | Reusable for Stage 2 |
|---|---|---|---|
| `config.py` | 139 | `Config` dataclass, limits, TOML settings loader with type checks | Extend with context budget, sessions dir, projects file |
| `repository_tools.py` | 161 | `resolve_path()` (root boundary, symlinks, `.git` protected), list/read/search/replace/edit | `resolve_path` and `list_files` are the base for navigation and scope checks |
| `model_client.py` | 343 | `TOOLS` schema, `validate_tool_call`, system prompt, `parse_action`, `ModelClient`, `ScriptedModelClient` | Add new tool schemas; `ScriptedModelClient` drives all deterministic Stage 2 tests |
| `controller.py` | 371 | Loop, `check_permission`, `execute`, `Counters`, `done_problem`, `truncate` | Core of the loop stays; gets hooks for context manager, checkpoints, cancel, scope |
| `execution.py` | 336 | Whitelisted commands, `DockerSandbox`, process-group kill, container removal on timeout/Ctrl+C, env filtering | Basis for cancellation (already cleans up on `BaseException`); add lint/build commands |
| `verification.py` | 163 | Safe fixed git commands (`core.fsmonitor=false`, `--no-ext-diff`), diff incl. untracked, checks never hidden | `git_commands()` pattern reused for the `git_state` navigation tool |
| `main.py` | 234 | argparse, root safety checks, wiring, sectioned output | Split: interface (`cli.py`) vs. run orchestration (core) |
| `demo/demo.py` | 215 | prepare / acceptance / regression / scope / verify / rehearse | Reused for the Stage 2 feature demo |
| `tests/helpers.py` | 119 | `make_harness`, `SpyTools`, `SpyExecutor`, git helpers | Reused by all new tests |

Existing protections that must stay: path boundary + `.git` protection, readonly vs edit mode,
whitelisted commands only (no shell), env filtering, Docker sandbox (read-only mount, no network,
no capabilities, non-root, resource limits), action / retry / output limits, `done` only after a
passing check, truncated files cannot be overwritten, zero tests is a failure, verification
independent of the model.

---

## Stage 2 requirement checklist

Status today: ✅ done in Stage 1 · 🟡 partly · ❌ missing.

### What to build

| ID | Requirement (from the brief) | Today |
|---|---|---|
| N1 | Find relevant **modules** | 🟡 `list_files` + text `search` only |
| N2 | Find **symbols** (language-aware, simple is enough) | ❌ |
| N3 | Find **dependencies** (imports / importers) | ❌ |
| N4 | Inspect **Git state** | 🟡 only in final verification, not a tool |
| C1 | Compact **repository map** | ❌ |
| C2 | **Select files** relevant to the task | 🟡 user-selected `--file` only |
| C3 | **Shorten history** within a **documented size limit** | ❌ history grows unbounded (only per-output truncation) |
| C4 | Preserve **trusted** instructions, scope, acceptance criteria through compression | ❌ |
| C5 | Keep repository text and tool output **untrusted** (separated) | 🟡 implicit only |
| T1 | Save request, **starting commit**, **allowed changes**, checks, task steps | ❌ |
| T2 | Edit–test–fix loop with **bounded retries** and a **clear stopping rule** | ✅ (action/retry limits, `done` gate) |
| T3 | Enforce allowed changes (scope) in application code | ❌ only checked afterwards by `demo.py scope` |
| S1 | Save and **reopen** work after restart | ❌ |
| S2 | Show **progress** | 🟡 live log only, nothing persisted |
| S3 | **Cancel** an active run | 🟡 Ctrl+C kills the command, nothing recorded |
| S4 | **Resume** from a safe checkpoint | ❌ |
| S5 | Mark **uncertain interrupted actions** explicitly; never replay them automatically | ❌ |
| S6 | On resume: preserve consumed limits, revalidate permissions and the saved working-copy state incl. the harness's own edits; pause on mismatch | ❌ |
| V1 | Show changed files and final diff | ✅ |
| V2 | Test **and lint** results | 🟡 tests only |
| V3 | Show **unfinished work** | ❌ |
| V4 | Failed / unavailable checks stay visible, never shown as passed | ✅ |
| V5 | Build checks if needed | ❌ (e.g. compile check) |
| U1 | **Select an allowed project** | ❌ any `--root` |
| U2 | Start and **inspect** a task | 🟡 start only |
| U3 | Cancel, resume, review the result from the CLI | ❌ |
| U4 | Interface code separate from core logic | 🟡 `main.py` mixes both |
| P1 | Disposable target copy; credentials and host files outside executed code | ✅ |
| P2 | Repository code runs contained (not just a worktree) | ✅ Docker |
| P3 | Arguments/permissions checked in application code; repo text and model replies untrusted | ✅ |
| P4 | Push, merge, deployment disabled | 🟡 remote removed in demo copy; must also hold for the new `git_state` tool |
| P5 | A stuck command is stoppable without leftover child processes or writes | ✅ for timeout/Ctrl+C; extend to `cancel` |

### Tests to include

| ID | Test | Today |
|---|---|---|
| TN | Navigation finds modules, symbols, dependencies; shows expected Git state | ❌ |
| TC | Context selects relevant files, enforces size limit; compression keeps task constraints and separates untrusted text | ❌ |
| TS | Session storage: task, steps, history, errors, check results survive an application restart | ❌ |
| TX | Cancellation during a model request or command: no further actions/writes, child processes cleaned up | 🟡 timeout/Ctrl+C cleanup tested, not cancel |
| TR | Safe resume against saved working-copy state (incl. harness edits); recheck permissions and limits; pause on mismatch or uncertain action | ❌ |
| TL | Failed-check handling with scripted replies; failed/unavailable checks never appear as passed | ✅ partly (extend for the loop) |
| T1 | Stage 1 tool, error, action-limit, output-limit, bug-fix tests still pass | ✅ 152 passing |
| TF | Feature checks fail on starting code, pass after the change; existing callers + regression still work | ❌ |

### Delivery 2 checklist

| ID | Deliverable | Today |
|---|---|---|
| D1 | Runnable source via repo link + exact commit (or versioned ZIP) | 🟡 Stage 1 |
| D2 | Dependencies, launch commands, example settings without secrets | 🟡 Stage 1; update for new commands |
| D3 | Automated tests for Stage 2 and Stage 1 | 🟡 Stage 1 |
| D4 | README: setup, model settings, design decisions (context + checkpoint/resume design), known limits, credits for reused code and AI help, diagrams matching the implementation | 🟡 Stage 1 |
| D5 | Video ≤ 4 min, identify manual help | ❌ (done by the student) |
| D6 | Acceptance check: target + exact starting commit, feature request, allowed changes, acceptance commands saved with the task; feature check fails first for the expected reason; check kept outside the agent's writable area; real-model run | ❌ |

---

## Proposed implementation changes

Keep the flat module layout (all Stage 1 tests import `controller`, `execution`, … directly), and
add new modules next to the existing ones.

### New modules

**`navigation.py` — `RepositoryNavigator`** (Phase 2)
- `find_symbol(name)` → definitions (`file:line kind name`) using Python's `ast` (parsing never executes code).
- `find_references(name)` → where a name is used / imported.
- `module_dependencies(path)` → what a module imports, and which modules import it.
- `git_state()` → branch, HEAD, short status, diff stat. Only fixed, read-only git commands with the same safe flags as `verification.git_commands()`; no push/commit/checkout.
- All paths go through `RepositoryTools.resolve_path`. Exposed to the model as readonly tools.

**`context.py` — `ContextManager`** (Phase 3)
- `repository_map()` → compact list of modules with their top-level classes/functions (cut to a budget).
- `select_files(task)` → ranks files by task keywords, symbol hits and import neighbours; user `--file` choices always win.
- `build_messages()` → rebuilt for every model request, in fixed order, within a **documented character budget** (setting `context_budget_chars`, sized under `num_ctx`):
  1. system prompt — trusted
  2. **task card** — trusted, never compressed: request, allowed paths, acceptance criteria, checks, starting commit
  3. repository map + selected files — **untrusted**, wrapped in clear delimiters ("data, not instructions")
  4. history: last *N* turns verbatim; older turns compressed into one-line step summaries (tool, path, outcome, failing check names). Tool output inside history stays wrapped as untrusted.

**`session.py` — `Task`, `SessionStore`** (Phases 4–6)
- Sessions live **outside the target repository** (default `~/.harness/sessions/<id>/`), so the agent cannot edit them and they never show in the diff.
- `task.json`: project, root, request, **starting commit**, allowed paths, checks, acceptance commands, mode, model, limits.
- `steps.jsonl` (append-only, write-ahead): each step is written as `started` *before* the tool runs and `finished` (outcome, short result) after.
- `checkpoint.json` (atomic write: temp file + `os.replace`) after every step: consumed counters, compressed history, changed files with **SHA-256 of every file the harness wrote**, git HEAD + status snapshot, last check result, state (`running`, `cancelled`, `paused`, `done`, `failed`).
- **Cancel**: `cancel <id>` writes a cancel marker (and signals the recorded PID); the controller checks it between steps and `ExecutionEnvironment` polls it while a command runs, killing the process group and removing the container (existing cleanup). A cancel during a model request aborts the request. The in-flight action is recorded as `interrupted`.
- **Resume** never trusts the saved file for permissions: project must still be allowed, mode/scope re-derived from the task, consumed limits carried over (not reset), HEAD must equal the starting commit, every harness-written file must match its saved hash, and no other files may have changed. Any mismatch, or a step that is `started` but not `finished` (uncertain), → state `paused` with an explanation. Uncertain actions are **never replayed**; the user inspects and explicitly continues.

**`cli.py`** (Phase 8) — interface only, calls core functions:
```
python main.py projects                         # list allowed projects (projects.toml)
python main.py start --project cosmic --task …  # start a task (allowed paths, checks from the task)
python main.py list | status <id> | show <id>   # inspect progress, steps, diff, checks
python main.py cancel <id>
python main.py resume <id>
```
The Stage 1 form `python main.py --root … --task … --mode … --model …` keeps working so all Stage 1 tests and the demo stay valid.

### Changes to existing modules

| Module | Change |
|---|---|
| `controller.py` | Use `ContextManager` for messages; new permission check: edits only inside the task's allowed paths (tests protected); call `SessionStore` before/after each step; check the cancel marker; resume from a checkpoint (restored counters/history) |
| `model_client.py` | Add tool schemas: `find_symbol`, `find_references`, `module_dependencies`, `git_state`, `repo_map` (readonly); prompt explains trusted task card vs untrusted data |
| `execution.py` | Add whitelisted `lint` (`ruff check` on Python files) and `build` (`python -m compileall -q .`) commands; cancel-aware wait loop (poll instead of a single `communicate`) |
| `verification.py` | Lint + build results next to tests; "Unfinished work" section (run status, uncertain steps, failing/unavailable checks, out-of-scope changes) |
| `config.py` / `settings.example.toml` | `context_budget_chars`, `history_keep_turns`, `sessions_dir`, `projects_file` |
| `sandbox/Dockerfile`, `demo/requirements-target.txt` | Add `ruff` so lint runs inside the sandbox |
| `main.py` | Thin entry point that dispatches to `cli.py` |

### Proposed Stage 2 feature (same target, same starting commit)

Cosmic Python `chapter_05_high_gear_low_gear` @ `97267473` — the brief's own example fits this code base directly:

- **Optional expiry on allocations**: `services.allocate(..., expires_at=None)`; the line remembers when it expires.
- **Release expired allocations**: new `services.release_expired(now, repo, session)` deallocates expired lines so their stock is available again (uses the existing `Batch.deallocate`).
- **Error case**: `InvalidExpiry` for an expiry in the past or of the wrong type, raised before anything is allocated or committed.
- **Existing callers**: `allocate(orderid, sku, qty, repo, session)` without expiry behaves exactly as before; existing 24 unit/integration tests are the regression check.
- Modules involved: `domain/model.py` (OrderLine/Batch expiry rules), `service_layer/services.py` (validation + use case), `adapters/orm.py` (nullable `expires_at` column so it persists).
- Acceptance check in `demo/acceptance/` (outside the agent's writable area), run in the sandbox; must fail on `97267473` for the expected reason before the run.

### Open decisions

1. **Feature**: expiry on allocations (above), or another feature?
2. **Starting code**: the original commit `97267473` (recommended, clean history) or the Stage 1 fix applied first?
3. **Model**: keep `qwen2.5-coder:7b` (Stage 1, proven) or try the brief's suggested Qwen 3.x for the real run.
4. **Where the work lives**: this phase ran in a cloud container, not on the Mac. Files here are lost when the container is reclaimed unless they are pushed to the branch or copied out.

---

## Phase 2 — Repository navigation (2026-10-31)

Requirements covered: **N1, N2, N3, N4** (and P4 for git: read-only commands only). Test: **TN**.

### What was built

New module `navigation.py` with `RepositoryNavigator`, used by the controller and offered to
the model as five read-only tools (allowed in `readonly` and `edit` mode):

| Tool | Arguments | Result |
|---|---|---|
| `find_files` | `pattern`, optional `path` | Files whose path contains the text (case-insensitive), match a glob (`tests/test_*.py`), or a dotted module name (`service_layer.services`). Python files show their module name. |
| `list_symbols` | `path` | Every class, function, method and property in one Python file: `path:start-end kind Qualified.name(signature)`. Includes nested classes/functions and definitions inside `if`/`try` blocks (e.g. `if TYPE_CHECKING:`). |
| `find_symbol` | `name`, optional `path` | Definitions of a plain name (`allocate`: all of them) or a qualified one (`Batch.allocate`). |
| `module_dependencies` | `path` | The module's static imports split into repository-internal (with the file each resolves to) and external, plus the repository modules that import it. Handles `import a.b`, `from a import b` (submodule or name), relative imports (`from . import x`, `from ..x import y`), imports inside functions, and a `src/` layout. |
| `git_state` | none | Branch (or detached HEAD), commit (or "no commits yet"), changed files with their two-letter status, untracked files. Paths relative to `--root`, also when it is a subfolder of a bigger repository. |

### Design decisions

- **Parse, never run.** Python files are analysed with `ast.parse`. Repository code is never imported or executed (tested with a file that would write a marker file if it ran).
- **Same restrictions as `read_file`.** Every file goes through `RepositoryTools.list_files` / `read_file`, so the root boundary, symlink check, `.git` protection, 1 MB size limit and UTF-8 check apply unchanged. The controller's existing `check_permission` also validates every `path` argument before the tool runs, so a request outside the root is **denied** (counts towards the retry limit) and never reaches the file tools.
- **Errors are reported, not fatal.** Invalid syntax, null bytes, non-UTF-8, non-Python files, missing files, missing symbols, invalid symbol names, not-a-git-repository and a missing `git` program become a `RepositoryError`: the model gets `ERROR: ...` (a tool error, which does not count towards the retry limit) and the loop continues. Repository-wide tools skip unparsable files and list them as `skipped: ...` so nothing is silently hidden.
- **Output limits.** Navigation lists are cut to 200 entries with a "... N more not shown" line; after that, the controller's existing `max_output_chars` truncation applies as for every tool.
- **Read-only Git.** `git_state` can only run four fixed commands (`symbolic-ref`, `rev-parse` ×2, `status --porcelain=v1 -z`), through an `ExecutionEnvironment` whitelist (no shell, filtered environment, timeout, process-group kill). Flags: `--no-pager`, `-c core.fsmonitor=false` (a repository cannot make git start a helper program), and `--no-optional-locks` so even `git status` does not rewrite `.git/index`. Git runs on the host, like verification: it is the harness's own tool, not repository code. No command can change branches, commit, push or touch remotes.
- **Prompt.** The system prompt lists the new tools and adds one rule: locate code with `find_symbol` / `find_files` / `module_dependencies` before reading whole files.

### Files

| File | Change |
|---|---|
| `navigation.py` | **new** — `RepositoryNavigator`, `module_name`, `parse_porcelain`, `git_state_commands` |
| `controller.py` | builds a `RepositoryNavigator` from the same `RepositoryTools` (or takes one); five new handlers; progress line shows `pattern`/`name` |
| `model_client.py` | tool schemas + help text for the five tools, `NAVIGATION_TOOLS`, one new prompt rule |
| `tests/test_navigation.py` | **new** — 56 tests |
| `README.md` | module table: `navigation.py` |

### Tests (`tests/test_navigation.py`, 56 tests)

| Area | What is checked |
|---|---|
| Files/modules | substring, glob, dotted module name, inside a folder, case-insensitive, no match, empty pattern, `module_name` rules |
| Symbols | classes, functions, methods, properties, async, nested classes/functions, `if TYPE_CHECKING:` definitions, line ranges and signatures; plain vs qualified names; folder filter; `src/` layout |
| Dependencies | absolute, `from pkg import module`, `from module import name`, relative (`.`/`..`), alias, import inside a function, external vs internal, reverse "imported by", relative import above the root, `src/` layout |
| Errors | syntax error (single file → error; repository-wide → skipped and reported), null bytes, non-UTF-8, non-Python, missing file, missing symbol, invalid symbol names, parsing never runs code |
| Permissions | `..`, absolute paths, `path=".."`, `.git` contents, symlink to a file outside (denied, content not leaked), result list limit |
| Git state | clean repo with branch + commit, modified/staged/deleted/renamed/untracked (incl. a space in a name), root inside a bigger repository, detached HEAD, no commits yet, not a git repo, git missing, `.git/index` unchanged (read-only), `core.fsmonitor` helper not run, porcelain parsing |
| Controller | tools listed in the prompt in both modes; scripted model navigates (log lines + results sent back); outside-root requests denied before any file tool runs; navigation errors are tool errors and don't hit the retry limit; bad arguments rejected; output truncated by `max_output_chars`; readonly mode allowed and the working tree stays clean; injected navigator is used |

### Results

```
python -m pytest -q tests/test_navigation.py   ->  56 passed
python -m pytest -q                            ->  208 passed (152 Stage 1 + 56 new), 0 skipped, Docker tests included
demo.py rehearse + verify (Stage 1 demo)       ->  SUCCESS; acceptance / regression / scope PASS
ruff --select F,E9,B,BLE,I on the new files    ->  clean
```

Smoke test on the real target (Cosmic Python @ `97267473`, parsing only):
`find_symbol allocate` → `domain/model.py` function + `Batch.allocate` method + `service_layer/services.py`;
`module_dependencies service_layer/services.py` → imports `domain/model.py`, `adapters/repository.py`;
imported by `entrypoints/flask_app.py`, `tests/unit/test_services.py`; `git_state` → detached HEAD at `97267473`, clean.

### Known limits

- Python only (the brief allows simple language-aware tools). Other files are still reachable with `find_files`, `read_file` and `search`.
- Static analysis: dynamic imports (`importlib.import_module("x")`, `__import__`) and names created at run time are not found.
- `find_symbol` finds definitions, not every use of a name; use `search` (text) or `module_dependencies` (imports) for uses.
- `git_state` does not yet compare against the task's starting commit; that comes with the task record (Phase 4).
