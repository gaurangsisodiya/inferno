"""Validate one repair result: original QuixBugs tests + Hypothesis properties.

A candidate that passes the original tests may still be wrong, because
QuixBugs has only a handful of test cases per program. The properties in
src/inferno/properties/ check general statements about the output on ~200
generated inputs each. Both run in the same locked-down sandbox as the repair loop.
"""

from __future__ import annotations

import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Callable

from inferno import sandbox
from inferno.loop import TestRunner, make_workspace, trim

PROPERTIES_DIR = Path(__file__).resolve().parent / "properties"

# (program, workspace, property_file) -> SandboxResult. The real one is sandbox.run_properties.
PropertyRunner = Callable[[str, Path, Path], sandbox.SandboxResult]


@dataclass
class Validation:
    program: str
    passes_original: bool
    passes_properties: bool | None  # None: no prop_<program>.py exists
    # True only when both pass. None when the originals pass but there are no
    # properties to check, so "valid" can't be confirmed either way.
    valid: bool | None
    property_output: str | None  # pytest output when the properties fail, else None


def property_file(program: str, properties_dir: Path = PROPERTIES_DIR) -> Path | None:
    path = properties_dir / f"prop_{program}.py"
    return path if path.exists() else None


def programs_with_properties(properties_dir: Path = PROPERTIES_DIR) -> list[str]:
    return sorted(p.stem.removeprefix("prop_") for p in properties_dir.glob("prop_*.py"))


def validate(
    record: dict,
    run_tests: TestRunner = sandbox.run_tests,
    run_properties: PropertyRunner = sandbox.run_properties,
    quixbugs_dir: Path = sandbox.QUIXBUGS_DIR,
    properties_dir: Path = PROPERTIES_DIR,
) -> Validation:
    """Check `record["final_source"]` (a line from results/runs/<run_id>.jsonl)."""
    program = record["program"]
    prop_file = property_file(program, properties_dir)

    with tempfile.TemporaryDirectory(prefix=f"inferno-validate-{program}-") as tmp:
        target = make_workspace(program, Path(tmp), quixbugs_dir)
        target.write_text(record["final_source"])

        original = run_tests(program, Path(tmp))
        props = run_properties(program, Path(tmp), prop_file) if prop_file else None

    passes_properties = None if props is None else props.passed
    if not original.passed:
        valid = False
    elif passes_properties is None:
        valid = None
    else:
        valid = passes_properties

    return Validation(
        program=program,
        passes_original=original.passed,
        passes_properties=passes_properties,
        valid=valid,
        property_output=trim(props.output) if props is not None and not props.passed else None,
    )
