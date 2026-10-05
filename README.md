# 🛠️ Coding Harness (Stage 1 Proof of Concept)

This project is a secure coding harness designed to connect a Large Language Model (LLM) to a local codebase. It allows an AI agent to explore a repository, identify bugs, apply fixes, and verify those fixes through automated tests—all while maintaining a strict security boundary to protect the host system.

## 🏗️ Architecture

The harness follows a modular design to ensure a clear separation of concerns:

```text
User CLI ──> Agent Controller <──> LLM (Ollama)
             │
     ┌───────┼──────────────────┐
     ▼       ▼                  ▼
 Repository  Execution        Basic Context
   Tools    Environment      (Task + Files + Results)
     │       │
     │       ▼
     │   Docker sandbox: read-only repo copy, no network, no host files
     └───────┼──────────────────┘
             ▼
     Verification (Tests in sandbox + Git Diff) ──> User Review
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
| `main.py` | **Interface** | Argument parsing, session wiring, and structured reporting. |
| `controller.py` | **Orchestration** | The agentic loop: Request $\rightarrow$ Validate $\rightarrow$ Execute $\rightarrow$ Return. |
| `model_client.py` | **LLM Bridge** | Integration with Ollama; handles system prompting and JSON parsing. |
| `repository_tools.py` | **File Access** | Secure implementations of `read`, `search`, `list`, and `edit`. |
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
Run the harness against a target repository:
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

### Real-World Demo
The `demo/` folder contains a proof-of-concept fix for the **Cosmic Python** repository:
1.  **Reproduction**: An external acceptance check (`demo/acceptance/`) proves the bug exists.
2.  **Fix**: The harness is run with a real model to identify and fix the bug. A full log of this successful execution is provided in [demo/demo_output.txt](demo/demo_output.txt).
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
