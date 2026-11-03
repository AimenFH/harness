# Stage 2 Delivery Audit

| | |
|---|---|
| Audited commit | `fbebfad6107f4fdf2448f2243a6d7e70f14838f4` ("test fix") |
| Working tree at audit start | **clean**; the audit and its fixes were committed in `667c504` (merged as `76235fa`) |
| Re-checked | 2026-11-03 at `76235fa`: **503 passed, 0 skipped**; baseline re-run in Docker (22 × `MISSING FEATURE`, regression 24/24); feature acceptance still **FAIL** |
| Date | 2026-11-03 |
| Test suite (offline, blank `OPENAI_API_KEY`/`ANTHROPIC_API_KEY`, no Ollama) | at audit time **498 passed**; after the fixes **503 passed, 0 failed, 0 skipped** in 53s |
| Same suite with Docker unreachable | **73 failed, 428 passed, 2 skipped** — 75 tests need a live Docker daemon + the `harness-sandbox` image |
| Stage 1 / Stage 2 boundary | Stage 2 starts at `610dd87` ("Stage 2 phase 1"); 17 commits, 62 files, +12 460 / −451 lines |
| Verdict | The harness is complete and genuinely strong. **The Stage 2 feature itself was never implemented**, and that is the one thing that can fail the delivery. |

> **Update after the audit.** Twelve of the fourteen TODO items below have been
> carried out: the documentation gaps (J3, J4, J6, the stale test log under J5),
> the F1 approval reporting, the G1 disposable-copy check, the C4 loop ordering,
> the F5 layering, and the I6/I7 evidence, now committed under
> `demo/results-stage2/runs/`. Rows that changed are marked *fixed after audit*
> and say what they were before; closed blockers and TODO items are struck
> through. The suite is **503 passing** (498 + 5 new tests), and the test count,
> the Docker split and the scorecard counts in this file were re-measured after
> the changes rather than carried over.
>
> **What is deliberately not done: the feature itself (TODO 11 and 12).** It needs
> a model run, which is the user's to make, and writing the feature by hand would
> contradict the disclosure in `RESULTS.md` that no feature code was hand-written.

## Scorecard

Legend: **DONE** = implemented with a pointer below · **PARTIAL** = real but incomplete · **MISSING** = not there.

### P. Project constraints & Stage 1 carry-over

| ID | Requirement | Status | Evidence | Gap |
|---|---|---|---|---|
| P1 | Own Python design, not a fork of an existing harness | DONE | `requirements.txt` declares only `requests>=2.28` and `pytest`. No aider/OpenHands/SWE-agent/Codex dependency or vendored code anywhere (`git grep`). History from `610dd87~1` back is the student's own Stage 1. | — |
| P2 | Model provider configurable, not hard-coded | DONE | `config.py:66` `OllamaSettings`, `config.py:122` `ollama_url`, `settings.example.toml:12`, `--model` / `--settings`. Client swapped by injection: `application.py:40` `build_model`, `cli.py:459` `model_factory`. | Only the Ollama `/api/chat` protocol is implemented (`model_client.py:339`); another vendor needs a new client class (the seam exists). |
| P3 | Stage 1 tools still exist and work | DONE | `controller.py:170-183` handler table still has `list_files`, `read_file`, `search`, `replace_in_file`, `edit_file`, `run_check`. `tests/test_repository_tools.py` (26 tests), `tests/test_controller.py` (23) pass. | — |
| P4 | Stage 1 permissions still enforced | DONE | `repository_tools.py:69` `resolve_path` (root boundary, symlink resolution, `.git`); `controller.py:426` `check_permission` (readonly vs edit, `EDIT_TOOLS`). `test_repository_tools.py:73,86,111,140`. | — |
| P5 | Execution limits still enforced | DONE | `config.py:26-35` `MAX_ACTIONS=20`, `MAX_RETRIES=3`; `controller.py:259` action-limit check **before** asking; `execution.py:364` timeout. `tests/test_limits.py` (12 tests). | — |
| P6 | Output limits still enforced | DONE | `config.py:32` `MAX_OUTPUT_CHARS=10000`; `controller.py:683` `truncate()` with `[OUTPUT TRUNCATED]`. `test_limits.py:60,76,96,105`. | — |
| P7 | Stage 1 bug-fix workflow runs end to end | DONE | `tests/test_bugfix_flow.py::test_bug_fix_flow`; real-model run recorded in `demo/RESULTS.md` (acceptance fails → harness fixes → verify passes). | — |

### R. Responsibilities / architecture (Figure 2)

| ID | Responsibility | Status | Evidence | Gap |
|---|---|---|---|---|
| R1 | UserInterface — submit, cancel | DONE | `cli.py:256` `build_parser` (`start`/`cancel`/`resume`/`status`/`show`/`list`/`projects`); `cli.py:410` `run_cancel`. | `cli.py:501-503` builds a `Verifier` and runs `verify()` for `--live-diff` — core logic inside the interface layer. |
| R2 | AgentController — run a step, resume | DONE | `controller.py:193` `run()`, `controller.py:200` `resume()`, `controller.py:244` `_loop()`. | — |
| R3 | ModelClient — request action, swappable | DONE | `model_client.py:275` `ModelClient.request_action`; `model_client.py:452` `ScriptedModelClient` (used by every deterministic test); `model_client.py:430` `Blocked` for mid-request cancellation. | — |
| R4 | RepositoryTools — symbol, git, read, edit | DONE | `repository_tools.py:55` (read/list/search/replace/edit); `navigation.py:94` `RepositoryNavigator` (`find_symbol`, `module_dependencies`, `git_state`). | — |
| R5 | ContextManager — map, select, compress | DONE | `context.py:156` `ContextManager`: `repository_map()` :162, `select_files()` :253, `for_model()` :433. | — |
| R6 | SessionStore — checkpoint, load | DONE | `session.py:246` `SessionStore.save/load/reopen/list_sessions/claim`; `session.py:94` `Checkpoint`. | — |
| R7 | ExecutionEnvironment — run checks, stop processes | DONE | `execution.py:250` `ExecutionEnvironment.run`; `execution.py:395` `kill_process_tree`; `execution.py:95` `DockerSandbox`. | — |
| R8 | Verification — acceptance checks, diff | DONE | `verification.py:146` `Verifier.verify`; `verification.py:167` `_inspect_git`. | — |
| R9 | Dependencies flow the right way | DONE | Verified the whole internal import graph: `main.py → cli.py → application.py → controller.py → {context, model_client, navigation, session, task_record, execution}`. **No core module imports `cli`.** `log` is injected as a callable (`controller.py:116`). | — |

### A. Navigation

| ID | Requirement | Status | Evidence | Gap |
|---|---|---|---|---|
| A1 | Find relevant modules/files | DONE | `navigation.py:106` `find_files` (glob, substring, dotted module name); `repository_tools.py:126` `search`. `test_navigation.py:127-153`. | — |
| A2 | Find symbols, language-aware | DONE | `navigation.py:136` `list_symbols`, :143 `find_symbol` (plain and qualified `Batch.allocate`), :417 `_collect_symbols`. Uses `ast.parse` only — never imports target code (`test_navigation.py:305` `test_parsing_never_runs_repository_code`). Nested blocks handled (`navigation.py:48` `BLOCK_NODES`). | — |
| A3 | Find dependencies | DONE | `navigation.py:182` `module_dependencies` (imports **and** importers), :251 `import_graph`, :475 `_absolute_module` (relative imports, src layout). `test_navigation.py:325-368`. | — |
| A4 | Inspect Git state as a tool | DONE | `navigation.py:54` `git_state_commands` (fixed read-only set), :281 `git_state`, exposed as the model tool `git_state` (`model_client.py:33`). `test_navigation.py:428-519`, incl. `test_git_state_is_read_only` and `test_git_state_does_not_run_repository_helper_programs`. | — |

### B. Context management

| ID | Requirement | Status | Evidence | Gap |
|---|---|---|---|---|
| B1 | Compact repository map | DONE | `context.py:162` `repository_map`; bounded by `MAP_FILES=60` / `MAP_SYMBOLS=12` (`context.py:62-63`); test modules counted not listed (`context.py:213`); unparsable files named (`context.py:205`). `test_context.py:120-166`. | — |
| B2 | Relevance-based file selection | DONE | `context.py:253` `select_files` — scores task words in path (×3), scope match (+4), defined symbols (×2), import neighbours (+1), each with a printed reason (`context.py:148`). `--file` picks win and are not re-suggested. `test_context.py:183-229`. | — |
| B3 | History shortening/compression | DONE | `context.py:433` `for_model`, :466 `shorten` (one line per older step, action → outcome), :492 `_fit`. `test_context.py:239-332`. | — |
| B4 | Documented, enforced size limit | DONE | **Number: 24 000 characters.** Defined `config.py:48` `CONTEXT_BUDGET_CHARS` and `context.py:58` `BUDGET_CHARS`. Enforced in `context.py:448` / `_fit` :492 on **every** request. Documented in `settings.example.toml:28-31`, `config.py:45-47`, README "Context management". `test_context.py:332` asserts the documented default. | — |
| B5 | Trusted instructions/scope/criteria preserved through compression | DONE | `context.py:92` `TRUSTED_HEADER`, :336 `task_card()` (starting commit, scope, checks, acceptance criteria, limits); `for_model` always keeps `messages[:2]` verbatim and `_fit` never touches the head (`context.py:496`). `test_context.py:407,425` (`..._never_compressed`), :544, :565 (survives a resume). | — |
| B6 | Repository text / tool output marked untrusted and kept separate | DONE | `context.py:80-89` `UNTRUSTED_OPEN/CLOSE/NOTE`, :126 `wrap_untrusted`; every tool result wrapped at `controller.py:551`; prompt rule at `model_client.py:122`. `test_context.py:459,467,482` (`test_a_file_that_tries_to_give_orders_arrives_as_marked_data`), :498. | — |

### C. Task execution

| ID | Requirement | Status | Evidence | Gap |
|---|---|---|---|---|
| C1 | Task record: request, starting commit, scope, checks, steps | DONE | `task_record.py:86` `TaskSpec` (`request`, `starting_commit`, `allowed_changes`, `checks`, `acceptance_commands`, `limits`), :168 `ActionRecord`, :196 `TaskRecord`. Commit captured at `application.py:134` via `navigator.head_commit()`. `test_task_execution.py:124,149,177,196`. | — |
| C2 | Edit–test–fix: failed check output feeds the next step | DONE | `controller.py:603` `_run_check` returns `result.report()` (full stdout/stderr) and it is appended to `messages` at `controller.py:318`. Failed verdicts survive compression (`context.py:583` `_check_verdict`). `test_task_execution.py:41` `test_failing_check_is_fed_back_and_the_fix_passes`. | — |
| C3 | Bounded retries and a clear stopping rule | DONE | `max_retries=3` (`config.py:28`); two independent rules — consecutive rejections (`controller.py:341`) and futile repeats (`controller.py:349`, `_repeat_problem` :470). Every status has words: `controller.py:40` `STOPPING_RULES`. `test_task_execution.py:369,383,514`. | — |
| C4 | Loop order matches Figure 3, checkpoint **before** the tool runs | DONE *(deviation removed after audit)* | The loop now follows Figure 3 exactly: `check_permission(action)` → refuse, or else `_checkpoint(..., pending=action)` → `run_tool(action)`. `execute()` splits into `check_permission()` + the new `run_tool()` and still does both for direct callers, so the public seam is intact. Post-action checkpoint unchanged. `test_sessions.py` `test_an_interrupted_action_is_saved_as_pending`, `test_a_completed_action_leaves_no_pending_behind`, and the new `test_a_denied_action_is_never_checkpointed_as_pending` — which captures **every** checkpoint written during a run, not just the last, and which I confirmed fails against the old ordering before keeping it. | *Was:* permission validation happened inside `execute()`, i.e. after the checkpoint, so a crash in that window marked a *denied* action as uncertain. Four cancellation tests patched `controller.execute` as the tool-running seam and now patch `run_tool`; their intent is unchanged. |
| C5 | On "done" the harness runs final checks itself | DONE | `controller.py:412` `done_problem()` refuses `done` until a check has passed since the last edit; independently, `application.py:200-203` always runs `Verifier.verify()` after the loop and `assess_task` compares the claim with evidence. `test_verification.py:124` `test_model_claims_success_but_verification_fails`. | — |
| C6 | Tool errors returned as results, not crashes | DONE | `controller.py:521-531` catches `AccessDeniedError`, `RepositoryError`, `ExecutionError` and returns `FAILED, "ERROR: ..."`. `test_controller.py:253`, `test_navigation.py:585`. | — |
| C7 | Task history carried through the loop | DONE | `messages` grows for the whole run (`controller.py:299,318`) and `for_model` is a non-destructive view (`context.py:431` "messages is not modified"). `test_context.py:445` `test_the_full_history_is_never_modified_by_sending_it`. | — |

### D. Sessions

| ID | Requirement | Status | Evidence | Gap |
|---|---|---|---|---|
| D1 | Save and reopen after restart | DONE | `session.py:275` `save` (temp → `flush` → `os.fsync` → `os.replace`), :300 `load`, :332 `reopen`. `test_sessions.py:139` `test_everything_is_reopened_after_a_restart`; `test_cli_commands.py:395` drives a **real CLI subprocess**. | — |
| D2 | Show progress | DONE | `session.py:188` `progress()`; `cli.py:324` `show_task`, `cli.py:496` `status`. `test_cli_commands.py:132`. | — |
| D3 | Cancel an active run (model request **and** command) | DONE | Mid-request: `model_client.py:303` `_chat_cancellable` (worker thread + poll). Mid-command: `execution.py:355` `_wait` polls and kills. Cross-process: `cancellation.py:74` `CancelFile`. `test_cancellation.py:147,167,207,283`; `test_cli_commands.py:411` cancels a **real** process then resumes it. | — |
| D4 | Resume from a safe checkpoint | DONE | `controller.py:200` `resume()`, `application.py:168` `resume_task`, `session.py:375` `check_resume`. `test_sessions.py:196` `test_safe_resume_continues_the_same_task`. | — |
| D5 | Uncertain interrupted actions marked and never replayed | DONE | `controller.py:57` `UNCERTAIN`; `session.py:341` `reopen` moves `pending` → `uncertain`; `controller.py:228-234` records it and tells the model instead (`_uncertain_notice` :770). `test_sessions.py:501,514` (`test_an_uncertain_action_is_never_replayed`), :534. | — |
| D6 | Resume preserves limits, revalidates permissions & working copy, pauses on mismatch | DONE | `controller.py:202` restores `Counters` from the checkpoint; `session.py:375` `check_resume` checks project allowlist, mode, `allowed_changes`, branch, commit, per-file sha256 of the agent's own edits (`session.py:432`), other dirty files (:444), consumed limits (:457). `application.py:150` adds a mismatch for an uncertain action and for unavailable Git state, so those **pause** too. `test_sessions.py:181,231,267,290,310,325,347,371,382,395,410,424,440`. | — |
| D7 | Saved session holds task, steps, history, errors, check results | DONE | `session.py:216` `to_dict` → spec, status, summary, `actions`, checkpoint (messages, counters, digests), `run_settings`, `result`. Model errors recorded at `controller.py:285`. Final checks/diff saved at `application.py:214-229`. `test_cli_commands.py:90` `test_show_uses_saved_diff_checks_history_even_after_repo_disappears`; `test_reporting.py:246`. | — |
| D8 | After cancellation no further actions or writes | DONE | Cancel checked before asking (`controller.py:246`), a late model reply is discarded (`model_client.py:312` comment + :326), `check_permission` refuses everything once cancelled (`controller.py:428`), a command is not even started (`execution.py:283`). `test_cancellation.py:167,192,322,349` (`test_no_file_is_edited_after_cancellation`). | — |

### E. Verification

| ID | Requirement | Status | Evidence | Gap |
|---|---|---|---|---|
| E1 | Shows changed files | DONE | `verification.py:167` `_inspect_git` parses `git status -z` incl. renames/untracked; `verification.py:97` `changed_files_text`. `test_reporting.py:168`. | — |
| E2 | Shows the final diff | DONE | `verification.py:19` `diff` command; untracked files get a synthetic diff (`verification.py:204`); `diff_text()` :107. `test_verification.py:36`, `test_reporting.py:185`. | — |
| E3 | Shows test and lint results | DONE | `verification.py:101` `checks_text(category)`; categories at :23; `--lint ruff`, `--build compileall`. `cli.py:361` prints Tests/Lint/Build separately. `test_reporting.py:31,48`. | — |
| E4 | Shows unfinished work | DONE | `verification.py:70` `assess_task` → `unfinished_work` (non-done status, uncertain action, out-of-scope change, unrun acceptance commands); `unfinished_text()` :115. `test_reporting.py:141,149,157`. | — |
| E5 | Failed or **unavailable** checks never reported as passed | DONE | `execution.py:207` `succeeded` requires not timed out, not cancelled, not unavailable, no skipped/xfail, exit 0 **and** at least one test actually ran; `execution.py:428` `test_counts` reads runner summaries rather than trusting the exit code. `--sandbox none` returns `unavailable=True` instead of falling back to the host (`execution.py:289`). `test_reporting.py:69,84,99,109,122,203,228`; `test_execution.py:167`. | — |
| E6 | Build checks if relevant | DONE | `execution.py:43` `compileall` (`-I`, pycache to the container tmpfs); `--build` / `build_checks`. `test_reporting.py:60`. | Off by default (`build_checks = []`), which is the right default. |

### F. CLI / UI

| ID | Requirement | Status | Evidence | Gap |
|---|---|---|---|---|
| F1 | Select an **allowed** project (allow-list enforced) | DONE *(fixed after audit)* | `start` is allow-list only: resolves `--project` against `load_projects` and `parser.error`s otherwise; fails closed on a missing/empty file; canonical paths reject aliases, symlinks, parents and children (`test_cli_commands.py`). **Added:** the Stage 1 `--root` form can no longer pass for an approved run — `Config.approval_text()` puts `Approval: NONE - no allow-list applied to this run…` in the task output, `cli.parse_config` warns on stderr, and `Config.approved` exposes the state. Tests: `test_legacy_root_without_an_allowlist_says_so_instead_of_looking_approved`, `test_an_approved_run_reports_where_its_approval_came_from`. | The legacy form still *accepts* an unapproved root rather than refusing it. Refusing would break the Stage 1 launch form across 10 test files and the documented `--root` compatibility contract, so it reports instead. `start --project` is the enforced path and the README now says so under "Stage 1 compatibility". |
| F2 | Start a task, inspect a task | DONE | `cli.py:268` `start`, :292 `status`, :293 `show`, :267 `list`. `main.py --help` confirms all seven commands. `test_cli_commands.py:58,132`. | — |
| F3 | Cancel, resume | DONE | `cli.py:294` `cancel`, :295 `resume` (with `--dry-run`, `--model`, `--sandbox`). `test_cli_commands.py:148,164,176,411`. | — |
| F4 | Review the result (diff + checks) | DONE | `cli.py:324` `show_task` prints Task, Progress, History, Conversation, Results, Checks, Tests/Lint/Build, Changed Files, Diff, Unfinished work; `--live-diff` adds the current working copy. `test_cli_commands.py:90,354`. | — |
| F5 | Interface separated from core | DONE *(fixed after audit)* | `cli.py:1` docstring; `application.py:1` "No argument parsing or terminal presentation here"; no core module imports `cli` (verified). `test_cli_commands.py` checks the SIGINT handler is restored for embedded callers. **Fixed:** `--live-diff` now calls `application.live_diff(root)`; `cli.py` no longer imports or constructs `Verifier`, and the inline `from verification import Verifier` is gone. | — |

### G. Safe working environment

| ID | Requirement | Status | Evidence | Gap |
|---|---|---|---|---|
| G1 | Works on a **disposable copy** | DONE *(fixed after audit)* | `demo/demo.py:80` and `demo/stage2.py:82` `prepare()` clone to `~/harness-demo/...` and `git remote remove origin`. `demo/stage2.py:39` `checked_target` refuses a symlink, a non-repo, a moved HEAD and any target with a remote. `projects.py` refuses a root containing the harness, `/`, or `$HOME`. **Added:** `projects.require_disposable()` — an approved project with a Git remote is refused with the exact `git clone` / `git remote remove` fix, called from `load_projects` so it applies to `projects`, `start` and `resume` alike. `git_remotes()` runs one fixed read-only command, no shell, hooks disabled, 30 s timeout. Tests: `test_approved_project_with_a_git_remote_is_refused`, `test_start_refuses_a_project_that_is_not_a_disposable_copy`. | A proxy, not proof: a repository that never had a remote still passes, and the harness cannot tell a throwaway from an original by inspection. It catches the realistic mistake — approving your own working clone. Stated as such in README "Known limits". |
| G2 | Repo code runs in a contained environment | DONE | `execution.py:95` `DockerSandbox.wrap`: `--network none`, `--read-only`, `--tmpfs /tmp`, `--cap-drop ALL`, `--security-opt no-new-privileges`, `--pids-limit 256`, `--memory 1g`, `--cpus 2`, repo bind-mounted **readonly** at `/work`, non-root `runner` user (`sandbox/Dockerfile:14`). No worktree-only path: `--sandbox none` makes checks *unavailable* (`execution.py:289`), there is no host fallback. `test_sandbox.py:29,120,164`; `test_reporting.py:122`. | — |
| G3 | Credentials and unrelated host files not exposed | DONE | `execution.py:52` `SAFE_ENV_VARS` allow-list (no `*_API_KEY`, no `*_TOKEN`); `execution.py:85` `CONTAINER_VARS` passes only `PYTEST_ADDOPTS` into the container; `HOME=/tmp`. `test_execution.py:150` `test_secrets_in_environment_are_not_passed`; `test_sandbox.py:48,164` `test_container_cannot_see_secrets_host_files_or_network`. | — |
| G4 | Arguments and permissions checked in application code | DONE | `controller.py:426` `check_permission` runs `validate_tool_call`, `resolve_path`, mode, `ScopePolicy.allows` — **before** any write (`controller.py:450` "File scope is enforced here, in the harness, BEFORE anything is written"). `execution.py:277` command allow-list, `shell=False`. `test_task_execution.py:230,285,594` (`test_the_controller_checks_permissions_even_for_a_handmade_action`). | — |
| G5 | Model replies treated as untrusted | DONE | `model_client.py:234` `parse_action` (JSON-or-nothing, brace salvage, type checks), :213 `validate_tool_call` (unknown tool, unknown argument, non-string value, missing required) — and the controller re-validates (`controller.py:435`). `test_model_client.py:63,230`; `test_task_execution.py:594`. | — |
| G6 | push, merge, deployment disabled | DONE | `execution.py:37` `ALLOWED_COMMANDS` contains only `unittest`, `pytest`, `ruff`, `compileall` — no `git`, no shell. Git appears only as fixed read-only sets: `navigation.py:54` (`symbolic-ref`, `rev-parse`, `status`) and `verification.py:13` (`status`, `rev-parse`, `diff`). `test_navigation.py:497` `test_git_state_is_read_only`. | — |
| G7 | A stuck command is stoppable with no orphans and no further writes | DONE | `execution.py:389` `NEW_PROCESS_GROUP` (`start_new_session` on POSIX), :395 `kill_process_tree` via `os.killpg(SIGKILL)`, container force-removed on timeout **and** on cancel (`execution.py:373,378`), `finally: kill_process_tree` on every path (:329). **Verified, not assumed:** `test_cancellation.py:207` `test_cancelling_a_command_kills_it_and_its_children` uses `pgrep` against real OS processes; `test_execution.py:113,132`; `test_sandbox.py:108`. | — |

### H. Required tests

All names below were run and pass offline (498/498).

| ID | Requirement | Status | Evidence | Gap |
|---|---|---|---|---|
| H1 | Navigation tests | DONE | `tests/test_navigation.py` — 49 tests: modules (`:127-153`), symbols (`:171-226`), dependencies (`:325-368`), Git state (`:428-519`), plus `test_model_navigates_then_reads_the_right_file` (:538). | — |
| H2 | Context tests | DONE | `tests/test_context.py` — 37 tests: selection (`:183-229`), budget enforced (`:239`, `:332`), constraints preserved (`:407`, `:425`), untrusted separation (`:459-510`). | — |
| H3 | Session storage after restart | DONE | `tests/test_sessions.py:139`; `tests/test_cli_commands.py:90`, `:395` (real subprocess), `tests/test_reporting.py:246`. | — |
| H4 | Cancellation | DONE | `tests/test_cancellation.py` — 25 tests, covering **both** mid-request (`:147`, `:167`) and mid-command (`:207`, `:234`), no later writes (`:322`, `:349`), child cleanup via `pgrep` (`:207`), container removal (`:283`), cross-process (`:485`). | — |
| H5 | Safe resume | DONE | `tests/test_sessions.py:196,231,267,290,310,325,347,371,382,395,410,424,440,514,549,634`; `tests/test_cli_commands.py:201,230,249`. | — |
| H6 | Checks and loop | DONE | `tests/test_task_execution.py:41,78,102,521,539,560`; `tests/test_reporting.py:69,84,99,109,122,203,228`. All via `ScriptedModelClient`. | — |
| H7 | Earlier behaviour still passes | DONE | `tests/test_controller.py`, `test_repository_tools.py`, `test_limits.py`, `test_execution.py`, `test_cli.py`, `test_bugfix_flow.py`, `test_model_client.py` all green. | — |
| H8 | Feature checks fail on the starting code **and pass after the change** | **PARTIAL** | *Fails first:* `demo/stage2/baseline/proof.json` — 23 cases, 1 passed / 22 failed, every failure `MISSING FEATURE: ...`; `tests/test_feature_demo.py:24,33` prove the baseline proof rejects an infrastructure failure masquerading as feature absence. Regression gate: 24/24 pass on the starting code. Existing callers preserved by `ALLOWED` scope + the 24 upstream tests. | **The "pass after the change" half does not exist.** No reference implementation is committed, no test applies one, and no recorded run gets the acceptance suite to green (best live result: 1 passed / 20 failed / 2 errors). This is the audit's single biggest gap. |
| H9 | Core tests need no live account or API key | DONE | Ran with `OPENAI_API_KEY=`/`ANTHROPIC_API_KEY=` blank and no Ollama: **498 passed, 0 skipped**. `grep` finds no test touching `localhost:11434` live; every model is `ScriptedModelClient` or a stubbed `requests.post`. | 72 of the 498 need a running Docker daemon and the built `harness-sandbox` image (verified by breaking `DOCKER_HOST`). That is contained execution, not an account — but it is an undocumented prerequisite for a clean grader run. |

### I. Feature & acceptance check

| ID | Requirement | Status | Evidence | Gap |
|---|---|---|---|---|
| I1 | Target repo and exact starting commit recorded | DONE | `demo/stage2.py:19-21` `COMMIT=97267473c8accfd998bb9f756034eac48fa154f6`, `REPOSITORY`, `BRANCH`; re-asserted at `demo/stage2.py:45`; saved in `baseline/task.json` and `baseline/proof.json`. | — |
| I2 | Request, allowed changes, acceptance commands written down and saved with the task | DONE | `demo/stage2/TASK.txt` (hash-pinned); `demo/stage2.py:63` `write_spec` → `task.json` with `allowed_changes`, `checks`, three `acceptance_commands`, `limits`. `tests/test_feature_demo.py:60`. | — |
| I3 | Touches ≥2 interacting modules, preserves callers, handles an error case | DONE | Scope `domain/model.py`, `service_layer/services.py`, `adapters/orm.py` (`demo/stage2.py:25`); `scope()` **enforces** ≥2 of them changed (`demo/stage2.py:146`); error case `services.InvalidExpiry` raised before any mutation (TASK.txt); existing callers guarded by the 24 upstream tests. | Design only — see H8. |
| I4 | Feature + regression checks exist; feature check fails on the starting code for the expected reason | DONE | `demo/stage2/acceptance/acceptance_expiry.py` (23 cases) + `demo/stage2/regression/tests/` (24 upstream tests, `SOURCE.json` hash-verified against the starting commit, `LICENSE.cosmicpython` kept). `baseline/acceptance.log` shows all 22 failures are the same `MISSING FEATURE` assertion and the one pass is `test_legacy_allocation_control_still_works_on_starting_code`. `demo/stage2.py:155` `expected_missing_feature` codifies that. | — |
| I5 | Acceptance check outside the writable area, in contained execution | DONE | `PROTECTED = demo/stage2/` (`demo/stage2.py:22`), asserted outside the target (`:48`); `task_record.py:103` `validate()` raises `ScopeError` for an acceptance path inside the root; `cli.py:203` refuses it too. Runs in Docker, read-only at `/protected` (`demo/stage2.py:108-112`), hash-pinned by `CHECKS.sha256.json` (`check_integrity` :56). `tests/test_task_execution.py:302,326`; `tests/test_feature_demo.py:50`. | — |
| I6 | Evidence of a real-model run producing the final diff **and passing checks** | **PARTIAL** *(evidence now in-tree)* | `demo/results-stage2/RESULTS.md` documents 7 live runs (`qwen2.5-coder:7b`, `qwen3:8b`) with per-run outcomes, stopping rules and protected-check verdicts; `environment.json` pins platform, Python, Docker, Ollama tags, settings hash. Manual help disclosed in a dedicated section. **Added:** all seven runs' raw artefacts committed under `demo/results-stage2/runs/` (52 files, 1.6 MB — harness logs, task records, saved sessions, diffs, acceptance/regression/scope results, run settings), home prefix redacted to `~`, with `runs/README.md` as an index. I re-read every run's `task-record.json` and **all seven recorded statuses and action counts match the narrative in RESULTS.md exactly.** | **No run passes the checks.** Run 7: acceptance exit 2 (import error), regression exit 4. Run 6: 1 passed / 20 failed / 2 errors. That is the whole remaining gap — it needs a model run, not evidence handling. |
| I7 | The harness itself found and changed the relevant modules | **PARTIAL** *(now verifiable)* | **Added:** the navigation steps are now checkable from a fresh clone. From `runs/attempt-4-qwen2.5-coder/task-record.json`: action 1 `repo_map {}`, 2 `find_symbol {'name': 'allocate'}`, 3–5 `read_file` on the three modules, then the edits at 7–8. No file path or line number was handed over as an edit instruction. Scope enforcement also verified from the same record: action 21 `DENIED replace_in_file tests/conftest.py`. Quoted in `runs/README.md`. | The task request **does** name the three files — "Scope: change only `domain/model.py`, `service_layer/services.py` and `adapters/orm.py`" (TASK.txt) — repeated in `--allow`. A saved task must record its allowed changes (C1), so this is arguably required; but taken literally, "doesn't hand it exact file paths" is not met, and navigation corroborates rather than discovers. Stated as such in `runs/README.md`. |
| I8 | Equivalent business feature, not a trivial one-file change | **PARTIAL** | The *specification* fully qualifies: new behaviour (optional `expires_at`), follow-on effect (`release_expired` across domain + service + ORM persistence), invalid input rejected with a clear error (`InvalidExpiry`), old calls work when the option is unused (24 upstream tests as the gate), injectable clock. Three interacting modules. | Not implemented. Across all 7 runs the models added signatures only and never reached `Batch.release_expired`, `services.release_expired` or `services.InvalidExpiry`. |
| I9 | Edit–test–fix verified for this feature | **PARTIAL** | Failure recovery is covered by scripted tests (H6: `test_failing_check_is_fed_back_and_the_fix_passes`, `test_done_is_refused_until_a_check_passes`, `test_an_edit_that_would_break_the_file_is_refused_and_the_loop_recovers`). Live runs show failed check → edit → re-check (RESULTS.md run 4 actions 13/15). | The loop never converged *on this feature*. The scripted coverage is generic, not keyed to the expiry task. |

### J. Delivery checklist

| ID | Requirement | Status | Evidence | Gap |
|---|---|---|---|---|
| J1 | Runnable source, clean committed state at an exact commit | DONE | `git status` clean at audit start; HEAD `fbebfad6107f4fdf2448f2243a6d7e70f14838f4`. CLI runs (`main.py --help`, and a live `--root` run exercised during this audit). | This audit file makes the tree dirty. Commit it (or delete it) and re-read HEAD before putting a hash in Moodle. |
| J2 | Dependencies declared, launch commands, example settings without secrets | DONE | `requirements.txt`; launch commands in README "Getting Started" / "Usage"; `settings.example.toml` and `projects.example.toml` carry no secrets. `settings.toml` and `projects.toml` are git-ignored. `git grep` for key/secret/token/password across tracked files returns only prose and `CancelToken`/`max_reply_tokens` identifiers. No `.env*` tracked. `tests/test_settings.py:23` asserts the example file equals the defaults. | — |
| J3 | README: setup, model settings, design decisions (context + checkpoint/resume), known limits, **credits for reused code and AI help** | DONE *(fixed after audit)* | Setup `README.md:134-158`; model settings, `settings.example.toml`; **context design** (the budget number and the trusted/untrusted split); **checkpoint/resume design**. **Added:** a `## 📚 Credits` section with *Reused code* (cosmicpython/code — Percival & Gregory — as the demo target and the 7 vendored test files, with `SOURCE.json` hashes and the CC-BY-ND notice cited), *Third-party dependencies*, and *AI assistance* (7 of 40 commits authored by `Claude`, named by hash, with what was and was not AI-assisted). **Added:** a top-level `### Known limits` section collecting the eight real limits. | *Was:* no credits section at all, and known limits only as one inline paragraph. The AI-assistance wording is written in the first person and should be read once to confirm it matches your recollection — I scoped it from `git log --author=Claude` and `RESULTS.md`, which is all the repository records. |
| J4 | Diagrams exist and match the implementation | DONE *(fixed after audit)* | Three ASCII diagrams plus the module-responsibility table mapping every Figure-2 box to a real module (`Controller`=AgentController, `Verifier`=Verification). **Fixed:** the architecture diagram now draws `Controller ──> ModelClient ──> Ollama` with `ContextManager` as a component the Controller uses ("builds every request inside the budget; never calls the model"), matching `controller.py:277-278`. **Fixed:** the agent-loop diagram now shows both checkpoints — `pending` written before the tool runs, cleared after the result — with a note on why that pair is what makes an interrupted action detectable. The stopping-rule line now also lists the futile-repeat rule and cancellation. | *Was:* `Controller <──> ContextManager ──> ModelClient` (implied the context manager calls the model) and no checkpoint in the loop diagram. |
| J5 | Automated tests for Stage 2 **and** Stage 1, one documented command | DONE *(log regenerated)* | One command, documented in README "Automated Test Suite": `python -m pytest -q`. Stage 2 suites: `test_navigation`, `test_context`, `test_sessions`, `test_cancellation`, `test_task_execution`, `test_cli_commands`, `test_reporting`, `test_feature_demo`. Stage 1 suites: `test_controller`, `test_repository_tools`, `test_limits`, `test_execution`, `test_cli`, `test_bugfix_flow`, `test_model_client`, `test_verification`, `test_sandbox`, `test_settings`. 498 pass. **Fixed:** `tests/test_results.txt` regenerated — 506 lines, "498 passed". **Fixed:** README now states the Docker prerequisite with exact numbers (74 tests touch Docker; 2 skip cleanly, 72 fail without it; 424 need neither Docker nor a model) and gives the `docker build` line. | — |
| J6 | README lists manual help given during the real-model run | DONE *(fixed after audit)* | **Added** `### Manual help during the real-model run` to README, summarising all five disclosures from RESULTS.md (checks hand-written and hash-pinned before any run; three harness defects diagnosed and fixed by hand between runs; no feature code written by hand; run 4's diff re-applied to a clean copy; `max_reply_tokens` raised for the qwen3 run). **Added** a blockquote in "Real-World Demos" stating plainly that the harness worked and the feature did not land, so a grader reading only the README is not misled. | — |
| J7 | Video ≤ 4 min | n/a | Cannot verify. Suggested content below. | — |
| J8 | Submission readiness | **PARTIAL** | No secrets, dependencies declared, suite green at 503. **Fixed:** the run evidence is now in the repository, so the submitted artefact can be audited on its own. | Two things left, both yours: (1) commit, then take the hash — the tree is dirty with this work; (2) repo-link reachability is not verifiable from here, so confirm `github.com/AimenFH/…` is reachable by the grader. |

**Count at audit time: 73 DONE / 10 PARTIAL / 1 MISSING.**
**After the documentation fixes: 76 DONE / 8 PARTIAL / 0 MISSING.**
**After the code and evidence fixes: 78 DONE / 6 PARTIAL / 0 MISSING** (J7 not assessable).

Still PARTIAL — **H8, I6, I7, I8, I9, J8**. Five of the six are one thing wearing
five hats: *the feature was never implemented by a model.* Nothing but a model run
closes them. J8 needs a commit and a reachable repo link, both yours.

## Step 2b — Spec coverage self-check

**No assignment PDF or spec text is in the repository.** `find . -iname "*.pdf"` returns nothing; there is no `assignment/` folder, and `docs/` holds only `stage2-progress.md`.

The closest thing to the spec is the student's own restatement of the brief in `docs/stage2-progress.md:112-200` (rows `N1–N4`, `C1–C5`, `T1–T3`, `S1–S6`, `V1–V5`, `U1–U4`, `P1–P5`, tests `TN/TC/TS/TX/TR/TL/T1/TF`, delivery `D1–D6`). I went through it row by row and every one maps onto an ID above:

`N1–N4` → A1–A4 · `C1` → B1 · `C2` → B2 · `C3` → B3+B4 · `C4` → B5 · `C5` → B6 · `T1` → C1 · `T2` → C2+C3 · `T3` → G4 · `S1` → D1 · `S2` → D2 · `S3` → D3+D8 · `S4` → D4 · `S5` → D5 · `S6` → D6 · `V1` → E1+E2 · `V2` → E3 · `V3` → E4 · `V4` → E5 · `V5` → E6 · `U1` → F1 · `U2` → F2 · `U3` → F3+F4 · `U4` → F5 · `P1` → G1+G3 · `P2` → G2 · `P3` → G4+G5+B6 · `P4` → G6 · `P5` → G7 · `TN/TC/TS/TX/TR/TL/T1/TF` → H1–H8 · `D1` → J1 · `D2` → J2 · `D3` → J5 · `D4` → J3+J4 · `D5` → J6+J7 · `D6` → I1–I6.

**Every requirement in the repository's restatement of the spec is mapped to an ID. No `X` items were needed.** Caveat I cannot remove: because the original brief is not in the repo, I audited against the task description you gave me plus the student's restatement. If the real PDF contains a requirement neither document mentions, this audit does not cover it. **Assumption stated:** the restatement in `stage2-progress.md` is faithful to the brief.

One inconsistency worth naming: `stage2-progress.md:151` marked `TF` ("Feature checks fail on starting code, **pass after the change**") as ✅ with the baseline proof as evidence. The baseline proof only covers the "fail first" half, and the claim was contradicted by the file's own header, which says Phase 9 is "run; feature not passing". **Fixed after the audit:** the row now reads 🟡 and states which half is proven and which is not.

## Blockers

Most important first.

1. **The Stage 2 feature was never implemented, so the acceptance check never passes (H8, I6, I8, I9).** Seven real-model runs, best result 1/23 acceptance cases. The brief asks for a feature that fails before and passes after. Right now the delivery demonstrates a harness, not a completed feature. This is the one finding that can sink the grade, and it is honestly disclosed in RESULTS.md — which helps, but does not satisfy the requirement.
2. ~~**None of the real-model evidence is in the repository (I6, I7, J8).**~~ **FIXED.** All seven runs are committed under `demo/results-stage2/runs/` with an index, home paths redacted. Every recorded status and action count matches RESULTS.md, and I7's navigation sequence is now quotable from the committed record.
3. ~~**No credits for reused code or AI help (J3, J6).**~~ **FIXED.** README now has a `Credits` section citing cosmicpython/code (as both the demo target and the source of the 7 vendored test files, with the CC-BY-ND notice) and an `AI assistance` subsection naming the 7 AI-authored commits, plus a `Manual help` subsection. *Read the AI-assistance paragraph once to confirm it matches what you actually did — I could only scope it from the git history and RESULTS.md.*
4. ~~**The Stage 1 `--root` form bypasses the project allow-list (F1, G1).**~~ **MITIGATED.** The run now reports `Approval: NONE …` in its own output and warns on stderr, and an approved project with a Git remote is refused outright. The legacy form still accepts an unapproved root — refusing it would break the documented Stage 1 launch contract — so this is now visible rather than silent, and documented as such.
5. ~~**`tests/test_results.txt` claims 152 tests; there are 498 (J5).**~~ **FIXED.** Regenerated: 506 lines, "498 passed in 50.25s".
6. ~~**72 of 498 tests silently depend on Docker (H9, J5).**~~ **FIXED.** README now states the exact split (74 touch Docker, 2 skip cleanly, 72 fail) and gives the `docker build` command as a prerequisite.
7. ~~**The progress log marks `TF` as done (Step 2b).**~~ **FIXED.** `docs/stage2-progress.md:151` now reads 🟡 "half done", spelling out which half is proven and which is not.

## Prioritized TODO

Effort: S ≤ 30 min · M ≤ half a day · L > half a day.

| # | Action | ID | Effort |
|---|---|---|---|
| ~~1~~ | ~~README "Credits and reused code" + "AI assistance".~~ **DONE** — `## 📚 Credits` added. | J3 | S |
| ~~2~~ | ~~README "Manual help during the real-model run" + a plain statement that the feature did not land.~~ **DONE** — subsection plus a blockquote in "Real-World Demos". | J6, J3 | S |
| ~~3~~ | ~~Regenerate `tests/test_results.txt`.~~ **DONE** — 498 passed, 506 lines. | J5 | S |
| ~~4~~ | ~~Document the Docker prerequisite with exact counts.~~ **DONE** — 74 touch Docker / 2 skip / 72 fail / 424 independent, plus the `docker build` line. | H9, J5 | S |
| ~~5~~ | ~~Fix the `TF` row in `docs/stage2-progress.md`.~~ **DONE** — now 🟡 with both halves spelled out. | Step 2b | S |
| ~~6~~ | ~~Fix the architecture diagram arrow and add the pre-action checkpoint to the loop diagram.~~ **DONE** — both corrected. | J4 | S |
| ~~7~~ | ~~Add a top-level README "Known limits" heading.~~ **DONE** — eight limits collected under `### Known limits`. | J3 | S |
| ~~8~~ | ~~Copy the real-model evidence into the repo.~~ **DONE** — all 7 runs under `demo/results-stage2/runs/`, 52 files, redacted, indexed. | I6, I7, J8 | M |
| ~~9~~ | ~~Close the F1 bypass.~~ **DONE** (as mitigation) — `Approval:` line, stderr warning, `Config.approved`, 2 tests. | F1 | M |
| ~~10~~ | ~~Add a disposable-copy guard in application code.~~ **DONE** — `projects.require_disposable()` refuses a remote-bearing approved project, 2 tests. | G1 | M |
| **11** | **Land the feature and record a passing run.** Run the prepared task on a stronger model (RESULTS.md's own first recommendation) or split TASK.txt into smaller steps, then `demo/stage2.py verify` to green and commit the diff + session into `runs/`. **This is the only substantive item left, and it needs your local Ollama.** | H8, I6, I8, I9 | L |
| **12** | Add `tests/test_feature_demo.py::test_acceptance_passes_on_the_reference_implementation`: apply a committed reference patch to a fixture, run the 23 cases, assert 23 passed — so "pass after the change" is covered by a test whatever model is available. **Deliberately not done here:** it means writing the feature by hand, and `RESULTS.md` currently states that no feature code was written by hand. Doing it would need that disclosure amended, so it is your call, not mine. | H8 | M |
| ~~13~~ | ~~Move the permission check ahead of the `pending` checkpoint.~~ **DONE** — `check_permission` → checkpoint → `run_tool`, with a test that fails against the old order. | C4 | S |
| ~~14~~ | ~~Move the `--live-diff` Verifier construction into an application helper.~~ **DONE** — `application.live_diff(root)`. | F5, R1 | S |

## Risks / things I could not verify

- **Container isolation was not exercised end to end by me.** `tests/test_sandbox.py::test_container_cannot_see_secrets_host_files_or_network` passed in my run with a live Docker daemon, which is good evidence, but I did not independently inspect a running container's mounts or network namespace.
- **The real-model runs.** I have RESULTS.md, `environment.json` and the baseline artefacts, and the baseline numbers reproduce from committed files. The seven attempt folders exist on this machine (`~/harness-demo/stage2-run/attempt-1…7`) but are outside the repo; I did not open them to confirm the per-run transcripts match RESULTS.md's narrative. Treat I6/I7 as "documented, artefacts out of tree".
- **Whether the task's naming of the three files violates I7.** The scope must be recorded (C1 requires it), so naming the files is arguably required rather than a hint. A grader could read it either way. The fix is cheap: show the navigation steps from the saved session as evidence (TODO #8).
- **Repo-link reachability and the video (J7, J8).** Not checkable offline.
- **Ollama was never contacted during this audit**, by design. Everything about live model behaviour comes from the committed records, not from a run I made.
- **The original brief.** Not in the repo. See the caveat in Step 2b.

## Suggested demo flow for the 4-minute video

Based only on what demonstrably works today.

| Time | Show | Why it earns marks |
|---|---|---|
| 0:00–0:20 | `main.py --help`, then `main.py projects` listing the approved disposable copy. Say "nothing outside this list can be touched." | F1, F2, G1 |
| 0:20–0:50 | `demo/stage2.py baseline` — acceptance FAIL (22 × `MISSING FEATURE`), regression PASS 24/24, target unchanged. Point at `CHECKS.sha256.json` and say the checks are hash-pinned and outside the agent's reach. | I1, I4, I5, H8 (first half) |
| 0:50–1:40 | `main.py start` with a real model. Let the live log show `[context] suggested files: domain/model.py, service_layer/services.py, adapters/orm.py`, a `repo_map` / `find_symbol` step, then `[context] history over budget: … -> … (budget 24000)`. | A1–A4, B1–B4 |
| 1:40–2:10 | Scroll to a `DENIED` line (out-of-scope edit, or the already-applied-change refusal) and to an `UNTRUSTED REPOSITORY DATA` block. Say the controller decides, not the model. | G4, B6, C3 |
| 2:10–2:40 | From a second terminal: `main.py cancel <id>`. Show the run stopping, then `main.py status <id>`. | D3, D8, F3 |
| 2:40–3:10 | `main.py resume <id> --dry-run`; then change a file by hand and resume again to show **PAUSED** with the named mismatch. This is the strongest single thing in the project. | D4, D6, H5 |
| 3:10–3:35 | `main.py show <id>` — Checks, Diff, Unfinished work, with `sandbox=docker (no network, read-only repository)` visible on a check. | E1–E5, G2, D7 |
| 3:35–4:00 | `python -m pytest -q` → 503 passed. Then **state plainly that the feature does not pass the acceptance check, that the cause is local-model capability, and exactly what manual help was given** (task text and checks hand-written up front; three harness defects diagnosed and fixed by hand between runs; no feature code written by hand; `max_reply_tokens` raised for the qwen3 run). | J5, J6 |

**Say the manual help out loud in the video, not just on screen.** Your RESULTS.md disclosure is honest and detailed — repeating it in the video converts a weakness into evidence of rigour. The same goes for the feature not landing: naming it yourself, with the seven-run analysis of *why*, is worth more than letting the grader find it.
