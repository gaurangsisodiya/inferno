"""Repair loop v1: ask an LLM to fix one buggy QuixBugs program.

The loop, for one program:

1. Copy the buggy file into a fresh temporary workspace.
2. Run its tests in the sandbox to get the initial failure output.
3. Send the model the buggy source + failure output; ask for the full fixed file.
4. Write the reply into the workspace and re-run the tests.
5. If they still fail, send the new failure output back and try again,
   up to `max_attempts` model calls in total.

Run it from the repo root:

    uv run python -m inferno.loop --program bitcount
"""

from __future__ import annotations

import argparse
import difflib
import json
import re
import shutil
import sys
import tempfile
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Callable

from inferno import sandbox
from inferno.client import TogetherClient

MAX_ATTEMPTS = 3
MAX_FEEDBACK_CHARS = 8000  # keep prompts small when the test output is huge

SYSTEM_PROMPT = (
    "You are an expert Python programmer who repairs buggy programs. "
    "Each program contains a small bug, usually on a single line. "
    "Make the smallest change that fixes it, and keep the function names and signatures unchanged. "
    "Reply with the complete corrected Python file and nothing else: "
    "no explanation, no Markdown."
)


@dataclass
class RepairResult:
    """Summary of one repair run. This is what `repair()` returns."""

    program: str
    model: str
    attempts: int  # model calls made (0 if the tests already passed)
    passed: bool
    tokens_in: int
    tokens_out: int
    cost_usd: float | None  # None if the model's price is unknown
    final_source: str = field(repr=False)  # last candidate file that was tested


# A function that takes (program, workspace) and returns a SandboxResult.
# The real one is sandbox.run_tests; tests swap in a fake.
TestRunner = Callable[[str, Path], sandbox.SandboxResult]


def initial_prompt(program: str, source: str, test_output: str) -> str:
    return (
        f"The program `{program}.py` below has a bug.\n\n"
        f"```python\n{source}```\n\n"
        f"Running its tests gives:\n\n```\n{trim(test_output)}\n```\n\n"
        "Return the complete corrected file."
    )


def retry_prompt(test_output: str) -> str:
    return (
        "Your version still fails the tests:\n\n"
        f"```\n{trim(test_output)}\n```\n\n"
        "Return the complete corrected file again."
    )


def trim(text: str, limit: int = MAX_FEEDBACK_CHARS) -> str:
    """Keep the start and end of long output, since pytest's summary is at the end."""
    if len(text) <= limit:
        return text
    half = limit // 2
    return text[:half] + "\n\n... [output trimmed] ...\n\n" + text[-half:]


_FENCE = re.compile(r"```[a-zA-Z0-9_+-]*\n(.*?)```", re.DOTALL)


def extract_code(reply: str) -> str:
    """Return the code from the model's reply, removing Markdown fences if present.

    We ask for raw code, but models often wrap it in ```python ... ``` anyway.
    If there are several fenced blocks, the longest one is used.
    """
    blocks = _FENCE.findall(reply)
    code = max(blocks, key=len) if blocks else reply
    return code.strip("\n") + "\n"


def make_workspace(program: str, root: Path, quixbugs_dir: Path) -> Path:
    """Create <root>/python_programs/ holding the buggy program and node.py.

    Returns the path of the program file that the loop overwrites with each candidate.
    """
    src_dir = quixbugs_dir / "python_programs"
    dst_dir = root / "python_programs"
    dst_dir.mkdir()
    dst_dir.chmod(0o755)  # the container runs as "nobody" and must be able to read it
    for name in (f"{program}.py", "node.py"):  # graph programs import node.py
        shutil.copyfile(src_dir / name, dst_dir / name)
        (dst_dir / name).chmod(0o644)
    return dst_dir / f"{program}.py"


def repair(
    program: str,
    client: TogetherClient,
    run_tests: TestRunner = sandbox.run_tests,
    max_attempts: int = MAX_ATTEMPTS,
    quixbugs_dir: Path = sandbox.QUIXBUGS_DIR,
) -> RepairResult:
    """Try to fix `program`, calling the model at most `max_attempts` times."""
    buggy_source = (quixbugs_dir / "python_programs" / f"{program}.py").read_text()

    def result(attempts: int, passed: bool, source: str) -> RepairResult:
        return RepairResult(
            program=program,
            model=client.model,
            attempts=attempts,
            passed=passed,
            tokens_in=client.tokens_in,
            tokens_out=client.tokens_out,
            cost_usd=client.cost_usd(),
            final_source=source,
        )

    # TemporaryDirectory deletes the folder when the `with` block ends.
    with tempfile.TemporaryDirectory(prefix=f"inferno-{program}-") as tmp:
        target = make_workspace(program, Path(tmp), quixbugs_dir)

        test_run = run_tests(program, Path(tmp))
        if test_run.passed:  # nothing to repair
            return result(0, True, buggy_source)

        messages = [
            {"role": "system", "content": SYSTEM_PROMPT},
            {"role": "user", "content": initial_prompt(program, buggy_source, test_run.output)},
        ]
        candidate = buggy_source
        for attempt in range(1, max_attempts + 1):
            reply = client.chat(messages)
            candidate = extract_code(reply.text)
            target.write_text(candidate)

            test_run = run_tests(program, Path(tmp))
            if test_run.passed:
                return result(attempt, True, candidate)

            # Keep the whole conversation so the model sees its previous attempt.
            messages.append({"role": "assistant", "content": reply.text})
            messages.append({"role": "user", "content": retry_prompt(test_run.output)})

        return result(max_attempts, False, candidate)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Repair one QuixBugs Python program.")
    parser.add_argument("--program", required=True, help="e.g. bitcount")
    parser.add_argument("--max-attempts", type=int, default=MAX_ATTEMPTS)
    parser.add_argument("--model", help="overrides INFERNO_MODEL")
    args = parser.parse_args(argv)

    buggy_path = sandbox.QUIXBUGS_DIR / "python_programs" / f"{args.program}.py"
    if not buggy_path.exists():
        parser.error(f"no such program: {buggy_path} (did you run scripts/setup_quixbugs.sh?)")

    try:
        sandbox.ensure_image()
        client = TogetherClient(model=args.model)
    except RuntimeError as e:  # Docker missing or API key missing: explain and stop
        print(f"error: {e}", file=sys.stderr)
        return 2
    res = repair(args.program, client, max_attempts=args.max_attempts)

    diff = difflib.unified_diff(
        buggy_path.read_text().splitlines(keepends=True),
        res.final_source.splitlines(keepends=True),
        fromfile=f"buggy/{args.program}.py",
        tofile=f"candidate/{args.program}.py",
    )
    print("".join(diff))
    summary = asdict(res)
    del summary["final_source"]
    print(json.dumps(summary, indent=2))
    return 0 if res.passed else 1


if __name__ == "__main__":
    sys.exit(main())
