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
     └───────┼──────────────────┘
             ▼
     Verification (Tests + Git Diff) ──> User Review
```

### Module Responsibilities
| Module | Responsibility | Key Features |
| :--- | :--- | :--- |
| `main.py` | **Interface** | Argument parsing, session wiring, and structured reporting. |
| `controller.py` | **Orchestration** | The agentic loop: Request $\rightarrow$ Validate $\rightarrow$ Execute $\rightarrow$ Return. |
| `model_client.py` | **LLM Bridge** | Integration with Ollama; handles system prompting and JSON parsing. |
| `repository_tools.py` | **File Access** | Secure implementations of `read`, `search`, `list`, and `edit`. |
| `execution.py` | **Environment** | Contained execution of whitelisted test commands (pytest/unittest). |
| `verification.py` | **Validation** | Final check of resulting code via `git diff` and test suites. |
| `config.py` | **Settings** | Centralized management of limits, timeouts, and defaults. |

---

## 🛡️ Security & Containment

To prevent the LLM from performing destructive actions or accessing sensitive data, this harness implements multiple layers of protection:

1.  **Disposable Copies**: The harness does not operate on the original source code. It utilizes a disposable copy of the target repository (via `demo/demo.py prepare`), ensuring the original codebase remains untouched.
2.  **Path Sandboxing**: Every tool in `repository_tools.py` enforces a strict root-directory boundary. Any attempt by the model to access files outside the `--root` folder (e.g., using `../` to reach SSH keys or system files) is intercepted and rejected with an `Access Denied` error.
3.  **Command Whitelisting**: The `ExecutionEnvironment` does not provide a general shell. It only allows the execution of pre-configured, safe test runners (`pytest`, `unittest`).
4.  **Resource Limits**: To prevent infinite loops or resource exhaustion, the `AgentController` enforces:
    *   **Action Limit**: Maximum number of model replies per session.
    *   **Output Limit**: Truncation of oversized tool outputs to prevent context window overflow.
    *   **Denied Action Tracking**: The session terminates if the model repeatedly requests forbidden actions.

---

## 🚀 Getting Started

### Prerequisites
*   Python 3.10+
*   [Ollama](https://ollama.com/) installed and running.

### Setup
```bash
# 1. Create and activate virtual environment
python -m venv .venv
source .venv/bin/activate

# 2. Install dependencies
pip install -r requirements.txt

# 3. Pull the recommended model
ollama pull qwen2.5-coder:7b
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

---

## 🧪 Validation & Testing

### Automated Test Suite
The project includes a comprehensive test suite in `tests/` that uses a `ScriptedModelClient` to ensure deterministic results.
```bash
python -m pytest -q
```
**Coverage includes**:
- ✅ **Tool Safety**: Verification that paths outside `--root` are rejected.
- ✅ **Controller Logic**: Validation of the action loop and tool routing.
- ✅ **Limit Enforcement**: Testing that action limits and output truncations work.
- ✅ **Error Handling**: Ensuring invalid tool requests return clear errors.

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
- [x] **Containment**: Path sandboxing and disposable target copies implemented.
- [x] **Tests**: Full suite of automated tests for all core components.
