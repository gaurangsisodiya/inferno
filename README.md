# inferno

An LLM agent that repairs buggy programs. Starting with
[QuixBugs](https://github.com/jkoppel/QuixBugs) (Python), with SWE-bench planned later.

## Setup

```bash
uv sync                          # creates .venv with Python 3.12 + dev deps
cp .env.example .env             # then set TOGETHER_API_KEY
scripts/setup_quixbugs.sh        # clones QuixBugs into data/quixbugs and
                                 # moves the reference solutions out of the repo
```

## Layout

| Path | Purpose |
| --- | --- |
| `src/inferno/client.py` | Together AI wrapper (OpenAI-compatible client), token counting, cost estimate. |
| `src/inferno/sandbox.py` | Runs one program's tests in a locked-down Docker container. |
| `src/inferno/loop.py` | The repair loop and its command-line entry point. |
| `src/inferno/run_all.py` | Batch runner: repairs every program, writes `results/runs/<run_id>.jsonl`. |
| `src/inferno/rename.py` | Neutral renaming of a program for the memorization check (`run_all --renamed`). |
| `src/inferno/properties/` | Hypothesis properties, `prop_<program>.py`, for 10 programs. |
| `src/inferno/validate.py`, `validate_run.py` | Re-check a run's fixes with original tests + properties, writes `results/validated/<run_id>.jsonl`. |
| `scripts/check_properties.py` | Checks each property file passes on the reference version and fails on the buggy one. |
| `docker/sandbox.Dockerfile` | Sandbox image (Python 3.12 + pytest), built automatically on first use. |
| `tests/` | Tests for the agent itself (model and Docker are mocked). |
| `scripts/setup_quixbugs.sh` | Clones QuixBugs into `data/quixbugs` and moves `correct_python_programs` and `correct_java_programs` to `~/inferno-private`. Idempotent. Set `QUIXBUGS_REF=<commit>` to pin a version. |
| `data/quixbugs/` | QuixBugs checkout (gitignored, not a submodule). |
| `results/` | Committed test baselines. |
| `.env.example` | Template for `.env` (gitignored), which holds `TOGETHER_API_KEY`. |

## Repairing a program

Requires Docker (on WSL: enable Docker Desktop's WSL integration) and `TOGETHER_API_KEY` in `.env`.

```bash
uv run python -m inferno.loop --program bitcount
uv run python -m inferno.loop --program gcd --max-attempts 1 --model openai/gpt-oss-120b
```

It prints the diff of the final candidate and a JSON record with `program`, `model`, `attempts`,
`passed`, `tokens_in`, `tokens_out` and `cost_usd`. The model defaults to `INFERNO_MODEL`, or
`openai/gpt-oss-120b` if that isn't set. The exit code is 0 if the repair passed.

The sandbox runs with `--network none`, a 512 MB memory limit, a read-only root filesystem and a
non-root user. Each test has a 5s timeout, and the whole run has a hard 120s limit. Only the
candidate's workspace and that program's QuixBugs test files are mounted, all read-only.
Any mount overlapping `~/inferno-private` is refused.

## Repairing every program

```bash
uv run python -m inferno.run_all                      # all 40 programs
uv run python -m inferno.run_all --run-id smoke --limit 3
uv run python -m inferno.run_all --programs gcd,bitcount
```

This runs the repair loop on each program that has a test file, one at a time. It appends one JSON
line per program to `results/runs/<run_id>.jsonl` as soon as that program finishes. The default
`--run-id` is `<model>_<date>`. Re-running with the same `--run-id` skips the programs already in
the file. `--limit N` runs at most N programs that aren't done yet. A program whose repair raised an
error (e.g. an API failure) isn't written to the file, so the next run retries it.

Each line records `program`, `model`, `attempts`, `passed`, `fixed_first_try`, `tokens_in`,
`tokens_out`, `cost_usd`, `seconds`, `final_source` (the last candidate tested), `git_commit`,
`git_dirty`, `quixbugs_commit` and `finished_at`. At the end it prints a summary of the whole file:
fixed on the first try, fixed within 3 tries, total tokens and total cost. Commit before a real run
so that `git_commit` identifies the code. The runner warns you if the tree is dirty.

## Memorization check (renamed programs)

QuixBugs is public, so a model may recognise a program instead of reasoning about it.
`--renamed` repeats the run on programs with neutral identifiers:

```bash
uv run python -m inferno.run_all --renamed            # run id: <model>_<date>_renamed
```

`src/inferno/rename.py` uses `ast` to rename the main function to `f_1`, other functions to `f_2...`,
parameters to `a_1...` and other variables to `v_1...`. It is scope-aware: builtins, imported names
and attributes are never renamed. Comments and docstrings are dropped. The model sees the program as
`f_1.py`, and test output has the original program/function name (incl. test names and file paths)
replaced by `f_1`. Before each test run this alias line is appended to the candidate; the model
never sees it:

```python
bitcount = f_1; bitcount.__name__ = bitcount.__qualname__ = "bitcount"
```

It restores `__name__` too, because most QuixBugs tests load their data with
`load_json_testcases(<function>.__name__)`. Result lines have `renamed: true` and the `alias`;
`final_source` is the model's renamed code without the alias, and `validate_run` appends the alias
again when validating. Programs the renamer can't handle (classes, `global`, ...) are skipped and
listed; none of the 40 currently need skipping.

## Validating fixes with properties

QuixBugs has only a few test cases per program, so a fix can pass them and still be wrong.
`src/inferno/properties/prop_<program>.py` holds Hypothesis properties for 10 programs: bitcount,
bucketsort, flatten, gcd, is_valid_parenthesization, kheapsort, powerset, quicksort, sqrt and
to_base. Each property is a general statement about the output (e.g. "sorted and a permutation of
the input"), checked on 200 generated inputs.

```bash
uv run python -m inferno.validate_run --run-id gptoss-v1
```

For each line of `results/runs/<run_id>.jsonl`, this takes `final_source`, runs the original
tests and the properties in the sandbox, and appends to `results/validated/<run_id>.jsonl`:
`passes_original`, `passes_properties` (`null` if the program has no properties), `valid` (`null`
when the originals pass but there are no properties), `property_output` on failure, and
`git_commit`. It is resumable like `run_all`. The summary lists the plausible-but-wrong fixes:
programs that pass the original tests but fail the properties.

Check the properties themselves with:

```bash
uv run python scripts/check_properties.py
```

Every property file must pass on the reference version and fail on the buggy one. This runs on the
host, importing the reference programs from `~/inferno-private` via `PYTHONPATH`. It prints only
pass/fail, never the reference code.

Run the agent's own tests with `uv run pytest`.

## SWE-bench Verified (mini-swe-agent)

`data/swebench_25.json` holds a fixed sample of 25 Verified tasks (seed 42,
at most 8 per repo), made by `uv run python -m inferno.swebench_tasks`.

```bash
uv run python -m inferno.swebench_run --model together_ai/openai/gpt-oss-120b --run-id gptoss-swe-v1
```

The harness is mini-swe-agent's `swebench_backticks.yaml` config with the
`litellm_textbased` model class: one ```` ```mswea_bash_command ```` block per
reply, no tool calling (gpt-oss on Together produced invalid parallel tool
calls under the default `swebench.yaml`). On top of mini's prompt we append
strict command-format rules (real line breaks, no `\n`/`\"` escapes or JSON,
closing fence on its own line, no `apply_patch`) to the instance and
format-error templates, and allow 5 consecutive format errors instead of 3.
Each result line records `harness` (currently `backticks-v2`), `agent_config`
and `model_class`; a run file never mixes harnesses.

Tasks run one at a time in mini-swe-agent's Docker environment, with a
per-task cost cap (`--cost-cap`, default $0.50), step limit (`--step-limit`,
50) and wall-clock timeout (`--timeout`, 1200 s). After every task, including
errors and timeouts, its containers are removed and its image is `docker rmi`'d,
so only one task image is on disk at a time. Results:

* `results/swebench/<run_id>.jsonl`: one line per task (exit status,
  `outcome` = submitted / limit / error, patch, steps, tokens, cost, seconds,
  git commit). Re-running skips finished tasks; `--retry-errors` reruns errors.
* `results/swebench/<run_id>.preds.json`: predictions for `swebench eval`.
* `results/swebench/<run_id>/`: each task's mini-swe-agent trajectory.

Cost comes from litellm's price or, failing that, our own table; a call
with no known price makes the task's `cost_usd` null (never a silent 0), and
the run refuses to start for such a model unless `--allow-unknown-cost`.
litellm's prices are the list bundled with its pinned version (uv.lock), not
one downloaded at startup (`LITELLM_LOCAL_MODEL_COST_MAP=True` is set by the
runner and checked in the worker). Each line records `litellm_version`,
`price_map_source`/`price_map_revision`, and `price_source` (litellm or table).

## Running the QuixBugs tests

Run the tests from inside `data/quixbugs`. Its `conftest.py` puts that directory on `sys.path`,
so test files can `import python_programs.<name>`:

```bash
cd data/quixbugs
uv run --project ../.. pytest python_testcases --timeout=10 -q
```

Always pass `--timeout`, because some buggy programs loop forever or recurse until they crash.
Pass `--runslow` to include the two slow knapsack cases that are skipped by default.

Baselines (QuixBugs commit `4257f44`, 10s per-test timeout):

| Programs | Result | File |
| --- | --- | --- |
| correct | 276 passed, 2 skipped | `results/baseline_correct.txt` |
| buggy | 187 failed, 89 passed, 2 skipped | `results/baseline_buggy.txt` |

## Keeping the reference solutions private

`scripts/setup_quixbugs.sh` moves both sets of reference solutions out of the repo
(override the destination with `INFERNO_PRIVATE=...`):

- `correct_python_programs/` → `~/inferno-private/correct_python_programs`
- `correct_java_programs/` → `~/inferno-private/correct_java_programs`

This way the agent's view of the repo never includes the answers.

### Running `--correct` tests anyway

The correct programs are a namespace package (no `__init__.py`), so putting the private
directory on `PYTHONPATH` makes them importable again:

```bash
cd data/quixbugs
PYTHONPATH=~/inferno-private uv run --project ../.. pytest python_testcases --correct --timeout=10 -q
```

If you run `--correct` without that `PYTHONPATH`, every test module fails at import (40 errors).

Do not give the agent this `PYTHONPATH` or access to `~/inferno-private`.

The Java correct-version tests (`java_testcases/junit/crt_program/`) reference
`correct_java_programs` and won't compile without it. This project doesn't run the Java tests.
