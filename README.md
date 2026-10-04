# harness

A small coding harness (Stage 1 proof of concept): it takes a coding task, lets a
local LLM (via [Ollama](https://ollama.com)) use tools to change a repository,
runs checks, and shows the changed files, test results and a diff.

```
CLI ──> Agent controller <──> LLM (Ollama)
             │
   ┌─────────┼──────────────────┐
   ▼         ▼                  ▼
Repository  Execution        Basic context
tools       environment      task + selected files + tool results
   └─────────┼──────────────────┘
             ▼
   Verification (tests + git diff)  ──>  user reviews the result
```

## How the pieces map to the design

| Box in the design | Module | What it does |
|---|---|---|
| CLI | `main.py` | Parses and validates arguments, wires everything together, prints the result in sections. |
| Agent controller | `controller.py` | The loop: ask the model → validate the request → run the allowed tool → send the result back. Enforces modes and limits. |
| LLM | `model_client.py` | The only code that talks to the model. Builds the system prompt, sends the conversation to Ollama, parses the reply into one JSON action. |
| Repository tools | `repository_tools.py` | `list_files`, `search`, `read_file`, `replace_in_file`, `edit_file`. Every path is resolved and must stay inside `--root` (and out of `.git`). |
| Execution environment | `execution.py` | Runs only *named, pre-configured* commands (`unittest`, `pytest`): no shell, cwd = repo, timeout, process-tree kill, secrets stripped from the environment. |
| Basic context | `model_client.initial_messages` + `Controller.selected_files_text` | The task request, the files selected with `--file`, and every tool result in the conversation history. |
| Verification | `verification.py` | Runs after every run, whatever the model claimed: `git status`, `git diff` (plus new files), and the final checks. |
| Settings | `config.py` | Modes, limits and defaults in one place. |

## Tools the model can request

| Tool | Mode | Notes |
|---|---|---|
| `list_files` | both | Skips `.git`, `node_modules`, virtualenvs, caches. |
| `search` | both | Plain-text, case-sensitive line search. |
| `read_file` | both | UTF-8 only, max 1 MB. Output over the limit is truncated. |
| `replace_in_file` | edit | `old` must occur exactly once. Preferred way to edit. |
| `edit_file` | edit | Writes a whole file. Refused for a file the model has only seen truncated. |
| `run_check` | both | Runs a whitelisted check by name. |
| `done` | both | Ends the run with a summary. |

## Safety and limits

- **Modes:** `readonly` refuses every edit tool; `edit` allows changes inside `--root` only.
- **Every request is checked** by the controller (tool name, argument names and types,
  path, mode) before anything runs; refused requests are counted as *denied*.
- **`done` must be earned:** after changing files, the model may only call `done`
  once a check has *passed* since its last change. Otherwise `done` is refused and
  the model is told why (3 refusals in a row stop the run). With no changes,
  `done` is always allowed, so the model can still report that it could not fix the task.
- **Limits** (`config.py`): 20 model replies per run, stop after 3 malformed/denied
  replies in a row, tool output cut to 10 000 characters (marked `[OUTPUT TRUNCATED]`).
- `--root` may not be `/`, your home folder, or a folder containing the harness itself.
- The result is `SUCCESS` only if the model called `done` **and** verification passed
  (at least one check ran and all passed — "0 tests ran" counts as a failure).

## Setup

```bash
python -m venv .venv
source .venv/bin/activate            # Windows: .venv\Scripts\activate
pip install -r requirements.txt
ollama pull qwen2.5-coder:7b         # or any other Ollama model
```

## Usage

```bash
python main.py --root path/to/target-repo \
  --task "Fix the incorrect total in orders/pricing.py" \
  --mode edit \
  --model qwen2.5-coder:7b \
  --check pytest \
  --file orders/pricing.py
```

| Option | Required | Meaning |
|---|---|---|
| `--root` | yes | The target repository (ideally a git repo, so the diff can be shown). |
| `--task` | yes | The coding task / bug-fix request. |
| `--mode` | yes | `readonly` or `edit`. |
| `--model` | yes | Ollama model name. |
| `--check` | no | `unittest` (default) or `pytest`; used by `run_check` and by verification. |
| `--file` | no | A file (relative to `--root`) sent to the model with the task. Repeatable. |
| `--offline` | no | Use a tiny scripted model instead of Ollama (to try the pipeline). |

`PYTEST_ADDOPTS` is passed through to the checks, e.g. `PYTEST_ADDOPTS="--ignore=tests/e2e"`.

The output has five sections: **Task**, **Progress** (one line per model action),
**Changed Files**, **Checks**, **Diff** and **Summary**. The exit code is `0` on
success and `1` otherwise.

Tip: run the harness on a disposable copy or a clean git checkout so you can review
the diff and throw the change away with `git checkout .` if you don't like it.

## Tests

```bash
pip install pytest
python -m pytest -q
```

The tests use a scripted model (`ScriptedModelClient`) and temporary repositories,
so they need neither Ollama nor network access.

## Demo

`demo/` contains the Stage 1 demo: a real bug fix in the
[Cosmic Python](https://github.com/cosmicpython/code) repository, with an
acceptance check kept outside the agent's writable area. See
[`demo/README.md`](demo/README.md).
