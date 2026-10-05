# Stage 1 demo results: real model run

Run on macOS, Python 3.14.7, Ollama with `qwen2.5-coder:7b`.
Setup and commands: see [`README.md`](README.md).

## Target and request

| | |
|---|---|
| Repository | https://github.com/cosmicpython/code |
| Branch | `chapter_05_high_gear_low_gear` |
| Starting commit | `97267473c8accfd998bb9f756034eac48fa154f6` |
| Request | [`TASK.txt`](TASK.txt): `services.allocate` must raise a new `services.InvalidQuantity` for `qty <= 0`, before anything is allocated or committed |
| May change | `service_layer/services.py` (`domain/model.py` only if really needed); no tests |

## 1. Acceptance check fails on the starting code

`python demo/demo.py acceptance`, run against a fresh copy at the starting commit.
The check is [`acceptance/acceptance_allocation_quantity.py`](acceptance/acceptance_allocation_quantity.py);
it lives in the harness repository, outside the agent's `--root`, and runs as a
separate process with a timeout inside the copy.

```
FFF.
FAILED acceptance_allocation_quantity.py::test_non_positive_quantity_is_rejected[0]
FAILED acceptance_allocation_quantity.py::test_non_positive_quantity_is_rejected[-5]
FAILED acceptance_allocation_quantity.py::test_negative_quantity_cannot_be_used_to_overallocate
3 failed, 1 passed
Exit code: 1
FAIL
```

This run used the first version of the check, whose failure message was
"`InvalidQuantity` does not exist". The current version calls `allocate` first and
reports the bug itself:

```
Failed: allocate(qty=-5) was accepted: available stock 10 -> 15, committed=True
```

## 2. Harness run with the real model

```bash
PYTEST_ADDOPTS="--ignore=tests/e2e" python main.py --root ~/harness-demo/cosmic \
  --task "$(cat demo/TASK.txt)" --mode edit --model qwen2.5-coder:7b --check pytest
```

```
=== Progress ===
[1] read_file service_layer/services.py
[2] edit_file service_layer/services.py
[3] read_file service_layer/services.py
[4] run_check
    SUCCEEDED (exit code 0)
[5] done

=== Changed Files ===
M service_layer/services.py

=== Checks ===
python -m pytest -p no:cacheprovider
Exit code: 0
PASS

=== Summary ===
Run:          done (actions 5/20 | executed 4 | malformed 0 | denied 0 | tool errors 0 | truncated 0)
Model says:   Added InvalidQuantity exception and checked that the tests pass. There is one warning about a deprecated API feature in the ORM, but the tests are successful.
Verification: PASS (1/1 checks passed)
Result:       SUCCESS
```

### Diff

```diff
diff --git a/service_layer/services.py b/service_layer/services.py
index 36c7bd9..d55183d 100644
--- a/service_layer/services.py
+++ b/service_layer/services.py
@@ -6,10 +6,12 @@ from domain import model
 from domain.model import OrderLine
 from adapters.repository import AbstractRepository
 
-
 class InvalidSku(Exception):
     pass
 
+class InvalidQuantity(Exception):
+    pass
+
 
 def is_valid_sku(sku, batches):
     return sku in {b.sku for b in batches}
@@ -31,6 +33,8 @@ def allocate(
     batches = repo.list()
     if not is_valid_sku(line.sku, batches):
         raise InvalidSku(f"Invalid sku {line.sku}")
+    if qty <= 0:
+        raise InvalidQuantity(f"Invalid quantity {qty}")
     batchref = model.allocate(line, batches)
     session.commit()
     return batchref
```

Notes on the model's fix:

- The quantity check is placed after the SKU check, not first. It still runs before
  `model.allocate` and `session.commit`, as the request requires. An unknown SKU with
  quantity 0 raises `InvalidSku` rather than `InvalidQuantity`.
- The model used `edit_file` (whole-file rewrite), which also removed one blank line
  before `class InvalidSku`. Formatting only.

## 3. Acceptance, regression, and scope after the fix

`python demo/demo.py verify`

```
=== Acceptance check (outside the agent's writable area) ===
....
4 passed
PASS

=== Regression tests (existing tests in the target) ===
........................
24 passed, 1 warning
PASS

=== Scope check (only allowed files may change) ===
allowed      service_layer/services.py
PASS

=== Demo verdict ===
acceptance  PASS
regression  PASS
scope       PASS
```

The one warning is SQLAlchemy 1.4's `RemovedIn20Warning` for `mapper()` in the
target's `adapters/orm.py`; it is unrelated to the change.

## Manual help

- Created a virtual environment and installed the target's dependencies by hand
  (`pip install -r requirements.txt -r demo/requirements-target.txt`), including
  `SQLAlchemy<2`, because the target uses `sqlalchemy.orm.mapper`, removed in 2.0.
- Excluded the target's e2e tests with `PYTEST_ADDOPTS="--ignore=tests/e2e"`; they
  need Flask and Postgres.
- Attempts: 1. The model fixed the bug on the first run; no retries and no manual
  edits to the target code.
