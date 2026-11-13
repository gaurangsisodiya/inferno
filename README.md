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

Run the agent's own tests with `uv run pytest`.

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
