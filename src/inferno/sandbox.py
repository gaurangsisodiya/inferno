"""Run one QuixBugs program's tests inside a locked-down Docker container.

Model-written code is untrusted: it could loop forever, eat memory, or try to
read files / reach the network. So the tests never run on the host. Instead:

* `--network none`      : no network at all
* `--memory` / `--cpus` / `--pids-limit` : resource caps
* `--read-only`, `--cap-drop ALL`, non-root user : can't modify the container
* only these paths are mounted, all read-only:
    - <workspace>/python_programs   (the candidate fix + node.py)
    - the few QuixBugs test files this one program needs
* a per-test timeout (pytest-timeout) AND a hard wall-clock timeout on the
  whole `docker run` (subprocess timeout, then `docker kill`).

~/inferno-private (the reference answers) is never mounted: every mount is
checked by `_assert_not_private` before the container starts.

Inside the container the files are laid out like the QuixBugs repo:

    /quixbugs/conftest.py
    /quixbugs/python_programs/<program>.py, node.py
    /quixbugs/python_testcases/test_<program>.py, load_testdata.py, node.py
    /quixbugs/json_testcases/<program>.json      (only if it exists)
"""

from __future__ import annotations

import os
import subprocess
import uuid
from dataclasses import dataclass
from pathlib import Path

from inferno.client import REPO_ROOT

QUIXBUGS_DIR = REPO_ROOT / "data" / "quixbugs"
PRIVATE_DIR = Path(os.environ.get("INFERNO_PRIVATE", Path.home() / "inferno-private"))

IMAGE = "inferno-sandbox:py3.12"
DOCKERFILE = REPO_ROOT / "docker" / "sandbox.Dockerfile"
CONTAINER_ROOT = "/quixbugs"

PER_TEST_TIMEOUT_S = 5  # pytest-timeout, per individual test case
HARD_TIMEOUT_S = 120  # whole `docker run`, enforced by subprocess
MEMORY_LIMIT = "512m"


@dataclass
class SandboxResult:
    """Outcome of running one program's tests once."""

    passed: bool
    output: str  # pytest's combined stdout/stderr (what we show the model)
    timed_out: bool = False  # True if the hard timeout killed the container


def _assert_not_private(path: Path, private_dir: Path = PRIVATE_DIR) -> None:
    """Refuse to mount anything inside, or containing, the private answers dir."""
    path = path.resolve()
    private = private_dir.resolve()
    if path == private or private in path.parents or path in private.parents:
        raise PermissionError(f"Refusing to mount {path}: overlaps {private}")


def needed_test_files(program: str, quixbugs_dir: Path = QUIXBUGS_DIR) -> list[tuple[Path, str]]:
    """(host path, container path) for each QuixBugs test file this program needs."""
    q = quixbugs_dir
    c = CONTAINER_ROOT
    mounts = [
        (q / "conftest.py", f"{c}/conftest.py"),
        (q / "python_testcases" / f"test_{program}.py", f"{c}/python_testcases/test_{program}.py"),
        (q / "python_testcases" / "load_testdata.py", f"{c}/python_testcases/load_testdata.py"),
        (q / "python_testcases" / "node.py", f"{c}/python_testcases/node.py"),
    ]
    # 31 of the 40 programs keep their test cases in a JSON file; the graph
    # programs define their cases inside the test file instead.
    json_file = q / "json_testcases" / f"{program}.json"
    if json_file.exists():
        mounts.append((json_file, f"{c}/json_testcases/{program}.json"))
    return mounts


def build_docker_command(
    program: str,
    workspace: Path,
    container_name: str,
    quixbugs_dir: Path = QUIXBUGS_DIR,
    private_dir: Path = PRIVATE_DIR,
    per_test_timeout: int = PER_TEST_TIMEOUT_S,
) -> list[str]:
    """Build the `docker run ...` argument list. Kept separate so tests can inspect it."""
    mounts = [(workspace / "python_programs", f"{CONTAINER_ROOT}/python_programs")]
    mounts += needed_test_files(program, quixbugs_dir)

    cmd = [
        "docker", "run", "--rm",
        "--name", container_name,
        "--network", "none",
        "--memory", MEMORY_LIMIT,
        "--memory-swap", MEMORY_LIMIT,  # same as --memory => no extra swap
        "--cpus", "1",
        "--pids-limit", "128",
        "--read-only",
        "--tmpfs", "/tmp",
        "--cap-drop", "ALL",
        "--security-opt", "no-new-privileges",
        "--user", "65534:65534",  # "nobody"
        "--env", "PYTHONDONTWRITEBYTECODE=1",
        "--workdir", CONTAINER_ROOT,
    ]
    for host, target in mounts:
        _assert_not_private(host, private_dir)
        # --mount (unlike -v) errors if the host path is missing instead of
        # silently creating an empty directory.
        cmd += ["--mount", f"type=bind,source={host},target={target},readonly"]

    cmd += [
        IMAGE,
        "python", "-m", "pytest",
        f"python_testcases/test_{program}.py",
        f"--rootdir={CONTAINER_ROOT}",
        f"--timeout={per_test_timeout}",
        "-q", "--tb=short", "-p", "no:cacheprovider",
    ]
    return cmd


def ensure_image() -> None:
    """Check Docker works, then build the sandbox image the first time it's needed."""
    # `docker info` fails if Docker isn't installed, the daemon isn't running,
    # or (on WSL) `docker` is only Docker Desktop's placeholder script.
    try:
        docker_ok = subprocess.run(["docker", "info"], capture_output=True).returncode == 0
    except FileNotFoundError:
        docker_ok = False
    if not docker_ok:
        raise RuntimeError(
            "Docker is not available. On WSL, enable Docker Desktop's WSL integration "
            "for this distro (Settings > Resources > WSL integration), or install "
            "Docker Engine, then check that `docker info` works."
        )
    found = subprocess.run(["docker", "image", "inspect", IMAGE], capture_output=True).returncode == 0
    if not found:
        subprocess.run(
            ["docker", "build", "-t", IMAGE, "-f", str(DOCKERFILE), str(DOCKERFILE.parent)],
            check=True,
        )


def run_tests(
    program: str,
    workspace: Path,
    quixbugs_dir: Path = QUIXBUGS_DIR,
    hard_timeout: int = HARD_TIMEOUT_S,
) -> SandboxResult:
    """Run test_<program>.py against <workspace>/python_programs/<program>.py."""
    name = f"inferno-{program}-{uuid.uuid4().hex[:8]}"
    cmd = build_docker_command(program, workspace, name, quixbugs_dir)
    try:
        proc = subprocess.run(cmd, capture_output=True, text=True, timeout=hard_timeout)
    except subprocess.TimeoutExpired as e:
        # Killing the `docker` CLI process doesn't stop the container, so kill it by name.
        subprocess.run(["docker", "kill", name], capture_output=True)
        partial = (e.stdout or b"").decode() if isinstance(e.stdout, bytes) else (e.stdout or "")
        return SandboxResult(
            passed=False,
            output=partial + f"\n[sandbox] Hard timeout: tests did not finish in {hard_timeout}s.",
            timed_out=True,
        )
    # pytest exit code 0 means every test passed. Anything else (failures,
    # import errors, "no tests collected") counts as a failure.
    return SandboxResult(passed=proc.returncode == 0, output=proc.stdout + proc.stderr)
