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
| `src/inferno/` | The agent package (not built yet). |
| `scripts/setup_quixbugs.sh` | Clones QuixBugs into `data/quixbugs` and moves `correct_python_programs` and `correct_java_programs` to `~/inferno-private`. Idempotent. Set `QUIXBUGS_REF=<commit>` to pin a version. |
| `data/quixbugs/` | QuixBugs checkout (gitignored, not a submodule). |
| `results/` | Committed test baselines. |
| `.env.example` | Template for `.env` (gitignored), which holds `TOGETHER_API_KEY`. |

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
