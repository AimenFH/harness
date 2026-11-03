# Stage 2 feature run — real model, recorded results

What this file records: the harness being run with a **real local model** against
the prepared Stage 2 feature task, and what the **protected checks** said about
the result. It is written to be read against the evidence, not instead of it.

**Headline, stated plainly: the harness worked; the feature did not land.**
Every harness behaviour the brief asks for was demonstrated on a live model run.
The acceptance check does **not** pass, because neither local model could
implement the feature correctly. Details, evidence and what is needed next are
below. Nothing here is a scripted or hand-written result.

| | |
|---|---|
| Target repository | `https://github.com/cosmicpython/code.git`, branch `chapter_05_high_gear_low_gear` |
| Starting commit | `97267473c8accfd998bb9f756034eac48fa154f6` |
| Feature task | [`demo/stage2/TASK.txt`](../stage2/TASK.txt) — optional expiry on stock reservations |
| Modules in scope | `domain/model.py`, `service_layer/services.py`, `adapters/orm.py` |
| Protected checks | 23 feature cases + 24 original upstream tests, hash-pinned, outside the agent's writable area, run in Docker |
| Models used | `qwen2.5-coder:7b` and `qwen3:8b`, both local in Ollama (no account, no API key) |
| Runs | 6, all recorded under `~/harness-demo/stage2-run/attempt-*` |
| Workspace | `~/harness-demo/stage2-run` (outside this repository) |
| Environment | [`environment.json`](environment.json) |

## How it was run

```bash
# 1. a disposable copy at the pinned commit, with the task saved alongside it
.venv/bin/python demo/stage2.py prepare  --workspace ~/harness-demo/stage2-run

# 2. prove the feature check fails on the starting code, for the right reason
.venv/bin/python demo/stage2.py baseline --workspace ~/harness-demo/stage2-run

# 3. let the harness do the work, with a real model
PYTEST_ADDOPTS="--ignore=tests/e2e" .venv/bin/python main.py start \
  --projects-file ~/harness-demo/stage2-run/approved-projects.toml \
  --session-dir   ~/harness-demo/stage2-run/sessions \
  --settings      ~/harness-demo/stage2-run/run-settings.toml \
  --project cosmic-expiry --task "$(cat demo/stage2/TASK.txt)" \
  --mode edit --model qwen2.5-coder:7b --check pytest \
  --record ~/harness-demo/stage2-run/task-record.json \
  --allow domain/model.py --allow service_layer/services.py --allow adapters/orm.py \
  --acceptance "$PWD/demo/stage2/acceptance/acceptance_expiry.py" \
  --acceptance "$PWD/demo/stage2/regression"

# 4. judge the result with the protected checks, independently
.venv/bin/python demo/stage2.py verify   --workspace ~/harness-demo/stage2-run
```

The exact settings used are in `run-settings.toml`, copied into each attempt's
folder. `PYTEST_ADDOPTS` is the one host variable the sandbox forwards; it
excludes the target's e2e tests, which need a live Flask app and Postgres.

## Step 2 — the baseline holds

`demo/stage2.py baseline` on the untouched starting commit:

```text
acceptance: FAIL; exit=1; counts=23   ->  22 failed, 1 passed
regression: PASS; exit=0; counts=24   ->  24 passed
Baseline proof: PASS. Feature acceptance: FAIL (expected=True);
legacy regression: PASS; target unchanged=True.
```

All 22 failures are the same assertion — `AssertionError: MISSING FEATURE:
services.allocate does not accept expires_at; ... services.InvalidExpiry is
missing` — and the one pass is the legacy control, which proves the environment
works and the failures are feature absence. Evidence:
[`demo/stage2/baseline/`](../stage2/baseline/).

## Step 3 — five runs, and what each one found

The runs are kept in full under `~/harness-demo/stage2-run/attempt-*`. They are
listed here because the first three failures were **harness defects that only a
live model exposed**, and fixing them is the most substantial engineering in
this phase.

| # | Model | Outcome | What it exposed |
|---|---|---|---|
| 1 | `qwen2.5-coder:7b` | `model error: Ollama did not answer within 420 seconds` at action 6 | **No cap on reply length.** The model chose to rewrite a whole file and generated until the context window was filled — ≈12 000 tokens, ≈520 s. This was also the cause of the two earlier failed runs recorded in `stage2-real/`. The conversation was only 17 367 characters, well inside the budget: the problem was the reply, not the prompt. |
| 2 | `qwen2.5-coder:7b` | `3 malformed or denied requests in a row (limit 3)` at action 8 | With `max_reply_tokens` added, the hang became an honest stop. But each cut-off reply was **echoed back in full** — 4 749 characters of a half-written file, twice — which ate the budget and kept the model writing the same file. |
| 3 | `qwen2.5-coder:7b` | `the model asked 3 times for an action that had already failed` at action 15 | With the echo shortened to 200 characters, the model recovered on the **first** retry and edited two modules. Then it re-asked for an edit it had already made. Cause: compression records an edit as `old=<91 characters>`, so **the model had no record of what it had already changed**. |
| 4 | `qwen2.5-coder:7b` | `the model asked 3 times for an action that had already failed` at action 29 | With the trusted progress note added, it edited **all three** modules, ran the check, and worked the failure. It then wrote invalid Python into `adapters/orm.py` and could not repair it. **Best run; judged below.** |
| 5 | `qwen3:8b` | `3 malformed or denied requests in a row (limit 3)` at action 7 | Never made an edit. Three replies of 3 000 tokens each with **empty content**: `max_reply_tokens` counts generated tokens, and a reasoning model can spend the entire limit on hidden thinking. The feedback for that case now says so instead of advising about whole files. |
| 6 | `qwen2.5-coder:7b` | `action limit reached (60/60)` | Run with the new **syntax gate** (a write may not leave a Python file unparsable). The gate **fired once and worked** — one breaking edit refused, the file kept working, where run 4 had been stopped dead by the same mistake. The model then found a different way to waste the run: an identical `replace_in_file` applied **22 times**, succeeding every time. See below. |

Each fix is described in [`docs/stage2-progress.md`](../../docs/stage2-progress.md)
under "Phase 3 follow-up", with the evidence that motivated it.

### Run 6: the gate worked, and uncovered the next defect

The syntax gate did what it was built for. It refused one breaking edit —

```text
Refused: this change would leave 'adapters/orm.py' with invalid Python
(line 43: unmatched ')'). The file was NOT changed.
```

— and `adapters/orm.py` stayed parsable for the rest of the run, where in run 4
the same mistake had ended it. But the model then produced this
`replace_in_file`, and the harness applied it **22 times**:

```text
old = 'mapper(model.OrderLine, order_lines,'
new = 'mapper(model.OrderLine, order_lines, properties={\n ... \n    }),\n    mapper(model.Batch, batches,'
```

The `new` text **contains** the `old` text. So after each replacement `old` is
still present exactly once, the identical call succeeds again, and another
duplicate block is appended. `adapters/orm.py` grew to 120 lines of repeated
`mapper(model.Batch, batches, ...)`, every `run_check` in between failed, and
the run burned all 60 actions.

Nothing stopped it, and the reason is a genuine gap in the stopping rules:
`failed_actions` only remembers actions that **failed**, and `_record_change()`
clears that memory and resets the repeat counter whenever a file really changes
— which this action did, every time. **The no-progress rule covers futile
*failed* actions but not futile *successful* ones.** The result was
syntactically valid, so the new gate had no reason to fire either.

This is the one harness defect from these runs that is still open. It is listed
under "what is needed" below.

## Step 4 — the protected checks judge the runs

### Run 6 (latest)

```text
acceptance: FAIL; exit=1; counts=23  ->  1 passed, 20 failed, 2 errors
regression: FAIL; exit=1; counts=24
Scope:      PASS; changed=['adapters/orm.py', 'domain/model.py', 'service_layer/services.py']
```

Both suites now **collect and run** — an improvement on run 4, which could not
get that far — so these are real test results rather than collection errors. The
20 failures are all still `MISSING FEATURE`, naming `Batch.release_expired`,
`services.release_expired` and `services.InvalidExpiry`. The one pass is the
legacy control. Regression fails because the duplicated ORM mappers break the
original tests.

In six runs the model added *signatures* — `expires_at` on `OrderLine` and both
`allocate` functions, `now` on the service, a nullable `expires_at` column — and
never reached the *behaviour* the feature actually consists of.

### Run 4 (for comparison)

Run 4's resulting code, applied to a clean copy of the starting commit and
judged by `demo/stage2.py verify` (contained, read-only, no network):

```text
acceptance: FAIL; exit=2; counts=1; sandbox=docker (no network, read-only repository)
regression: FAIL; exit=4; counts=0; sandbox=docker (no network, read-only repository)
Scope:      PASS; changed=['adapters/orm.py', 'domain/model.py', 'service_layer/services.py']
verify_exit=1
```

- **Scope passes.** Only the three allowed modules were changed, and at least
  two of them, which is what the task required. The harness refused an attempt
  to edit `tests/conftest.py`.
- **Acceptance and regression fail**, both for the same reason: the model left
  `adapters/orm.py` with `SyntaxError: unmatched ')'` at line 43, so pytest
  cannot even collect. Exit 2 and exit 4 are collection failures, not test
  failures. The harness reported this as FAILED; **zero tests never counts as a
  pass**, which is exactly the policy that stops a broken edit looking like a
  clean run.

What the model did get right, before breaking the file: `expires_at` on
`OrderLine` and on `domain.allocate`, `expires_at` and `now` on
`services.allocate`, and a nullable `expires_at` date column in the ORM table.
What is missing: `Batch.release_expired`, `services.release_expired`,
`services.InvalidExpiry`, passing `expires_at` into the `OrderLine` the service
builds, and the mapper change that does not parse. Diff:
`attempt-4-qwen2.5-coder/partial.diff`.

## What this run does demonstrate

Every one of these is from the live runs above, not from the test suite:

| Behaviour | Evidence |
|---|---|
| Navigation finds the right code | run 4 actions 1–5: `repo_map`, `find_symbol allocate`, then the three modules the task names — no wasted reads |
| Context selection | `[context] suggested files: domain/model.py, service_layer/services.py, ..., adapters/orm.py` — the three in-scope modules ranked top |
| The size limit works under load | `[context] history over budget: 36161 characters in 40 messages -> 16346 in 10 (budget 24000, 32 older steps shortened)`, on every request from action 13 on, with the task card and progress note verbatim throughout |
| Scope enforced in application code | action 21: `DENIED ... tests/conftest.py is outside the allowed changes for this task`; the file is untouched in the diff |
| Failed checks guide fixes | action 13 `FAILED (exit code 1)` → the model edits → action 15 `FAILED (no tests were run, exit code 4)` → it re-reads and edits again |
| Contained execution | every check `sandbox=docker (image harness-sandbox, no network, read-only repository)` |
| Repository text kept untrusted | every tool result arrives inside `----- BEGIN UNTRUSTED REPOSITORY DATA: <tool> -----` markers |
| Bounded retries, clear stopping rule | all five runs stopped on a named rule with a reason in words — never a hang, never a silent failure |
| A write may not break a file | run 6: `Refused: this change would leave 'adapters/orm.py' with invalid Python (line 43: unmatched ')'). The file was NOT changed.` — and the file stayed parsable for the remaining 44 actions |
| Nothing passes that did not pass | final status `unfinished`, `Verification: FAIL`, and the saved acceptance commands reported as unfinished work rather than assumed |
| Saved sessions | `attempt-*/sessions/*.json` reopen with `main.py show <id> --session-dir ...` after the process has ended |

## What is not done, and what is needed

**The feature is not implemented and the acceptance check does not pass.** The
cause is model capability, not harness behaviour: a 7B local coder model could
not write this feature correctly, and the 8B reasoning model could not keep to
the one-action-per-reply format at all.

Options for Part 3, in the order I would try them:

1. **Stop a futile *successful* action, not just a futile failed one.** Run 6's
   open defect. An identical `(tool, arguments)` that has already been applied
   should be refused — "you already made this exact change" — and counted like
   the existing repeat denials. With `replace_in_file` an identical repeat is
   normally harmless because `old` no longer matches; it only succeeds when
   `new` contains `old`, which is precisely the self-perpetuating case. This is
   small, mirrors the existing `failed_actions` design, and would have saved 44
   of run 6's 60 actions.
2. **A stronger model.** Six runs in, every harness fix has exposed the next
   model weakness rather than a harness one, and the model has never reached the
   feature's behaviour. The same task on a larger model is the control that
   would settle whether anything is left to fix in the harness at all.
3. **Smaller task steps.** The task asks for five related API changes at once.
   Splitting it would suit a small local model, at the cost of being a weaker
   demonstration of the harness.

The syntax gate that was first on this list has been built; it is item 5 under
"Resource Limits" in the main README, and run 6 shows it working.

## Manual help, stated for the record

- The task text, the 23 acceptance cases and the 24-test regression gate were
  written by hand, before any run, and are hash-pinned. The agent cannot read or
  change them, and never did.
- The three harness defects in runs 1–3 were diagnosed and fixed **by hand**,
  between runs. One diagnosis replayed run 1's saved conversation against Ollama
  with a token cap to confirm the cause; that script is kept at
  `attempt-1-no-reply-cap/replay-probe.py`.
- The feature code in run 4's diff is **entirely the model's own**. No feature
  code was written, corrected or completed by hand, and no reference patch was
  applied. The target was reset to the starting commit between runs with
  `git checkout -- .` and nothing else.
- Run 4's diff was re-applied to a clean copy of the starting commit so the
  protected checks could judge it; applying a saved diff changes no code.
- The choice of `max_reply_tokens = 3000` for run 5 was a manual, documented
  change from the shipped default of 1200, made because a reasoning model's
  thinking counts towards the limit.
