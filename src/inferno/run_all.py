"""Batch runner: repair every QuixBugs Python program and log one JSON line each.

    uv run python -m inferno.run_all
    uv run python -m inferno.run_all --run-id smoke --limit 3
    uv run python -m inferno.run_all --programs gcd,bitcount

With --renamed (memorization check), each program is renamed to neutral
identifiers first (see inferno.rename); the default run id gets a
"_renamed" suffix. Programs that can't be renamed are skipped and listed.

Results go to results/runs/<run_id>.jsonl. Each line is written (and flushed
to disk) as soon as that program finishes, so the run can be interrupted at
any point. Re-running with the same --run-id skips programs already in the
file and runs only the rest. The summary at the end covers the whole file.

A program whose repair raised an exception (e.g. an API error) is NOT written,
so the next run with the same --run-id retries it.
"""

from __future__ import annotations

import argparse
import datetime as dt
import json
import os
import re
import subprocess
import sys
import time
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Callable, Iterable

from inferno import sandbox
from inferno.client import REPO_ROOT, TogetherClient
from inferno.loop import MAX_ATTEMPTS, TestRunner, repair
from inferno.rename import RenameError, rename_program

RUNS_DIR = REPO_ROOT / "results" / "runs"


def list_programs(quixbugs_dir: Path = sandbox.QUIXBUGS_DIR) -> list[str]:
    """Programs that have both a buggy file and a test file (40 in QuixBugs), sorted."""
    tests = (quixbugs_dir / "python_testcases").glob("test_*.py")
    names = (p.stem.removeprefix("test_") for p in tests)
    return sorted(n for n in names if (quixbugs_dir / "python_programs" / f"{n}.py").exists())


def default_run_id(model: str, today: dt.date | None = None, renamed: bool = False) -> str:
    """e.g. 'openai-gpt-oss-120b_2026-11-14'. Slashes etc. become '-' so it's a valid filename."""
    today = today or dt.date.today()
    suffix = "_renamed" if renamed else ""
    return f"{re.sub(r'[^A-Za-z0-9._-]+', '-', model)}_{today.isoformat()}{suffix}"


def git_info(repo: Path = REPO_ROOT) -> tuple[str | None, bool | None]:
    """(HEAD commit hash, whether tracked files have uncommitted changes). (None, None) outside git."""
    try:
        commit = subprocess.run(
            ["git", "-C", str(repo), "rev-parse", "HEAD"],
            capture_output=True, text=True, check=True,
        ).stdout.strip()
        status = subprocess.run(
            ["git", "-C", str(repo), "status", "--porcelain", "--untracked-files=no"],
            capture_output=True, text=True, check=True,
        ).stdout
    except (OSError, subprocess.CalledProcessError):
        return None, None
    return commit, bool(status.strip())


def load_results(path: Path) -> list[dict]:
    """Read the finished results from a run file.

    A line that isn't valid JSON (e.g. half-written when the process was
    killed) is skipped with a warning, so that program is run again.
    """
    if not path.exists():
        return []
    results = []
    for n, line in enumerate(path.read_text().splitlines(), 1):
        if not line.strip():
            continue
        try:
            results.append(json.loads(line))
        except json.JSONDecodeError:
            print(f"warning: {path.name}:{n} is not valid JSON; ignoring it", file=sys.stderr)
    return results


def append_result(path: Path, record: dict) -> None:
    """Append one JSON line and force it to disk before returning."""
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a") as f:
        # If an earlier run was killed mid-line, start on a fresh line.
        if f.tell() > 0 and not path.read_bytes().endswith(b"\n"):
            f.write("\n")
        f.write(json.dumps(record) + "\n")
        f.flush()
        os.fsync(f.fileno())


@dataclass
class RunOutcome:
    errored: list[str] = field(default_factory=list)  # repair raised; not recorded, retried next time
    rename_failed: dict[str, str] = field(default_factory=dict)  # program -> reason (--renamed only)


def run_all(
    programs: Iterable[str],
    run_file: Path,
    make_client: Callable[[], TogetherClient],
    run_tests: TestRunner = sandbox.run_tests,
    max_attempts: int = MAX_ATTEMPTS,
    quixbugs_dir: Path = sandbox.QUIXBUGS_DIR,
    git_commit: str | None = None,
    git_dirty: bool | None = None,
    quixbugs_commit: str | None = None,
    limit: int | None = None,
    renamed: bool = False,
) -> RunOutcome:
    """Repair each program not already in `run_file`, appending a line per program.

    `make_client` is called once per program so token and cost totals are per program
    (TogetherClient's counters are cumulative). `limit` caps how many programs are run
    in this call, not counting ones skipped as already done.
    With `renamed`, each program is renamed first; ones that can't be are skipped.
    Nothing is written for programs that errored or couldn't be renamed.
    """
    done = {r["program"] for r in load_results(run_file)}
    todo = [p for p in programs if p not in done]
    if limit is not None:
        todo = todo[:limit]
    skipped = len(done & set(programs))
    if skipped:
        print(f"Skipping {skipped} program(s) already in {run_file.name}.")

    outcome = RunOutcome()
    for i, program in enumerate(todo, 1):
        print(f"[{i}/{len(todo)}] {program} ... ", end="", flush=True)
        rn = None
        if renamed:
            buggy = (quixbugs_dir / "python_programs" / f"{program}.py").read_text()
            try:
                rn = rename_program(buggy, program)
            except RenameError as e:
                print(f"SKIPPED (rename failed: {e})")
                outcome.rename_failed[program] = str(e)
                continue
        start = time.monotonic()
        try:
            res = repair(
                program, make_client(), run_tests,
                max_attempts=max_attempts, quixbugs_dir=quixbugs_dir, renamed=rn,
            )
        except Exception as e:  # noqa: BLE001 - one bad program must not stop the batch
            print(f"ERROR {type(e).__name__}: {e}")
            outcome.errored.append(program)
            continue
        seconds = round(time.monotonic() - start, 2)

        record = asdict(res)  # program, model, attempts, passed, tokens, cost_usd, final_source
        record.update(
            fixed_first_try=res.passed and res.attempts == 1,
            max_attempts=max_attempts,
            seconds=seconds,
            renamed=renamed,
            alias=rn.alias_line if rn else None,  # appended to final_source when testing it
            run_id=run_file.stem,
            git_commit=git_commit,
            git_dirty=git_dirty,
            quixbugs_commit=quixbugs_commit,
            finished_at=dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds"),
        )
        append_result(run_file, record)
        status = "PASS" if res.passed else "FAIL"
        print(f"{status} attempts={res.attempts} tokens={res.tokens_in}/{res.tokens_out} {seconds}s")
    return outcome


def summarize(results: list[dict]) -> str:
    """Totals over every line in a run file."""
    n = len(results)
    first = sum(r["fixed_first_try"] for r in results)
    within3 = sum(r["passed"] and r["attempts"] <= 3 for r in results)
    tin = sum(r["tokens_in"] for r in results)
    tout = sum(r["tokens_out"] for r in results)
    costs = [r["cost_usd"] for r in results]
    cost = "unknown (model price not set)" if None in costs else f"${sum(costs):.4f}"
    already = sum(r["attempts"] == 0 for r in results)

    def pct(k: int) -> str:
        return f"{k}/{n} ({100 * k / n:.0f}%)" if n else "0/0"

    lines = [
        f"Programs:           {n}",
        f"Fixed first try:    {pct(first)}",
        f"Fixed within 3 tries: {pct(within3)}",
        f"Tokens in / out:    {tin:,} / {tout:,}",
        f"Total cost:         {cost}",
    ]
    if already:
        lines.append(f"(already passing before any model call: {already})")
    return "\n".join(lines)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Repair every QuixBugs Python program.")
    parser.add_argument("--run-id", help="name of results/runs/<run_id>.jsonl (default: model_date)")
    parser.add_argument("--model", help="overrides INFERNO_MODEL")
    parser.add_argument("--max-attempts", type=int, default=MAX_ATTEMPTS)
    parser.add_argument("--limit", type=int, help="run at most N programs not already done")
    parser.add_argument("--programs", help="comma-separated subset, e.g. gcd,bitcount")
    parser.add_argument("--renamed", action="store_true",
                        help="memorization check: show the model neutrally renamed programs")
    args = parser.parse_args(argv)

    available = list_programs()
    if not available:
        parser.error(f"no programs found in {sandbox.QUIXBUGS_DIR} (did you run scripts/setup_quixbugs.sh?)")
    programs = available
    if args.programs:
        programs = [p.strip() for p in args.programs.split(",") if p.strip()]
        unknown = sorted(set(programs) - set(available))
        if unknown:
            parser.error(f"unknown program(s): {', '.join(unknown)}")
    if args.limit is not None and args.limit < 1:
        parser.error("--limit must be at least 1")

    try:
        sandbox.ensure_image()
        make_client = lambda: TogetherClient(model=args.model)  # noqa: E731
        model = make_client().model  # also checks the API key is set before we start
    except RuntimeError as e:
        print(f"error: {e}", file=sys.stderr)
        return 2

    run_id = args.run_id or default_run_id(model, renamed=args.renamed)
    run_file = RUNS_DIR / f"{run_id}.jsonl"
    existing = load_results(run_file)
    other_models = {r["model"] for r in existing} - {model}
    if other_models:
        print(f"error: {run_file} already has results for {', '.join(other_models)}, "
              f"not {model}. Use a different --run-id.", file=sys.stderr)
        return 2
    if any(r.get("renamed", False) != args.renamed for r in existing):
        kind = "renamed" if not args.renamed else "non-renamed"
        print(f"error: {run_file} already has {kind} results. Use a different --run-id.",
              file=sys.stderr)
        return 2

    commit, dirty = git_info()
    if dirty:
        print(f"warning: uncommitted changes; results will be tagged {commit} but may not match it",
              file=sys.stderr)
    quixbugs_commit, _ = git_info(sandbox.QUIXBUGS_DIR)
    mode = " | RENAMED" if args.renamed else ""
    print(f"Run {run_id} | model {model} | commit {commit}{mode} -> {run_file}")

    outcome = run_all(
        programs, run_file, make_client,
        max_attempts=args.max_attempts, git_commit=commit, git_dirty=dirty,
        quixbugs_commit=quixbugs_commit, limit=args.limit, renamed=args.renamed,
    )

    print("\n" + summarize(load_results(run_file)))
    if outcome.rename_failed:
        print(f"\n{len(outcome.rename_failed)} program(s) skipped because renaming failed:")
        for program, reason in outcome.rename_failed.items():
            print(f"  - {program}: {reason}")
    if outcome.errored:
        print(f"\n{len(outcome.errored)} program(s) errored and were not recorded "
              f"(re-run with --run-id {run_id} to retry): {', '.join(outcome.errored)}")
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
