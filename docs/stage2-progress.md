# Stage 2 progress log

Stage 2 brief: *Build a capable coding harness* (FH Technikum Wien, delivery 2 of 3).
This file records each phase: what was checked or built, test results, and open decisions.

| Phase | Topic | Status |
|---|---|---|
| 1 | Baseline, architecture review, requirement checklist, proposal | **done** |
| 2 | Navigation (symbols, dependencies, Git state) | **done** |
| 3 | Context management (repo map, file selection, bounded history) | **not started** (skipped for now) |
| 4 | Task record, scope enforcement, action log, stopping rules | **done** |
| 5 | Cancellation (from another process, mid-request, mid-command) | **done** |
| 6 | Session storage and safe resume | **done** |
| 7 | Verification extras (lint/build checks, unfinished work) + check loop | planned |
| 8 | CLI subcommands, allowed projects, interface/core split | **done** |
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
| U1 | **Select an allowed project** | ✅ named, explicit TOML approvals (Phase 8) |
| U2 | Start and **inspect** a task | ✅ `start`, `status`, `show`, `list` (Phase 8) |
| U3 | Cancel, resume, review the result from the CLI | ✅ `cancel`, safe `resume`, saved checks/diff (Phase 8) |
| U4 | Interface code separate from core logic | ✅ `cli.py` / `application.py` / controller (Phase 8) |
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
| `list_symbols` | `path` | Every class, function, method and property in one Python file: `path:start-end kind Qualified.name(signature)`. Includes nested classes/functions and definitions inside any nested block (`if TYPE_CHECKING:`, `try`/`except*`, `with`/`async with`, `for`/`async for`, `while`, `match`/`case`). |
| `find_symbol` | `name`, optional `path` | Definitions of a plain name (`allocate`: all of them) or a qualified one (`Batch.allocate`). `path` may name a folder **or a single file**. |
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
| `tests/test_navigation.py` | **new** — 59 tests |
| `README.md` | module table: `navigation.py` |

### Tests (`tests/test_navigation.py`, 59 tests)

| Area | What is checked |
|---|---|
| Files/modules | substring, glob, dotted module name, inside a folder, case-insensitive, no match, empty pattern, `module_name` rules |
| Symbols | classes, functions, methods, properties, async, nested classes/functions, definitions in nested blocks (`if TYPE_CHECKING:`, `match`/`case`, `async with`, `async for`), line ranges and signatures; plain vs qualified names; folder filter; **single-file filter**; non-Python file filter; `src/` layout |
| Dependencies | absolute, `from pkg import module`, `from module import name`, relative (`.`/`..`), alias, import inside a function, external vs internal, reverse "imported by", relative import above the root, `src/` layout |
| Errors | syntax error (single file → error; repository-wide → skipped and reported), null bytes, non-UTF-8, non-Python, missing file, missing symbol, invalid symbol names, parsing never runs code |
| Permissions | `..`, absolute paths, `path=".."`, `.git` contents, symlink to a file outside (denied, content not leaked), result list limit |
| Git state | clean repo with branch + commit, modified/staged/deleted/renamed/untracked (incl. a space in a name), root inside a bigger repository, detached HEAD, no commits yet, not a git repo, git missing, `.git/index` unchanged (read-only), `core.fsmonitor` helper not run, porcelain parsing |
| Controller | tools listed in the prompt in both modes; scripted model navigates (log lines + results sent back); outside-root requests denied before any file tool runs; navigation errors are tool errors and don't hit the retry limit; bad arguments rejected; output truncated by `max_output_chars`; readonly mode allowed and the working tree stays clean; injected navigator is used |

### Results

```
python -m pytest -q tests/test_navigation.py   ->  59 passed
python -m pytest -q                            ->  211 passed (152 Stage 1 + 59 new), 0 skipped, Docker tests included
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

### Phase 2 follow-up — verification on the Mac and two fixes (2026-10-31)

Phase 2 was first built and committed from a cloud container with no Ollama, so the
"so Ollama can use them" half of the requirement had never actually been exercised
against a live model. This follow-up re-ran everything on the developer's Mac
(Python 3.14.7, Docker 29.8.2, Ollama 0.40.11) and closed two real gaps found by
probing the navigator against the **real** target repository rather than fixtures.

**Live model run** — readonly, `qwen2.5-coder:7b`, target Cosmic Python @ `97267473`:

```
[1] find_symbol allocate
[2] module_dependencies domain/model.py
[3] git_state
[4] done
Run: done (actions 4/20 | executed 3 | malformed 0 | denied 0 | tool errors 0 | truncated 0)
Verification: PASS (1/1 checks passed)      Result: SUCCESS
```

The model picked all three navigation tools unprompted, in a sensible order, with no
malformed replies and no denied requests, and changed nothing. That is the evidence
that the tools are usable by a real local model, not only by the scripted one.

**Fix 1 — definitions inside `match`, `async with` and `async for` were invisible.**
`_collect_symbols` descended into `if`/`try`/`with`/`for`/`while` but not `ast.Match`,
`ast.AsyncWith`, `ast.AsyncFor` or `ast.TryStar`, so a `def` in any of those blocks was
missed by both `list_symbols` and `find_symbol` — silently, which is the worst failure
mode for a discovery tool. The block list is now a named `BLOCK_NODES` tuple (built with
`getattr` so `TryStar` stays optional below Python 3.11) and the walk also follows
`case` bodies.

**Fix 2 — `find_symbol` could not be narrowed to one file.**
`find_symbol("allocate", "domain/model.py")` failed with `Not a directory`, because
`_python_files` always called `list_files`. Narrowing a search to a single file is an
obvious thing for a model to try, and the error gave it no way forward. `_python_files`
now accepts a file path as well as a folder, and a non-Python file gets the same clear
`is not a Python file (.py)` message as `list_symbols`.

Both fixes are covered by three new tests (`test_definitions_in_nested_blocks_are_found`,
`test_find_symbol_can_be_narrowed_to_one_file`,
`test_find_symbol_in_a_non_python_file_is_a_clear_error`), taking
`tests/test_navigation.py` from 56 to 59 and the suite from 208 to 211.

Checked and found already correct, so left alone: non-Python files, directories passed
to `list_symbols`, paths outside the root, `..`, `.git` contents, missing symbols, and
detached HEAD (the demo target is detached at the pinned commit, so `git_state` reports
`(detached HEAD)` with the full SHA — exactly what the task record will need in Phase 4).

Still open from this phase: no linter is installed on the Mac (`ruff`, `flake8`,
`pylint`, `mypy` all absent), so the `ruff` line in the results above is from the
cloud run and cannot be reproduced locally yet. Phase 7 adds the linter to
`requirements.txt` and to the sandbox image.

---

## Phase 4 — Task execution (2026-10-31)

Requirements covered: **T1, T2, T3** and the task-execution half of **S2**.
Tests: **X6** (checks and loop), plus the permission, limit and scope tests.
Built and verified entirely on the developer's Mac.

Numbering note: this phase answers the brief's *third* "What to build" row
(Task execution). The *second* row (Context: repository map, file selection,
bounded history) was **skipped** and is still open — see "Still missing" below.

### What was built

New module `task_record.py`:

| Part | Role |
|---|---|
| `TaskSpec` | The request, project, root, **starting commit**, mode, allowed changes, checks, acceptance commands and limits. `validate()` refuses a specification whose acceptance command sits inside the agent's writable area. |
| `ScopePolicy` | Which files may be changed, as globs relative to the root. `allows(path)` / `refusal(path)`. No patterns = any file in the root, which is the Stage 1 behaviour. |
| `ActionRecord` | One model reply: step, tool, arguments, outcome (`ok`/`denied`/`failed`/`malformed`), detail, and for `run_check` the check's one-line status. |
| `TaskRecord` | Spec + every action + final status and summary. `save()` / `load()` as plain JSON. |
| `CancelToken` | A stop request the controller checks between steps. |

`navigation.py` gained `head_commit()` — the current SHA through the existing
read-only git whitelist — so a task can be pinned to the commit it started from.

### Where each requirement is met

| # | Requirement | Where |
|---|---|---|
| 1 | Save the task specification | `TaskSpec`, built by `spec_from_config()`; printed as the `=== Task Record ===` section; written with `--record FILE` |
| 2 | Record every tool action and its result | `Controller._note()` on every path of `execute()`, plus `done` and malformed replies; printed as `=== Actions ===` |
| 3 | Edit–test–fix loop, failed checks fed back | unchanged Stage 1 loop: the full check report (status + stdout + stderr) is the next user message. Proved by `test_failing_check_is_fed_back_and_the_fix_passes` |
| 4 | Clear stopping conditions | `STOPPING_RULES`: `done`, `action_limit_reached`, `retry_limit_reached`, `cancelled`, `error`. `RunResult.stopping_rule()` prints the reason in words |
| 5 | Scope enforced in the harness | `Controller.check_permission()` consults `ScopePolicy` **before** the write. `demo.py scope` still exists, but it is no longer what protects the files |
| 6 | Reject changes outside permitted paths | same check; the denial names the allowed patterns, and the file is never opened |
| 7 | Acceptance tests outside the writable scope | `TaskSpec.validate()` and `main.parse_config()` both refuse an `--acceptance` path inside `--root` (exit code 2) |
| 8 | An edit needs a later passing check before `done` | unchanged `done_problem()`; `test_done_is_refused_until_a_check_passes` and `test_a_passing_check_before_the_edit_does_not_count` |

### New CLI options

```bash
--allow GLOB        a file or glob the agent may change (repeat; default: anything in --root)
--acceptance PATH   an acceptance check saved with the task; must be OUTSIDE --root
--record FILE       write the task specification and every action to JSON
```

### A real weakness found by running the real model, and fixed

The live runs exposed something the scripted tests had not. `qwen2.5-coder:7b`
could not get `replace_in_file`'s `old` text to match, and asked for the **same
failing action six times**, re-reading the file in between. Every attempt was a
tool error, and tool errors deliberately do not count towards the retry limit,
so nothing stopped it: the run burned all 20 actions making no progress. The
in-a-row counter did not help either, because the `read_file` between attempts
reset it.

Fix: an action that already failed is remembered (`tool` + exact arguments). If
the model asks for it again while the repository has not changed, it is
**denied** instead of re-run, and those denials are counted on their own
(`self.repeats`), so `max_retries` finally bites:

```
[11] replace_in_file  denied: this exact action already failed once and the
                      repository has not changed since ...
[13] replace_in_file  denied: ...
[15] replace_in_file  denied: ...
Stopped: the model asked 3 times for an action that had already failed
(limit 3) and the repository did not change in between. It is not making progress.
```

The memory is cleared whenever a file actually changes, and `run_check` is
exempt, because the same check legitimately gives a different answer. Covered by
`test_repeating_an_identical_failed_action_is_denied`,
`test_a_failed_action_may_be_retried_after_a_file_changes` and
`test_run_check_may_always_be_repeated`.

### A second weakness found, and fixed: edit_file could destroy an unread file

`edit_file` replaces a whole file. Stage 1 only refused that for a file the model
had read **truncated** (`partly_read`); a file it had **never read** was not
protected at all. In a live run the model replaced the real 36-line
`service_layer/services.py` with this, having never read it:

```python
class InvalidQuantity(Exception):
    pass

class InvalidSku(Exception):
    pass

# existing code
```

`# existing code` is a literal placeholder. The real code was gone, and every
later step failed trying to patch a file that no longer contained what the model
expected. This was the root cause of both failed live runs.

Fix: the controller now tracks the files the model has seen in full
(`read_files`) and refuses `edit_file` on an **existing** file that is not among
them:

> Permission denied: you have not read this file in this run, so replacing all of
> it would delete code you have not seen. Call read_file first, then change only
> the lines that need changing with replace_in_file.

What counts as "seen in full", and what is deliberately still allowed:

| Case | Allowed? | Why |
|---|---|---|
| `edit_file` on a file never read | **no** | it would delete code the model has not seen |
| `edit_file` after a complete `read_file` | yes | the model has the content |
| `edit_file` after a **truncated** `read_file` | **no** | the Stage 1 rule, with its own message |
| `edit_file` to **create** a file | yes | there is nothing to destroy |
| `edit_file` again on a file the model itself wrote | yes | writing the whole file means knowing it |
| `edit_file` on a file passed with `--file` (not truncated) | yes | the whole content was sent with the task |
| `replace_in_file` without reading | yes | `old` must already match, so it cannot blind-overwrite |
| `edit_file` after only a `replace_in_file` | **no** | a partial change does not show the whole file |

The prompt was updated too, so the model is told the rule instead of discovering
it by being denied: `edit_file`'s help now says to read an existing file first and
prefer `replace_in_file`, and a new edit rule forbids placeholders such as
`# existing code`.

**Cost, as predicted: 13 tests had to be rewritten**, 3 of them Stage 1
(`test_controller.py`: the three `done`-gate tests) and 10 in
`test_task_execution.py`. In every case the fix was to insert the `read_file`
the model should have been doing anyway, and to shift the message-index
assertions that moved as a result. No test lost its point: they are about the
`done` gate, the task record and the scope, not about overwriting unread files.
Nine new tests cover the rule itself.

### The fix solved the task on the real model

Same command, same model, same tight scope, immediately after the change:

```
[1] find_symbol allocate
[2] read_file service_layer/services.py        <- now required before the rewrite
[3] edit_file service_layer/services.py        <- 975 characters, the WHOLE file
[4] read_file service_layer/services.py
[5] run_check   SUCCEEDED (exit code 0)
[6] done
Run: done (actions 6/20 | executed 5 | malformed 0 | denied 0 | tool errors 0)
Verification: PASS (1/1 checks passed)         Result: SUCCESS
demo.py verify -> acceptance PASS | regression PASS | scope PASS
```

Forced to read the file first, the model wrote the complete 975-character module
instead of a seven-line stub, and produced a minimal correct diff:

```diff
+class InvalidQuantity(Exception):
+    pass
...
+    if qty <= 0:
+        raise InvalidQuantity(f"Invalid quantity {qty}")
```

Two failed runs became one clean pass, from a rule in the harness rather than a
better prompt or a bigger model. The denial the model never had to see is the
reason it succeeded.

### Files

| File | Change |
|---|---|
| `task_record.py` | **new** — `TaskSpec`, `ScopePolicy`, `ActionRecord`, `TaskRecord`, `CancelToken`, `spec_from_config` |
| `controller.py` | takes `spec` and `cancel`; scope check before writes; `_note()` records every action; `CANCELLED` status and `STOPPING_RULES`; repeated-failed-action rule; `read_files` + read-before-overwrite rule |
| `config.py` | `allowed_changes`, `acceptance_commands`, `record_path`; `Allowed:` line in the summary |
| `main.py` | `--allow`, `--acceptance`, `--record`; refuses acceptance inside `--root`; `=== Task Record ===` and `=== Actions ===` sections; `Stopped:` line |
| `navigation.py` | `head_commit()` |
| `tests/test_task_execution.py` | **new** — 46 tests |
| `tests/helpers.py` | `make_harness` takes `task`, `spec`, `cancel` and any Config field |
| `model_client.py` | `edit_file` help and a new edit rule: read an existing file first, never write a placeholder |
| `tests/test_controller.py` | 3 `done`-gate tests now read before editing |

### Results

```
python -m pytest -q tests/test_task_execution.py  ->  46 passed
python -m pytest -q                               ->  257 passed (211 + 46), 0 skipped
demo.py rehearse + verify (Stage 1 demo)          ->  SUCCESS; acceptance / regression / scope PASS
real-model run, tight scope                       ->  SUCCESS in 6/20 actions (see above)
```

Three live runs with `qwen2.5-coder:7b`, scope `service_layer/services.py` only,
in the order they happened — each one showed a different part working:

| Run | Outcome | What it proved |
|---|---|---|
| 1 | `action_limit_reached` at 20 | the task record captures a limit stop; scope denial fired on `domain/model.py` |
| 2 | `retry_limit_reached` at 15 | the repeated-failed-action rule stops a model that is going nowhere |
| 3 | **`done` at 6, SUCCESS** | with the read-before-overwrite rule the model fixed the bug correctly |

Runs 1 and 2 both failed for the same underlying reason — the destroyed file —
and the enforcement the harness added on the strength of them turned run 3 into a
pass. For the record, the denials seen along the way:

```
[7]  replace_in_file  denied: domain/model.py is outside the allowed changes
                      for this task (service_layer/services.py)
[15] replace_in_file  denied: this exact action already failed once ...
Stopped: ... It is not making progress.
```

Every one of those denials is the harness refusing something, recording it, and
stopping with a reason — never a silent failure or a false pass.

### Still missing (brief requirement 2, skipped)

Context management is untouched: there is still no repository map, no automatic
file selection, and **no bound on the conversation history** — only the
per-output `max_output_chars` truncation. A long run still grows the message
list until Ollama's `num_ctx` silently drops the oldest messages, which is
exactly what C3/C4 of the checklist exist to prevent.

### Proving it in IntelliJ

The IntelliJ project had no module, no interpreter and no run configurations, so
nothing could be run from the IDE. Added (all inside the git-ignored `.idea/`,
plus `harness.iml`, now also git-ignored):

| File | Purpose |
|---|---|
| `harness.iml`, `.idea/modules.xml`, `.idea/misc.xml` | a Python module whose content root is the project, with `tests/` as a test source root and `.venv/` excluded |
| `.idea/runConfigurations/All_tests.xml` | pytest over `tests/` |
| `.idea/runConfigurations/Navigation_tests.xml` | pytest over `tests/test_navigation.py` |
| `.idea/runConfigurations/Task_execution_tests.xml` | pytest over `tests/test_task_execution.py` |

Each run configuration pins the interpreter with
`SDK_HOME=$PROJECT_DIR$/.venv/bin/python`, so it runs even before a global SDK is
registered. All files are valid XML and the command they run is the same one
verified above. **IntelliJ must reload the project once** to pick up the new
module and configurations; this could not be triggered from outside the IDE.

---

## Phase 6 — Session storage and safe resume (2026-10-31)

Covers the brief's fourth "What to build" row (Sessions): **S1, S2, S4, S5, S6**,
and the session half of **S3**. Tests: **X3** (session storage) and **X5** (safe
resume). Built and verified on the developer's Mac.

### What was built

New module `session.py`:

| Part | Role |
|---|---|
| `new_session_id()` | A stable id: `20261031-192133-423acb45` — date, time, 8 random hex. Sortable, readable, unique, and it never changes once saved. |
| `Checkpoint` | Everything needed to carry on: step, consumed counters, the conversation, changed files **with a sha256 each**, files read in full, files read truncated, actions that already failed, `repeats`, `rejected_in_a_row`, `checked_since_edit`, the last check's name and status, the repository's branch/commit/dirty list, and `pending`. |
| `Session` | id, `TaskSpec`, status, summary, every `ActionRecord`, the latest `Checkpoint`, timestamps, and `uncertain`. `progress()` prints how far it got. |
| `SessionStore` | `save` / `load` / `reopen` / `list_sessions`, one JSON file per session. |
| `check_resume()` | Compares a saved session with the world as it is now and returns **every** mismatch. |
| `ResumeReport` | `safe`, the mismatches, and the notes; `text()` is what the CLI prints. |

### The checkpoint design

**What a checkpoint is for.** A run used to live only in memory: if the process
died, the work was lost and the repository was left half-changed with nothing to
say what had happened. A checkpoint is the smallest set of facts that lets a new
process pick the task up — or refuse to.

**When one is written.** Twice per step, and that pairing is the whole trick:

```
          ┌─ checkpoint(pending=action)   <- "I am about to run this"
step N ───┤  execute the tool
          └─ checkpoint(pending=None)     <- "it finished, result recorded"
```

If the process is killed anywhere between those two writes, the file on disk
still says `pending`. That is how an interrupted action becomes *detectable*
rather than invisible. A final checkpoint is written when the run stops, carrying
the terminal status.

**How it is written — durable and atomic.** `SessionStore.save()`:

1. writes the whole JSON to `<id>.json.tmp` in the same directory,
2. `flush()` then `os.fsync()` — the bytes are on the disk, not in a buffer,
3. `os.replace(tmp, <id>.json)` — atomic on POSIX **and** Windows.

A reader therefore sees either the previous checkpoint or the new one, never a
half-written file. A crash costs at most the newest step. `list_sessions()`
ignores `.tmp` files, so a leftover temp file from a crash is never offered as a
session. Covered by `test_a_half_written_temporary_file_never_becomes_the_session`.

**What is deliberately *not* saved.** The check's full stdout. Only its name and
status line are kept, because the output can be huge and is already in the
conversation. On resume a stand-in `CommandResult` is rebuilt from the status, so
`done_problem()` still refuses `done` after a check that failed — the safety
property survives even though the text does not.

### Safe resume: what is revalidated

`check_resume()` collects **all** mismatches rather than raising on the first, so
the user is told everything that is wrong at once:

| Check | Pauses when |
|---|---|
| Session finished | the status is `done` |
| Project allowlist | the root is not in `--allowed-project` |
| Same project | the session was saved for a different `--root` |
| Permissions: mode | `--mode` differs from the saved one |
| Permissions: scope | `--allow` patterns differ from the saved ones |
| Git branch | the branch moved |
| Git commit | the commit moved |
| **The agent's own edits** | a file the agent changed no longer has the sha256 it had at the checkpoint, or is missing |
| Other working-copy changes | `git status` shows files the checkpoint did not |
| Action limit | already used up |
| Retry limit | `repeats` already at the limit |

On any mismatch the harness prints `PAUSED:`, lists the reasons, says
"Nothing was executed", and exits **3**. It does not resume.

### Uncertain actions are never replayed

A `pending` action in a reopened session means the process stopped between
starting it and recording its result. Nobody knows whether the file was written.
So `reopen()` moves it out of `pending` into `uncertain`, and:

- it is recorded in the action log with the outcome **`uncertain`**,
- it is reported in `progress()` and in the resume notes,
- the model is told about it in plain words ("started but its result was never
  recorded ... It has NOT been repeated. Check the current state of the files
  yourself"),
- and it is **never re-executed**.

It is a *note*, not a mismatch: the run may continue, it just must not repeat the
action. `test_an_uncertain_action_is_never_replayed` asserts the tool was never
called again and the file is untouched.

### A design error this phase corrected

The first version treated `cancelled`, `error`, `action_limit_reached` and
`retry_limit_reached` as finished, so none of them could be resumed. That is
wrong twice over. The brief's Sessions row asks for "**cancel** an active run,
and **resume** from a safe checkpoint" — that pair is the expected flow. And a
run that died because Ollama was unreachable is the single most useful thing to
resume once Ollama is back.

`FINISHED` is now `("done",)` alone. Everything else stays resumable *in
principle*, and whether it actually may continue is decided by the other checks —
exhausted limits, moved commit, changed permissions — which give a far more
useful message than "already finished". `test_only_done_is_treated_as_finished`
and `test_a_cancelled_run_can_be_resumed` pin this down.

A second gap found while testing: a fatal model error ended the run without
appearing in the action log, although the brief expects a reopened session to
show its **errors**. It is now recorded as `(model error)`.

### New CLI

```bash
# save the run as a session
python main.py --root ... --task ... --mode edit --model qwen2.5-coder:7b \
    --session-dir ~/harness-sessions --allow service_layer/services.py

# see what is saved
python main.py --list-sessions --session-dir ~/harness-sessions

# check whether resuming is safe, execute nothing
python main.py --resume <id> --session-dir ~/harness-sessions --dry-run

# resume
python main.py --resume <id> --session-dir ~/harness-sessions
```

`--allowed-project PATH` (repeatable) restricts which projects the harness may
work on at all; it is checked for a new run and revalidated on resume. Exit
codes: `0` success, `1` the run did not finish cleanly, `2` the session could not
be read, `3` **paused** on a mismatch.

### Files

| File | Change |
|---|---|
| `session.py` | **new** — ids, `Checkpoint`, `Session`, `SessionStore`, `check_resume`, `ResumeReport` |
| `controller.py` | `run()` split into `run` / `resume` / `_loop`; `_checkpoint()` before and after every action; restores consumed limits, read files, failed actions and the check gate; `UNCERTAIN` outcome; records `(model error)` |
| `navigation.py` | `state_summary()` → `(branch, commit, dirty)` from the same read-only git whitelist |
| `config.py` | `session_dir`, `allowed_projects`; `Session:` line in the summary |
| `main.py` | `--session-dir`, `--resume`, `--list-sessions`, `--allowed-project`, `--dry-run`; `report_run()` extracted so a fresh run and a resumed one report identically |
| `tests/test_sessions.py` | **new** — 33 tests |
| `tests/helpers.py` | `make_harness` takes `session` and `store` |
| `.idea/runConfigurations/Session_tests.xml` | **new** — pytest over `tests/test_sessions.py` |

### Tests (`tests/test_sessions.py`, 33 tests)

A "restart" is modelled honestly: the `Session` object is discarded and reloaded
from disk through a brand new `SessionStore`, so nothing can leak through memory.
A "kill" is modelled by making a tool handler raise, so the pre-action checkpoint
is on disk and the post-action one never happens — exactly what SIGKILL leaves.

| Section | What is checked |
|---|---|
| Ids and storage | unique, sortable, path traversal in an id refused, no temp file left behind, a half-written temp file never becomes the session, unreadable sessions skipped not fatal, newest first |
| Reopening after restart | request, steps, history, tool error, fatal error, check result, changed files and consumed limits all come back; progress text |
| Successful safe resume | continues the same task to `done`; step numbers continue; the action budget is **not** renewed; a `done` session is refused |
| Repository mismatch | the agent's own edit modified or deleted; commit moved; branch switched; an unrelated file appeared; a different root; one-byte digest change |
| Permission changes | mode changed; allowed changes widened or narrowed; project no longer in the allowlist; all mismatches reported at once |
| Exhausted limits | action limit used up; retry limit used up; remaining never negative |
| Uncertain actions | saved as `pending`; reopened as `uncertain`; **never replayed**; the model is told; reported as a note not a mismatch; a completed step leaves no `pending` |
| Cancel then resume | a cancelled run reopens and finishes; only `done` counts as finished |

### Results

```
python -m pytest -q tests/test_sessions.py  ->  33 passed
python -m pytest -q                         ->  290 passed (257 + 33), 0 skipped
demo.py rehearse (Stage 1 demo)             ->  SUCCESS, Verification PASS
```

End-to-end across **three separate OS processes**, with a real git repository:

```
process 1  killed mid-edit        -> [1] read_file, [2] edit_file <<< killed >>>
process 2  --list-sessions        -> 20261031-192133-423acb45  running  UNCERTAIN ACTION
process 2  --resume --dry-run     -> Safe to resume; note: step 2 edit_file ... NOT replayed
process 3  --resume               -> [resume] uncertain action not replayed: edit_file
                                     [resume] step 2, 2/20 actions used
                                     [3] read_file [4] replace_in_file [5] run_check
                                     SUCCEEDED [6] done
```

The action log from the resumed run shows the interrupted step for what it was:

```
[1] OK        read_file path='hello.py'
[2] UNCERTAIN edit_file path='hello.py' content='def greet():\n    return "hello"\n'
[3] OK        read_file path='hello.py'
[4] OK        replace_in_file path='hello.py' ...
[5] OK        run_check  -> SUCCEEDED (exit code 0)
[6] OK        done
```

### Known limits

- **Cancellation is still only between steps.** `CancelToken` is checked at the
  top of each iteration, so nothing further starts, but a model request or a
  command already running is not interrupted. The brief's X4 wants one
  representative cancellation *during* a request or a command; that is Phase 5
  and is still open. A command that hangs is already killed with its process
  group by the existing timeout.
- The conversation is saved in full. With no history bound (requirement 2, still
  skipped) a long session file grows with the run.
- `check_resume` compares the dirty list as sets of `git status --short` lines, so
  a file that changed *and* was already dirty at the checkpoint is caught by its
  digest rather than by the list.
- Resume rebuilds the check result from its status line only, so the resumed
  model cannot re-read the old failure output. It is told the status, not the text.

---

## Phase 5 — Cancellation (2026-10-31)

Covers the brief's **S3** and its test **X4**, which asks for one representative
cancellation during a model request *or* a command. Both are covered, plus the
harder requirement the prompt added: cancelling from **another CLI process**.

### Why a file, not a signal

Ctrl+C only reaches the terminal you are sitting in front of. A long run may be
in another terminal, started by a script, or left going while you do something
else. So cancellation goes through a small file next to the session:

```bash
terminal 1:  python main.py --root ... --session-dir ~/sessions ...
terminal 2:  python main.py --cancel <id> --session-dir ~/sessions
```

A file needs no daemon, no port and no privileges, it works when the two
processes share no memory, and it survives either of them dying. Ctrl+C is kept
as a convenience on top (one press asks for a clean stop, a second aborts), but
nothing depends on it — `cancellation.py` contains no `SIGINT` and no
`KeyboardInterrupt`, which `test_cancellation_does_not_depend_on_ctrl_c` asserts.

### New module `cancellation.py`

| Part | Role |
|---|---|
| `CancelToken` | In-process cancellation, for the Ctrl+C handler and tests. |
| `CancelFile` | Cross-process: `request()` writes the flag (atomically, via a temp file plus `os.replace`), `cancelled` polls for it, `reason` reads it back, `clear()` removes it. |
| `AnyCancel` | Cancelled as soon as any source is; reports the reason of the one that fired. A run normally has a token *and* a file. |
| `wait_for()` | A poll loop that raises `CancelledError` — a blocking call cannot be interrupted by a file appearing on disk, so every wait is a poll. |

**Everything latches.** The first time a source reports cancelled it keeps
reporting cancelled, even if the file is deleted. A run must not quietly come
back to life, and a late model reply must not be able to start another action.
`clear()` is only for a *resumed* run, so the flag that stopped it last time does
not stop it again immediately.

### Where cancellation is noticed

| Point | How |
|---|---|
| Between steps | `Controller._loop` checks before asking the model |
| **During a model request** | `ModelClient._chat_cancellable` runs the request in a daemon thread and polls the cancel source while it waits |
| **During a command** | `ExecutionEnvironment._wait` polls every 0.25 s instead of one blocking `communicate`; on cancel it kills the process group and force-removes the Docker container |
| Before a command starts | `run()` refuses to start a process at all if the run is already cancelled |
| At the permission gate | `check_permission` denies every tool once cancelled, so no write can slip through |

### The model request: honest about what is interrupted

A local model cannot be told "never mind" — there is no way to abort a
generation already running in Ollama. So the request runs in a **daemon thread**
and, on cancellation, is *abandoned* rather than waited for:

- the harness stops within `POLL_SECONDS` instead of after a possibly very long
  generation,
- the thread cannot hold the process open, because it is a daemon,
- and its result is dropped on the floor — nothing ever reads it.

That last point is the requirement "prevent delayed model responses from
triggering new actions", and it is what
`test_a_late_model_reply_cannot_start_an_action` proves: the blocked reply is an
`edit_file`, the run is cancelled, the reply is then released, and the file is
still untouched. The socket itself is left to its own timeout in the abandoned
thread; it can affect nothing.

### Two real bugs this phase found and fixed

**A command could start after cancellation.** `ExecutionEnvironment.run()` only
checked the cancel source *while waiting*, so a command whose process started
before the first poll ran to completion — and a fast command could finish and be
reported as a clean pass on an already-cancelled run. It now refuses to start the
process at all when the run is cancelled, and a cancelled `CommandResult` never
counts as `succeeded`.

**Stopping cleared a pending action.** `_stop()` wrote a final checkpoint with
`pending=None`, which would turn a genuinely unknown outcome into an apparently
clean one and defeat the Phase 6 uncertainty mechanism. It now passes
`keep_pending=True`: stopping never invents certainty.
(`test_stopping_does_not_clear_a_pending_action`.)

A third, smaller fix: the controller's run state (`counters`, `record`, ...) only
existed after `run()`, so calling `execute()` directly crashed in `_deny`. It is
now initialised in `__init__`.

### Cancelled is not the same as uncertain

Worth stating plainly, because the first draft of these tests got it wrong. A
*cooperative* cancellation always lets the current action return, so its outcome
is **known**: the command was killed, the kill was observed, and `CANCELLED` is
written down. That must not be dressed up as an unknown outcome.

Real uncertainty needs a **hard kill**, where nobody gets to record anything —
that is the `pending` mechanism from Phase 6, tested in `test_sessions.py`. The
two are deliberately different, and both are now tested for what they actually
are.

### Files

| File | Change |
|---|---|
| `cancellation.py` | **new** — `CancelToken`, `CancelFile`, `AnyCancel`, `wait_for`, `CancelledError` |
| `model_client.py` | `request_action(messages, cancel=None)`; `_chat_cancellable` worker thread; `Blocked` scripted reply for deterministic tests |
| `execution.py` | `run(name, cancel=None)`; `_wait` poll loop; refuses to start when cancelled; `CommandResult.cancelled`, excluded from `succeeded`, shown in `status()` |
| `controller.py` | passes the cancel source to the model and to `run_check`; `CancelledError` → `CANCELLED`; denies every tool once cancelled; run state initialised in `__init__`; `_stop(keep_pending=True)` |
| `task_record.py` | `CancelToken` moved to `cancellation.py` and re-exported |
| `main.py` | `--cancel ID --session-dir DIR [--reason ...]`; `build_cancel()` combining the token and the file plus the SIGINT handler; a resumed run clears the stale flag |
| `tests/test_cancellation.py` | **new** — 25 tests |
| `tests/helpers.py`, `tests/test_controller.py` | test doubles updated for the new signatures |
| `.idea/runConfigurations/Cancellation_tests.xml` | **new** |

### Tests (`tests/test_cancellation.py`, 25 tests)

Deterministic, with no live model and no timing guesswork: a model request is
blocked with `model_client.Blocked` (which waits exactly where a real request
would), a command is a real subprocess that sleeps *and spawns a child*, and
Docker is a `FakeSandbox` recording its `stop()` calls.

| Section | What is checked |
|---|---|
| Cancel sources | token latches and keeps the first reason; a file written by one object is seen by another; requesting twice is safe and leaves no temp file; a noticed cancellation cannot be undone; `clear()` lets a fresh reader start again; `AnyCancel` reports the source that fired; `wait_for` timeout vs cancel |
| Blocked model request | a blocked request is cancelled and touches nothing; **a late reply cannot start an action**; cancelling first asks the model nothing |
| Running command | the command **and its child** are killed (verified with `pgrep`), well before the timeout; a cancelled command is never a passing check; the plain timeout still works without a cancel source |
| Docker | the container is force-removed on cancel; a repeated removal is safe |
| No further writes | nothing is edited after cancellation; an edit reaching the gate afterwards is denied |
| Persisted state | the cancelled status, summary, actions and counters survive into a fresh store; a mid-command cancellation records `CANCELLED`; `_stop` keeps a pending action |
| Repeated cancellation | five cancellations and a second `run()` are both safe |
| **Another process** | a real second OS process (`main.py --cancel`) stops a real first one; the queued edit never happens; the child process is gone; the state is saved |
| Not Ctrl+C | `cancellation.py` contains no `SIGINT`/`KeyboardInterrupt` |

### Results

```
python -m pytest -q tests/test_cancellation.py  ->  25 passed
python -m pytest -q                             ->  315 passed (290 + 25), 0 skipped
demo.py rehearse + verify (Stage 1 demo)        ->  SUCCESS; acceptance / regression / scope PASS
```

Verified by hand with two real terminals, a run whose check hangs and spawns a
child, and a third `sleep` for good measure:

```
process 1   [1] read_file hello.py
            [2] run_check            <- blocked, 3 processes alive
process 2   python main.py --cancel 20261031-193556-3d797d32 --session-dir ...
            Cancellation requested: cancelled from another terminal
process 1       CANCELLED after 0.8s
            Stopped: cancelled from another terminal. No further actions were executed.
```

Afterwards: `pgrep sleep 600` → **0** (all three gone), `hello.py` unchanged
(the `edit_file` that was next in the script never ran), and the session saved
with status `cancelled`.

### Known limits

- The Ollama HTTP request is abandoned, not aborted at the socket. Ollama keeps
  generating until it finishes and the daemon thread then discards the answer.
  Cancellation is immediate from the harness's side and nothing can act on a
  late reply, but the local model does keep burning CPU for a while.
- Cancellation is checked between steps and inside the two waits, so the
  granularity is `POLL_SECONDS` (0.25 s) plus however long one already-running
  tool call takes to return. A file write is not interrupted half way — which is
  deliberate, since a half-written file is worse than a finished one.
- `--cancel` needs `--session-dir`: a run started without one has no id and
  nothing to address, so only Ctrl+C can stop it.

---

## Phase 8 — Command-line interface (2026-10-31)

Requirement 6 / **U1–U4** implemented. Scope: CLI and project selection; other
planned phases remain open.

### Changes

- `main.py` is a thin executable and Stage 1 import/injection compatibility layer.
  `cli.py` owns parsing and terminal presentation; `application.py` owns task
  lifecycle services and delegates execution to the existing controller.
- `start --project NAME` selects an exact canonical path from `[projects]` in
  `~/.harness/projects.toml` (override with `--projects-file`). Configuration fails
  closed; rejects missing/empty/malformed files, unknown fields, invalid values,
  duplicates, dangerous roots and control files inside approved repositories.
  New starts require a Git repository with a starting commit.
- Sessions save by default in `~/.harness/sessions` (override `--session-dir`). The
  session ID is printed before the model request, and the initial conversation
  checkpoint is saved so a first-request interruption can be resumed.
- `status` displays saved progress; `show` displays action and conversation history,
  errors, results, full final check results and the persisted diff. These commands
  need neither a model nor the original repository. `show --live-diff` reads the
  current approved working copy without running tests. `projects` and `list` aid
  discovery.
- `cancel` works from another process and remains idempotent. Final verification
  now receives cancellation too, preventing checks from restarting after a stop.
- `resume` rechecks current approval, permissions, branch/commit, saved changes and
  limits. It pauses for uncertain actions or unavailable Git state. POSIX process
  locks prevent simultaneous CLI runners for a session. Dry runs leave the saved
  checkpoint/cancel flag unchanged. Model/sandbox settings are restored.
- Sessions accept older files without final-report fields and label unavailable
  evidence explicitly. Malformed session shapes, invalid IDs and ID/filename
  mismatches produce useful errors. SIGINT handlers are restored for embedded callers.
- Stage 1 direct-root launch, `parse_config` imports, demo model/sandbox injection,
  and legacy session flags remain supported. Direct-root compatibility launches
  apply configured approvals when available and retain prior behavior without a
  configuration; new subcommands require approval. Approved tasks cannot bypass
  revalidation through legacy resume flags.
- README includes configuration and launch examples, architecture, checkpoint
  design, platform/compatibility limits, and exit codes 0/1/2/3/130. No new dependencies.

### Validation

- Baseline before changes: **315 passed**.
- Added **84 CLI tests** in `tests/test_cli_commands.py`.
- Full suite: `.venv/bin/python -m pytest -q` → **399 passed, 0 skipped**
  (includes all Stage 1 tests and real Docker checks).
- `git diff --check` and top-level / `start --help` checks passed.
- CLI tests include every command, configuration and permission errors, failure
  reporting, restart-safe inspection, revoked approval, checkpoint mismatches,
  uncertain actions, budgets, active-run locks, Stage 1 flags, actual process exit
  codes, and a real-process cancel/resume cycle during the first model request.

Known limits: process locks use POSIX `fcntl`; the existing working-copy safety
check hashes harness-written files and compares Git status for other changes,
not the bytes of every untouched file. No context compression or lint/build work
was added in this phase. `--offline` replaces the model only; checks still use the
configured sandbox.
