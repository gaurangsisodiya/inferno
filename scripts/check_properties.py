"""Sanity-check the Hypothesis properties against QuixBugs' own versions.

    uv run python scripts/check_properties.py
    uv run python scripts/check_properties.py gcd sqrt

A useful property file must:
  * PASS on the reference correct program (otherwise the property is wrong), and
  * FAIL on the original buggy program (otherwise it can't catch this bug).

This runs on the host, NOT in the agent sandbox: the correct versions are
imported from the private directory (~/inferno-private, or $INFERNO_PRIVATE)
via PYTHONPATH. The QuixBugs programs are trusted code, unlike model output.
Reference-run output is never printed (--tb=no, results are read from a
JUnit XML report), so no reference source ends up in logs.

Exit code 0 if every property file meets both conditions.
"""

from __future__ import annotations

import argparse
import os
import subprocess
import sys
import tempfile
import xml.etree.ElementTree as ET
from pathlib import Path

from inferno.sandbox import PRIVATE_DIR, QUIXBUGS_DIR
from inferno.validate import PROPERTIES_DIR, programs_with_properties

# Backstops only: each call to the program is already capped at 2s of CPU
# inside the property files (time_limited), and shrinking is off.
PER_TEST_TIMEOUT_S = 60
HARD_TIMEOUT_S = 300


def run_properties(program: str, package: str, pythonpath: Path) -> dict[str, bool]:
    """Run prop_<program>.py on host with the given package. Returns {test name: passed}."""
    prop_file = PROPERTIES_DIR / f"prop_{program}.py"
    env = dict(os.environ, INFERNO_PROGRAM_PACKAGE=package, PYTHONPATH=str(pythonpath),
               PYTHONDONTWRITEBYTECODE="1")
    with tempfile.TemporaryDirectory() as tmp:
        report = Path(tmp) / "report.xml"
        try:
            subprocess.run(
                [sys.executable, "-m", "pytest", str(prop_file), "-q", "--tb=no", "--no-header",
                 "-p", "no:cacheprovider", f"--timeout={PER_TEST_TIMEOUT_S}",
                 f"--junitxml={report}"],
                env=env, cwd=tmp, capture_output=True, text=True, timeout=HARD_TIMEOUT_S,
            )
        except subprocess.TimeoutExpired:
            return {f"<whole file timed out after {HARD_TIMEOUT_S}s>": False}
        if not report.exists():
            return {"<collection>": False}
        cases = ET.parse(report).getroot().iter("testcase")
        results = {c.get("name"): not any(ch.tag in ("failure", "error") for ch in c)
                   for c in cases}
    return results or {"<collection>": False}  # no tests = import failed


def main(argv: list[str]) -> int:
    known = programs_with_properties()
    p = argparse.ArgumentParser(
        description="Check each property file passes on the reference program and fails on the "
                    f"buggy one. Reads the reference programs from {PRIVATE_DIR} "
                    "($INFERNO_PRIVATE); prints only pass/fail.")
    p.add_argument("programs", nargs="*", metavar="PROGRAM",
                   help=f"programs to check (default: all {len(known)}: {', '.join(known)})")
    args = p.parse_args(argv)
    unknown = [name for name in args.programs if name not in known]
    if unknown:
        p.error(f"no property file for: {', '.join(unknown)}")

    correct_pkg = PRIVATE_DIR / "correct_python_programs"
    if not correct_pkg.is_dir():
        print(f"error: {correct_pkg} not found (run scripts/setup_quixbugs.sh)", file=sys.stderr)
        return 2

    programs = args.programs or known
    problems = []
    for program in programs:
        print(f"... {program}", end="\r", flush=True)
        on_correct = run_properties(program, "correct_python_programs", PRIVATE_DIR)
        on_buggy = run_properties(program, "python_programs", QUIXBUGS_DIR)

        correct_ok = all(on_correct.values())
        buggy_caught = not all(on_buggy.values())
        status = "OK " if correct_ok and buggy_caught else "BAD"
        caught = sum(not ok for ok in on_buggy.values())
        print(f"{status} {program:28} correct: {'pass' if correct_ok else 'FAIL'}   "
              f"buggy: {'fails' if buggy_caught else 'PASSES'} "
              f"({caught}/{len(on_buggy)} properties catch it)")
        for name, ok in on_correct.items():
            if not ok:
                print(f"      property fails on CORRECT version: {name}")
        if not correct_ok:
            problems.append(f"{program}: fails on the correct version")
        if not buggy_caught:
            problems.append(f"{program}: passes on the buggy version")

    if problems:
        print("\nProperty files that don't meet both conditions:")
        for p in problems:
            print(f"  - {p}")
        return 1
    print(f"\nAll {len(programs)} property files pass on correct and fail on buggy.")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
