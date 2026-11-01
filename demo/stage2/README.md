# Stage 2 feature demo: expiry on stock reservations

This phase prepares tests and the task only. **The feature has not been
implemented.** The disposable target is still the exact starting commit. The
next phase must use the AI harness with a real model to implement it.

## Actual target architecture and interface

Repository: https://github.com/cosmicpython/code.git, branch
`chapter_05_high_gear_low_gear`, full starting commit
`97267473c8accfd998bb9f756034eac48fa154f6`.

Inspected the source at that commit before choosing the contract:

| Module | Existing responsibility | Why the feature crosses it |
|---|---|---|
| `domain/model.py` | Hashable `OrderLine`; `Batch._allocations` is a set; available stock is purchased stock minus allocated quantities. `deallocate` already ignores missing lines. | Optional expiry metadata and deterministic release of expired reservations, preserving set/idempotency behavior. |
| `service_layer/services.py` | Builds an order line, loads `repo.list()`, checks SKU, delegates domain allocation and commits. | Compatible optional arguments, clear validation, release across batches, transaction boundary. |
| `adapters/orm.py` | SQLAlchemy imperative mappings; order lines and allocation relationships persist in SQLite/Postgres. | Nullable expiry date must survive reload and remain backward compatible with legacy rows. |
| `adapters/repository.py` | Existing `list()` returns all batches; `get()` reloads one batch with its allocations. | Reuse without changing this module. |

The contract uses **calendar dates** (`datetime.date`), matching the target's
existing date-based domain. Datetime timestamps are rejected, avoiding mixed
naive/aware comparisons and unspecified time zones. Tests use fixed dates and an
injected clock; no sleeps or reliance on the real current date for expiry/release.

- `OrderLine(orderid, sku, qty, expires_at=None)` keeps legacy construction valid.
- `services.allocate(orderid, sku, qty, repo, session, expires_at=None, *, now=None)`
  preserves the old five-argument calls. `now=None` uses `date.today()`; an injected
  date controls validation. A supplied expiry must be strictly later than `now`.
- Invalid expiry/clock values raise `services.InvalidExpiry` with a clear message
  before stock or commit changes. `None` is valid as an omitted expiry.
- `Batch.release_expired(now)` returns a reservation count.
- `services.release_expired(now, repo, session)` returns the total released
  reservation count across batches and commits once. `now` is a required date;
  release includes reservations with `expires_at <= now`.
- Permanent/future reservations survive. Repeating release returns zero for an
  already released reservation; stock can be reused without inflation.
- Expiry and release survive ORM reload; old rows without an expiry remain valid.
  A fresh demo database is used; production schema migration is outside this task.

Full task: [TASK.txt](TASK.txt). Allowed changes are exactly the three modules
`domain/model.py`, `service_layer/services.py`, and `adapters/orm.py`; final scope
verification requires changes to at least two. Tests, dependencies, entrypoints,
configuration, and Git metadata may not be changed.

## Protected tests and provenance

- [acceptance/acceptance_expiry.py](acceptance/acceptance_expiry.py): 23 cases,
  including one legacy control. Covers optional expiry, domain/service behavior,
  inclusive boundary, permanent/future reservations, multiple batches/SKUs,
  invalid dates/types/clocks, stock reuse, repeated allocation/release, and ORM
  reload/legacy rows.
- [regression/SOURCE.json](regression/SOURCE.json): hashes of seven **unmodified**
  source files copied from the exact starting commit, containing all 24 original
  unit/integration tests and their fixtures. The original Cosmic Python license
  text is included in [regression/LICENSE.cosmicpython](regression/LICENSE.cosmicpython).
  E2E tests require a web server/Postgres and are not part of this SQLite baseline;
  they are neither collected nor counted as passing/skipped checks.
- [CHECKS.sha256.json](CHECKS.sha256.json): hashes of the protected task, acceptance,
  regression, fixture and container-runner files. Checks refuse changed inputs.
- All of these files live in the harness repository, outside the agent's target
  root. Docker mounts them read-only at `/protected`; the target is mounted
  read-only at `/work` for checks. The model's write tools see only the disposable
  target copy during a harness run.
- The trusted runner ignores `PYTEST_ADDOPTS` and repository pytest configuration,
  disables plugin autoload, uses its own protected fixtures, and checks JUnit
  counts. Missing tools, import/collection errors, skipped checks, zero tests,
  timeouts and unrelated failures cannot establish the expected baseline failure.
- The parent collection guard keeps target tests out of host harness pytest
  collection. Target application modules and checks run only inside Docker.

## Reproduce this preparation and baseline

From the harness repository, with the existing environment and Docker running:

```bash
docker build -t harness-sandbox -f sandbox/Dockerfile .
.venv/bin/python demo/stage2.py prepare
.venv/bin/python demo/stage2.py baseline
```

A separate Stage 2 copy is created at `~/harness-demo/stage2/cosmic`, leaving the
Stage 1 copy at `~/harness-demo/cosmic` alone. The clone has no remotes. `prepare`
reuses only an unchanged copy at the exact starting commit and **refuses to erase
existing work**. Use `--workspace /another/external/directory` for a new disposable
workspace. It creates outside the target:

- `task.json`: actual harness `TaskSpec`, request, full commit, allowed paths,
  checks, limits and exact acceptance/regression/scope commands.
- `approved-projects.toml`: only the `cosmic-expiry` disposable project is approved.
- `demo-settings.toml`: bounded run settings for the subsequent model demonstration.
- `baseline.json`, `acceptance.json`/`.log`, `regression.json`/`.log`: actual evidence.

`baseline` succeeds only when the target is clean before and after, the 22 feature
cases fail specifically with `MISSING FEATURE` assertions, the legacy control
passes, and all 24 protected regression checks pass. Its zero exit code means
**the baseline was demonstrated**, not that feature acceptance passed. Running
`acceptance` itself currently exits 1, correctly reporting a failed feature suite.

Recorded evidence: [baseline/proof.json](baseline/proof.json),
[baseline/task.json](baseline/task.json), [baseline/environment.json](baseline/environment.json),
and the full logs in [baseline/](baseline/). No manual feature implementation,
scripted reference patch, or real-model run was performed in this preparation
phase. The real-model run that followed it is in
[../results-stage2/RESULTS.md](../results-stage2/RESULTS.md).

## Next phase only: let the harness implement it

The following is documented for the next phase; it has **not been run**:

```bash
PYTEST_ADDOPTS="--ignore=tests/e2e" .venv/bin/python main.py start \
  --projects-file "$HOME/harness-demo/stage2/approved-projects.toml" \
  --session-dir "$HOME/harness-demo/stage2/sessions" \
  --settings "$HOME/harness-demo/stage2/demo-settings.toml" \
  --project cosmic-expiry --task "$(cat demo/stage2/TASK.txt)" \
  --mode edit --model qwen2.5-coder:7b --check pytest \
  --allow domain/model.py --allow service_layer/services.py --allow adapters/orm.py \
  --acceptance "$PWD/demo/stage2/acceptance/acceptance_expiry.py" \
  --acceptance "$PWD/demo/stage2/regression"

# Independent checks after the harness has made its changes:
.venv/bin/python demo/stage2.py verify
```

The model name may be selected for the real run. Existing target tests are useful
feedback for the harness, but only the external protected checks prove the feature.
`verify` requires acceptance, protected regression, and the multi-module scope gate
to pass. The harness verifier currently reports externally recorded acceptance as
unfinished until checked separately; never equate the model's `done` with feature
acceptance. Record the actual model run, final diff, test results and any manual
help in the next phase. Do not use the Stage 1 `rehearse` command for this feature.
