# 🛠️ Coding Harness (Stage 2 in progress)

This project is a secure coding harness designed to connect a Large Language Model (LLM) to a local codebase. It allows an AI agent to explore a repository, identify bugs, apply fixes, and verify those fixes through automated tests—all while maintaining a strict security boundary to protect the host system.

## 🏗️ Architecture

The harness follows a modular design to ensure a clear separation of concerns:

```text
main.py ──> cli.py ──> application.py ──> Controller <──> ModelClient / Ollama
                            │                │
                            │                ├── RepositoryTools / Navigator
                            │                ├── Context (task + files + results)
                            │                └── ExecutionEnvironment ──> Docker
                            │
                            ├── Project approvals / SessionStore
                            └── Verifier (checks in Docker + Git diff)
                                          │
                                          ▼
                                Saved results ──> CLI review

Docker: read-only repository mount, no network, no unrelated host files
```

### The agent loop (`controller.py`)

```text
 Read task, mode and selected files
             │
             ▼
 ┌──> Ask model for one action (JSON) ──── "done" ───> Final checks in sandbox
 │           │                                         + changed files + diff
 │           ▼                                                 │
 │   Validate: known tool? valid arguments?                    ▼
 │   path inside --root? allowed in this mode?           User reviews result
 │           │                  │
 │         allowed           refused
 │           ▼                  ▼
 │      Run the tool      DENIED / ERROR text
 │   (checks run in the     (counted)
 │      sandbox)                │
 │           │                  │
 └── result back to the model ◄─┘

 Stops on: done · action limit · too many refused/malformed replies in a row · model error
```

### Module Responsibilities
| Module | Responsibility | Key Features |
| :--- | :--- | :--- |
| `main.py` | **Entry point** | Thin executable; preserves Stage 1 imports and model/sandbox injection. |
| `cli.py` | **Interface** | Subcommands, argument validation, progress and saved-result presentation; no controller loop. |
| `application.py` | **Task services** | Start/resume/cancel orchestration, sandbox/model wiring, final verification and saved results; no argument parsing. |
| `projects.py` | **Project approvals** | Strict TOML configuration, canonical repository paths, protected control-file locations. |
| `session.py` | **Persistence** | Atomic checkpoints, saved history/checks/diff, safe-resume validation, one runner per task. |
| `controller.py` | **Orchestration** | The agentic loop: Request $\rightarrow$ Validate $\rightarrow$ Execute $\rightarrow$ Return. |
| `model_client.py` | **LLM Bridge** | Integration with Ollama; handles system prompting and JSON parsing. |
| `repository_tools.py` | **File Access** | Secure implementations of `read`, `search`, `list`, and `edit`. |
| `navigation.py` | **Navigation** | Read-only, language-aware tools: `find_files`, `list_symbols`, `find_symbol` (AST-based, never runs code), `module_dependencies` (imports and importers), `git_state` (branch, commit, changed/untracked files). |
| `execution.py` | **Environment** | Whitelisted test commands (pytest/unittest), run in a Docker sandbox (`DockerSandbox`) with timeouts and process cleanup. |
| `verification.py` | **Validation** | Final check of resulting code via `git diff` and test suites. |
| `config.py` | **Settings** | Centralized management of limits, timeouts, and defaults; loads `--settings` files. |
| `sandbox/Dockerfile` | **Sandbox image** | Python + pytest + the target's packages. The repository is mounted, never copied in. |

---

## 🛡️ Security & Containment

To prevent the LLM from performing destructive actions or accessing sensitive data, this harness implements multiple layers of protection:

1.  **Disposable Copies**: The harness does not operate on the original source code. It utilizes a disposable copy of the target repository (via `demo/demo.py prepare`), ensuring the original codebase remains untouched.
2.  **Path Sandboxing**: Every tool in `repository_tools.py` enforces a strict root-directory boundary. Any attempt by the model to access files outside the `--root` folder (e.g., using `../` to reach SSH keys or system files) is intercepted and rejected with an `Access Denied` error.
3.  **Contained Execution**: The `ExecutionEnvironment` does not provide a general shell. It only runs pre-configured test runners (`pytest`, `unittest`), and by default runs them inside a throwaway **Docker container** (`--sandbox docker`):
    *   only the repository copy is visible, mounted **read-only** at `/work` — no home folder, no SSH keys, no other host files;
    *   **no network** (`--network none`), no host environment variables (API keys, tokens), no Linux capabilities, a read-only system, a non-root user, and memory/CPU/process limits;
    *   on timeout or Ctrl+C the container is force-removed, so nothing keeps running or writing.

    The acceptance check and regression tests in `demo/demo.py` run in the same sandbox. `--sandbox none` runs checks directly on this machine; the output then says *NOT contained*.
4.  **Resource Limits**: To prevent infinite loops or resource exhaustion, the `AgentController` enforces:
    *   **Action Limit**: Maximum number of model replies per session.
    *   **Output Limit**: Truncation of oversized tool outputs to prevent context window overflow.
    *   **Denied Action Tracking**: The session terminates if the model repeatedly requests forbidden actions.
5.  **`done` Must Be Earned**: After changing files, the model may only call `done` once a check has *passed* since its last change. Otherwise `done` is refused and the model is told why (3 refusals in a row stop the run). With no changes, `done` is always allowed, so the model can still report that it could not fix the task.

---

## 🚀 Getting Started

### Prerequisites
*   Python 3.11+
*   [Ollama](https://ollama.com/) installed and running.
*   [Docker](https://docs.docker.com/get-docker/) running (for the sandbox the checks run in).

No secrets, API keys or accounts are needed: the model runs locally in Ollama.

### Setup
```bash
# 1. Create and activate virtual environment
python -m venv .venv
source .venv/bin/activate

# 2. Install dependencies
pip install -r requirements.txt

# 3. Pull the recommended model
ollama pull qwen2.5-coder:7b

# 4. Build the sandbox image (once; again after changing demo/requirements-target.txt)
docker build -t harness-sandbox -f sandbox/Dockerfile .

# 5. Optional: your own settings (limits, timeouts, sandbox)
cp settings.example.toml settings.toml
```

### Usage

Approve a **disposable repository copy** explicitly before using the Stage 2 commands.
The harness edits the selected copy directly; `start` does not create one for you.
The copy must be a Git repository with at least one commit.

```bash
mkdir -p ~/.harness
cp projects.example.toml ~/.harness/projects.toml
# Edit ~/.harness/projects.toml with your actual disposable repository path:
```

```toml
[projects]
cosmic = "/absolute/path/to/disposable-cosmic-copy"
```

Names select exactly one directory. Paths may be absolute, start with `~/`, or be
relative to the TOML file. Missing/empty configurations, unknown fields, invalid
types, duplicate canonical paths and dangerous roots are rejected. Keep the
approval file, settings, session directory and task records outside approved
repositories so the agent cannot modify its own permissions or evidence.

```bash
python main.py projects
python main.py start --project cosmic \
  --task "Fix the incorrect total calculation in pricing.py" \
  --mode edit --model qwen2.5-coder:7b --check pytest \
  --allow pricing.py --file pricing.py

# Use the ID printed as soon as the task starts, including from another terminal:
python main.py list
python main.py status TASK_ID
python main.py show TASK_ID
python main.py cancel TASK_ID --reason "Pause for review"
python main.py resume TASK_ID --dry-run
python main.py resume TASK_ID
```

`start` runs in the foreground and saves every checkpoint by default under
`~/.harness/sessions`. `cancel` requests a cooperative stop; `status` shows the
persisted outcome once the worker observes it. `show` displays action results,
errors, conversation history, final checks and the saved diff without contacting
the model, running tests, or requiring the target repository to still exist.
For an interrupted or older task without a final report, unavailable results are
labelled explicitly. To inspect the **current** working-copy diff as well, use
`show TASK_ID --live-diff`; this requires current project approval and runs no tests.

Override the configuration/storage paths before or after a subcommand:

```bash
python main.py --projects-file /path/to/projects.toml \
  --session-dir /path/to/sessions start --project cosmic \
  --task "Inspect allocation behavior" --mode readonly --model qwen2.5-coder:7b \
  --offline
python main.py status TASK_ID --session-dir /path/to/sessions
python main.py resume TASK_ID --projects-file /path/to/projects.toml \
  --session-dir /path/to/sessions --offline
```

`--offline` uses scripted model replies; final checks still run in Docker by
default. Run `python main.py COMMAND --help` for command options. The existing
`--settings`, `--file`, `--allow`, `--acceptance`, `--record`, `--check` and
`--sandbox` options are available on `start`. Model and sandbox settings are
saved for resume; `resume --model NAME` or `--sandbox docker|none` overrides them.

| Exit code | Meaning |
| :--- | :--- |
| `0` | Successful run, successful inspection/cancel request, or safe resume dry run. |
| `1` | Task did not complete successfully, or final checks failed/were unavailable. |
| `2` | Invalid usage/configuration, denied project, unreadable session, active-session conflict, or setup/I/O error. |
| `3` | Resume paused because checkpoint safety checks failed; no actions executed. |
| `130` | Task cancelled or interrupted with Ctrl+C. |

### Checkpoints and safe resume

Task specifications, actions and conversation history are saved outside the
target with atomic JSON replacement. A pre-action checkpoint records the action
about to run; a post-action checkpoint records its outcome. Final verification
adds the full check results/output and diff to the session. If the process dies
before verification is saved, `show` reports the missing final result explicitly.

Resume preserves consumed limits, mode and allowed paths; checks current project
approval, branch/commit, changed-file status and hashes of harness-written files;
and refuses a completed task or an exhausted budget. Uncertain interrupted actions
pause the CLI and are never replayed automatically. Inspect the history and current
diff, then start a new task after resolving uncertainty. A process lock prevents
two CLI workers from resuming the same session. `--dry-run` leaves the checkpoint
and cancellation marker unchanged. Cancelled runs remain resumable once their
worker has stopped. Cancellation also applies to final verification.

Known limits: runner locks currently require POSIX (`fcntl`, macOS/Linux). The
existing checkpoint tracks content hashes for harness-written files and Git status
for other changes; it does not hash every untouched file. Context compaction and
additional lint/build checks remain separate planned Stage 2 work.

### Stage 1 compatibility

The original launch form still works, including the demo and existing Python
imports. Direct `--root` selection applies the default project configuration when
present, or an explicit `--projects-file`. Without a configuration it retains
Stage 1 behavior; use `start --project` to require approval on every launch. Legacy runs
are saved only when `--session-dir` is supplied. Existing `--list-sessions`,
`--cancel ID` and `--resume ID` forms remain available with `--session-dir`.
Tasks started with approvals still require current approval when resumed through
the legacy flag form.

```bash
python main.py \
  --root /path/to/target-repo \
  --task "Fix the incorrect total calculation in pricing.py" \
  --mode edit \
  --model qwen2.5-coder:7b \
  --check pytest \
  --file pricing.py
```

| Option | Meaning |
| :--- | :--- |
| `--root` | The disposable copy of the target repository (required). |
| `--task` | The bug-fix request (required). |
| `--mode` | `readonly` (inspect only) or `edit` (may change files) (required). |
| `--model` | Ollama model name (required). |
| `--check` | Test command for `run_check` and verification: `unittest` (default) or `pytest`. |
| `--file` | A file sent to the model with the task. Repeat for several files. |
| `--sandbox` | `docker` (default) or `none` (checks run directly on this machine, NOT contained). |
| `--settings` | A TOML settings file, see [`settings.example.toml`](settings.example.toml). |
| `--offline` | Do not contact Ollama; uses a tiny scripted reply (for trying the CLI). |

The harness stops with a clear error before contacting the model if Docker is not running or the image is missing.

---

## 🧪 Validation & Testing

### Automated Test Suite
The project includes a comprehensive test suite in `tests/` that uses a `ScriptedModelClient` to ensure deterministic results.
```bash
python -m pytest -q
```
A full detailed log of the passing unit tests is provided in [tests/test_results.txt](tests/test_results.txt).
The tests that start real Docker containers (`tests/test_sandbox.py`) are skipped when Docker or the `harness-sandbox` image is not available; everything else needs neither Docker nor Ollama.

**Coverage includes**:
- ✅ **Tool Safety**: Verification that paths outside `--root` are rejected.
- ✅ **Controller Logic**: Validation of the action loop and tool routing.
- ✅ **Limit Enforcement**: Testing that action limits and output truncations work.
- ✅ **Error Handling**: Ensuring invalid tool requests return clear errors.
- ✅ **Sandbox**: The container sees no secrets, host files or network and cannot write the repository; a timeout removes the container.
- ✅ **Settings**: The example file matches the defaults; unknown or invalid settings are rejected.
- ✅ **Stage 2 CLI**: All commands, strict project configuration, denied projects, saved review after restart, failure exit codes, resume safety and concurrent-run rejection. A separate-process test cancels an active task during its first model wait, then resumes it safely. Existing cancellation tests cover process/child/container cleanup.

### Real-World Demo
The `demo/` folder contains a proof-of-concept fix for the **Cosmic Python** repository:
1.  **Reproduction**: An external acceptance check (`demo/acceptance/`) proves the bug exists.
2.  **Fix**: The harness is run with a real model to identify and fix the bug. The full run (failing acceptance check, harness output with the diff, and the passing verification) is in [demo/RESULTS.md](demo/RESULTS.md).
3.  **Verification**: The `demo/demo.py verify` command confirms that the acceptance check now passes and no regressions were introduced.

---

## 📋 Submission Checklist Mapping
- [x] **Runnable Source**: Provided via GitHub with clear launch commands.
- [x] **Interface**: Structured output showing Progress, Changed Files, Diff, and Results.
- [x] **Controller**: Validates tool requests and manages context loop.
- [x] **Repository Tools**: Secure file operations within allowed scope.
- [x] **Execution**: Contained test runs with output capture.
- [x] **Limits**: Enforced action/output limits and denied action counting.
- [x] **Verification**: Final code check and diff generation.
- [x] **Setup**: `requirements.txt`, `sandbox/Dockerfile`, and `settings.example.toml` (no secrets needed).
- [x] **Containment**: Checks run in a Docker sandbox (read-only repository, no network, no host files); path checks on every file tool; disposable target copies.
- [x] **Tests**: Full suite of automated tests for all core components.
