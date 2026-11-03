# Stage 2 feature runs — raw evidence

The artefacts behind [`../RESULTS.md`](../RESULTS.md). Seven real-model runs of
the harness against the prepared Stage 2 feature task, copied here from the
external workspace (`~/harness-demo/stage2-run/`) so they can be read from a
fresh clone of this repository.

**Nothing here was edited except one substitution: the absolute home directory
was replaced with `~`.** Runner `.lock` files were left out (they are empty
artefacts of the one-runner-per-session lock). Everything else is byte-for-byte
what the harness wrote at the time.

## What each run shows

| Run | Model | Recorded status | Actions | What it exposed |
|---|---|---|---|---|
| [`attempt-1-no-reply-cap`](attempt-1-no-reply-cap/) | `qwen2.5-coder:7b` | `error` | 6 | No cap on reply length: the model generated until the context window filled. `replay-probe.py` is the by-hand diagnosis that confirmed the cause. |
| [`attempt-2-full-echo`](attempt-2-full-echo/) | `qwen2.5-coder:7b` | `retry_limit_reached` | 8 | A cut-off reply echoed back in full ate the context budget. |
| [`attempt-3-no-progress-note`](attempt-3-no-progress-note/) | `qwen2.5-coder:7b` | `retry_limit_reached` | 15 | Compression measured bulky edit arguments, so the model had no record of what it had already changed. |
| [`attempt-4-qwen2.5-coder`](attempt-4-qwen2.5-coder/) | `qwen2.5-coder:7b` | `retry_limit_reached` | 29 | **Best run.** Edited all three modules, ran checks, worked the failure; then wrote unparsable Python and could not repair it. Judged by the protected checks (`verify.log`). |
| [`attempt-5-qwen3`](attempt-5-qwen3/) | `qwen3:8b` | `retry_limit_reached` | 7 | A reasoning model spends `max_reply_tokens` on hidden thinking and returns empty content. |
| [`attempt-6-syntax-gate`](attempt-6-syntax-gate/) | `qwen2.5-coder:7b` | `action_limit_reached` | 60 | The syntax gate fired and worked; then an identical `replace_in_file` succeeded 22 times and burned the action limit. |
| [`attempt-7-both-rules`](attempt-7-both-rules/) | `qwen2.5-coder:7b` | `retry_limit_reached` | 31 | The no-futile-repeat rule held: the growth edit applied **once**, the repeat denied at action 17. |

Every `status` and action count above is read straight out of that run's
`task-record.json`, and every one matches the narrative in `../RESULTS.md`.

## Files in a run folder

| File | What it is |
|---|---|
| `harness.log` | the full terminal output of the run: progress lines, `[context]` decisions, denials, check verdicts |
| `task-record.json` | the task specification plus every action with its outcome and detail (`--record`) |
| `sessions/<id>.json` | the saved session: checkpoint, conversation, counters, final result |
| `harness-exit.txt` | the process exit code |
| `partial.diff` | what the model left in the target when the run stopped |
| `acceptance.{log,json}` | the 23 protected feature cases judging that run |
| `regression.{log,json}` | the 24 original upstream tests judging that run |
| `verify.log` | `demo/stage2.py verify` — acceptance + regression + scope together |
| `run-settings.toml` | the exact settings the run used |

## Reading a saved session

The sessions still open after the fact, which is requirement **D1/H3** — the
session is rendered from the saved JSON and needs neither the target repository
nor a model:

```bash
.venv/bin/python main.py show 20261101-012730-0bc82ccf \
  --session-dir demo/results-stage2/runs/attempt-4-qwen2.5-coder/sessions
```

Because the home prefix was redacted, the recorded `root` path in these copies
no longer resolves on disk. That does not affect `show` or `status`, which read
only persisted data. It does mean these copies are for **inspection, not for
resuming**; a resume would correctly refuse them (the project is not approved and
the saved working copy is not there), which is requirement **D6** doing its job.

## What the evidence does and does not prove

**Does:** the harness navigated to the right modules on its own. Run 4's first
five actions, straight from `attempt-4-qwen2.5-coder/task-record.json`:

```text
1 OK  repo_map {}
2 OK  find_symbol {'name': 'allocate'}
3 OK  read_file {'path': 'domain/model.py'}
4 OK  read_file {'path': 'service_layer/services.py'}
5 OK  read_file {'path': 'adapters/orm.py'}
```

No file paths or line numbers were handed to it as edit instructions — it mapped
the repository, found the symbol, then read the three modules. The task text does
name those three files as its *scope* (a saved task must record its allowed
changes), so navigation corroborates rather than discovers from nothing.

Scope enforcement, also from run 4's record:

```text
21 DENIED replace_in_file tests/conftest.py
   Permission denied: tests/conftest.py is outside the allowed changes for this task
```

**Does not:** show the feature passing. No run gets the acceptance suite to
green — the best is run 6 at 1 passed / 20 failed / 2 errors. See
[`../RESULTS.md`](../RESULTS.md) for the per-run analysis and
[`../../../STAGE2_AUDIT.md`](../../../STAGE2_AUDIT.md) for what that leaves open.
