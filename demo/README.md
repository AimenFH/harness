# Stage 1 demo: bug fix in Cosmic Python

## Target repository

| | |
|---|---|
| Repository | https://github.com/cosmicpython/code |
| Branch | `chapter_05_high_gear_low_gear` |
| Starting commit | `97267473c8accfd998bb9f756034eac48fa154f6` ("api tests no longer need hardcoded sql fixture", 2019-04-24) |
| Why this one | Listed in the assignment. Small (~150 lines of app code), pure-Python business rules, real tests. |
| Modules that work together | `service_layer/services.py` (use case: validate and allocate an order) → `domain/model.py` (`Batch`, `OrderLine`, `allocate`: stock rules) ← `adapters/repository.py` (loads batches) |
| Existing tests | 24 tests in `tests/unit` and `tests/integration` (SQLite in memory). The 2 tests in `tests/e2e` need Flask + Postgres and are excluded. |

## The bug

`services.allocate()` accepts quantities of zero or less. A negative quantity
*increases* a batch's available stock, so a later order can take more units than
the batch holds:

```
batch of 10 → allocate(qty=-5) succeeds → available 15 → allocate(qty=15) succeeds
```

The behaviour crosses modules: the service layer accepts the request and commits,
and the domain model's stock accounting (`available_quantity`) is corrupted.

## Bug-fix request (given to the harness)

See [`TASK.txt`](TASK.txt). In short: `services.allocate` must raise a new
`services.InvalidQuantity` for `qty <= 0`, before anything is allocated or committed.

**Allowed to change:** `service_layer/services.py`, and `domain/model.py` if really needed.
Nothing else, and no tests. `python demo/demo.py scope` checks this.

## Acceptance check

[`acceptance/acceptance_allocation_quantity.py`](acceptance/acceptance_allocation_quantity.py)
lives in the harness repository, **outside the agent's writable area** (the agent's
`--root` is the disposable copy). It is run as a separate process, with a timeout,
inside the copy.

| Test | Starting commit | After fix |
|---|---|---|
| quantity 0 → `InvalidQuantity`, stock unchanged, no commit | FAIL | PASS |
| quantity -5 → `InvalidQuantity`, stock unchanged, no commit | FAIL | PASS |
| -5 cannot be used to over-allocate (15 of 10 → `OutOfStock`) | FAIL | PASS |
| positive quantity still allocates | PASS | PASS |

On the starting commit the failures show the bug itself, not just the missing exception:

```
Failed: allocate(qty=0) was accepted: available stock 10 -> 10, committed=True
Failed: allocate(qty=-5) was accepted: available stock 10 -> 15, committed=True
3 failed, 1 passed
```

## One-time setup

```bash
cd coding-harness
source .venv/bin/activate
pip install -r requirements.txt -r demo/requirements-target.txt
ollama pull qwen2.5-coder:7b  # or the model you will use
python demo/demo.py prepare   # clones once into ~/harness-demo, then makes a fresh copy
python demo/demo.py rehearse  # optional: full harness run with a SCRIPTED model (no Ollama)
python demo/demo.py prepare   # reset after the rehearsal
```

`prepare` always makes a fresh copy at the pinned commit and removes the git remote,
so nothing can be pushed from it. The harness's checks run with the harness's Python,
which is why the target's packages go into the same virtual environment.

## 4-minute demo script

Run `python demo/demo.py prepare` just before recording.

| Time | Show | Command |
|---|---|---|
| 0:00–0:30 | Target, commit, task | `cat demo/TASK.txt` and point at the table above |
| 0:30–1:00 | **Failure before** | `python demo/demo.py acceptance` → 3 failed, `FAIL` |
| 1:00–3:00 | **Harness run** with the real model | see below |
| 3:00–3:15 | **Diff** | the harness's `=== Diff ===` section |
| 3:15–4:00 | **Acceptance + regression + scope pass** | `python demo/demo.py verify` |

Harness run (Linux/macOS):

```bash
PYTEST_ADDOPTS="--ignore=tests/e2e" python main.py \
  --root ~/harness-demo/cosmic \
  --task "$(cat demo/TASK.txt)" \
  --mode edit --model qwen2.5-coder:7b --check pytest
```

Windows PowerShell:

```powershell
$env:PYTEST_ADDOPTS = "--ignore=tests/e2e"
python main.py --root $HOME\harness-demo\cosmic --task (Get-Content demo\TASK.txt -Raw) `
  --mode edit --model qwen2.5-coder:7b --check pytest
```

`--check pytest` is needed because this repository uses pytest-style tests
(`unittest discover` finds 0 of them). `PYTEST_ADDOPTS` excludes the e2e tests,
which need a web server and Postgres.

If a run goes wrong (the model calls `done` without fixing anything, or hits the
action limit), run `python demo/demo.py prepare` for a clean copy and try again.

## Manual help to declare in the demo

- The target's dependencies (`SQLAlchemy<2`) were installed by hand.
- The e2e tests are excluded with `PYTEST_ADDOPTS`.
- If the model needed more than one attempt, say so (and how many).
