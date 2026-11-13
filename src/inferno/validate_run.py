"""Validate every result in a run: original tests + Hypothesis properties.

    uv run python -m inferno.validate_run --run-id gptoss-v1
    uv run python -m inferno.validate_run --run-id gptoss-v1 --programs gcd,sqrt

Reads results/runs/<run_id>.jsonl and writes results/validated/<run_id>.jsonl,
one line per program, each flushed as soon as it's done. Re-running skips
programs already in the output file. A program whose validation raised an
exception isn't written, so it is retried next time.

The summary highlights "plausible but wrong" fixes: candidates that pass the
original QuixBugs tests but fail the properties.
"""

from __future__ import annotations

import argparse
import datetime as dt
import sys
from dataclasses import asdict
from pathlib import Path

from inferno import sandbox
from inferno.client import REPO_ROOT
from inferno.loop import TestRunner
from inferno.run_all import RUNS_DIR, append_result, git_info, load_results
from inferno.validate import PROPERTIES_DIR, PropertyRunner, validate

VALIDATED_DIR = REPO_ROOT / "results" / "validated"


def validate_run(
    records: list[dict],
    out_file: Path,
    run_tests: TestRunner = sandbox.run_tests,
    run_properties: PropertyRunner = sandbox.run_properties,
    quixbugs_dir: Path = sandbox.QUIXBUGS_DIR,
    properties_dir: Path = PROPERTIES_DIR,
    git_commit: str | None = None,
    git_dirty: bool | None = None,
    limit: int | None = None,
) -> list[str]:
    """Validate each record not already in `out_file`. Returns programs that raised."""
    done = {r["program"] for r in load_results(out_file)}
    todo = [r for r in records if r["program"] not in done]
    if limit is not None:
        todo = todo[:limit]
    if done:
        print(f"Skipping {len(done)} program(s) already in {out_file.name}.")

    errored = []
    for i, rec in enumerate(todo, 1):
        program = rec["program"]
        print(f"[{i}/{len(todo)}] {program} ... ", end="", flush=True)
        try:
            v = validate(rec, run_tests, run_properties, quixbugs_dir, properties_dir)
        except Exception as e:  # noqa: BLE001 - one bad program must not stop the batch
            print(f"ERROR {type(e).__name__}: {e}")
            errored.append(program)
            continue

        line = asdict(v)
        line.update(
            run_id=rec.get("run_id", out_file.stem),
            model=rec.get("model"),
            renamed=rec.get("renamed", False),
            run_passed=rec.get("passed"),  # what the repair run itself reported
            attempts=rec.get("attempts"),
            run_git_commit=rec.get("git_commit"),  # code that produced the fix
            git_commit=git_commit,  # code (incl. properties) that validated it
            git_dirty=git_dirty,
            validated_at=dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds"),
        )
        append_result(out_file, line)
        props = {None: "no properties", True: "properties PASS", False: "properties FAIL"}
        print(f"original {'PASS' if v.passes_original else 'FAIL'}, {props[v.passes_properties]}")
    return errored


def plausible_but_wrong(results: list[dict]) -> list[str]:
    """Programs whose candidate passes the original tests but fails the properties."""
    return sorted(r["program"] for r in results
                  if r["passes_original"] and r["passes_properties"] is False)


def summarize(results: list[dict]) -> str:
    with_props = [r for r in results if r["passes_properties"] is not None]
    wrong = plausible_but_wrong(results)
    lines = [
        f"Programs validated:                     {len(results)}",
        f"Pass original tests:                    {sum(r['passes_original'] for r in results)}",
        f"Programs with properties:               {len(with_props)}",
        f"  valid (original + properties pass):   {sum(r['valid'] is True for r in with_props)}",
        f"  passed originals but FAILED properties: {len(wrong)}",
    ]
    if wrong:
        lines.append("Plausible-but-wrong fixes: " + ", ".join(wrong))
    return "\n".join(lines)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Validate a run's fixes with tests + properties.")
    parser.add_argument("--run-id", required=True, help="reads results/runs/<run_id>.jsonl")
    parser.add_argument("--programs", help="comma-separated subset, e.g. gcd,sqrt")
    parser.add_argument("--limit", type=int, help="validate at most N programs not already done")
    args = parser.parse_args(argv)

    run_file = RUNS_DIR / f"{args.run_id}.jsonl"
    records = load_results(run_file)
    if not records:
        parser.error(f"no results in {run_file}")
    if args.programs:
        wanted = [p.strip() for p in args.programs.split(",") if p.strip()]
        unknown = sorted(set(wanted) - {r["program"] for r in records})
        if unknown:
            parser.error(f"not in {run_file.name}: {', '.join(unknown)}")
        records = [r for r in records if r["program"] in wanted]
    if args.limit is not None and args.limit < 1:
        parser.error("--limit must be at least 1")

    try:
        sandbox.ensure_image()
    except RuntimeError as e:
        print(f"error: {e}", file=sys.stderr)
        return 2

    commit, dirty = git_info()
    if dirty:
        print(f"warning: uncommitted changes; results will be tagged {commit} but may not match it",
              file=sys.stderr)
    out_file = VALIDATED_DIR / f"{args.run_id}.jsonl"
    print(f"Validating {run_file.name} | commit {commit} -> {out_file}")

    errored = validate_run(records, out_file, git_commit=commit, git_dirty=dirty, limit=args.limit)

    print("\n" + summarize(load_results(out_file)))
    if errored:
        print(f"\n{len(errored)} program(s) errored and were not recorded "
              f"(re-run to retry): {', '.join(errored)}")
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
