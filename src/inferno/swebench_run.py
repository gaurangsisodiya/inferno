"""Run mini-swe-agent on the fixed 25-task SWE-bench Verified sample, one task at a time.

    uv run python -m inferno.swebench_run --model together_ai/openai/gpt-oss-120b --run-id gptoss-swe-v1
    uv run python -m inferno.swebench_run --model ... --run-id ... --limit 1   # one task only

For each task in data/swebench_25.json (see inferno.swebench_tasks):

1. `docker pull` the task's SWE-bench image (its own timeout; not counted
   against the task's wall-clock limit).
2. Start a worker subprocess that builds mini-swe-agent's own pieces: the
   swebench.yaml config, `get_sb_environment` (DockerEnvironment, cwd
   /testbed) and `DefaultAgent`, with our limits merged in, and runs
   `agent.run(problem_statement)`. The agent saves its trajectory after every
   step, so tokens/steps/cost survive even if the worker is killed.
3. Kill the worker's whole process group if it outlives the timeout plus a
   grace period (the agent's own wall-clock check only runs between steps).
4. Always (success, error, timeout, Ctrl-C): `docker rm -f` every container
   with this task's label, then `docker rmi` the image, so at most one task
   image is on disk at a time.
5. Append one JSON line to results/swebench/<run_id>.jsonl (fsynced) and
   rewrite results/swebench/<run_id>.preds.json for `swebench eval`.

Restarting with the same --run-id skips tasks already in the JSONL file
(including ones that ended in an error; pass --retry-errors to rerun those).

Costs: litellm's price is used when it knows the model (including its
cached-input discounts); otherwise our own table (PRICES_PER_MILLION below, plus inferno.client's). mini-swe-agent is set
to cost_tracking="ignore_errors" so a missing price doesn't crash it, and a
call whose price neither source knows is marked price_source "unknown" with
cost_usd null, never a silent 0. Without a known price the cost cap can't
work, so the run refuses to start unless --allow-unknown-cost is given.

For reproducible prices, this module sets LITELLM_LOCAL_MODEL_COST_MAP=True
before litellm can be imported, in the runner and (since it runs this same
module) in the worker. litellm then uses the price list bundled with its pinned
version (uv.lock) instead of downloading the latest one at startup. The worker
checks where litellm actually loaded its prices from and refuses to run the
task otherwise. Each result line records litellm_version, price_map_source,
price_map_revision and price_source (litellm / table / litellm+table / unknown).
"""

from __future__ import annotations

import os

# Must run before anything imports litellm (mini-swe-agent does, on import).
os.environ["LITELLM_LOCAL_MODEL_COST_MAP"] = "True"

import argparse
import datetime as dt
import json
import signal
import subprocess
import sys
import tempfile
import time
import traceback
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable, Iterable, Protocol

from inferno import client as inferno_client
from inferno.client import REPO_ROOT
from inferno.run_all import append_result, git_info, load_results
from inferno.swebench_tasks import TASKS_FILE, load_task_file, repo_of

RESULTS_DIR = REPO_ROOT / "results" / "swebench"
LABEL_KEY = "inferno.swebench.task"

DEFAULT_COST_CAP = 0.50
DEFAULT_STEP_LIMIT = 50
DEFAULT_TIMEOUT_S = 20 * 60
KILL_GRACE_S = 120  # after the timeout: lets the last model call/command finish before SIGKILL
DEFAULT_PULL_TIMEOUT_S = 30 * 60

# Our own prices (USD per 1M tokens, input/output), keyed by litellm model name.
# Only used when litellm doesn't know the price. Last checked 2026-11-15.
# The fallback prices every prompt token at the input rate: it doesn't look at
# cached tokens, so a cached-input discount is never applied (conservative for
# the cost cap). Kimi-K2.6 lists $0.20/1M cached input on Together; unused here.
PRICES_PER_MILLION: dict[str, tuple[float, float]] = {
    "together_ai/openai/gpt-oss-120b": (0.15, 0.60),
    "together_ai/moonshotai/Kimi-K2.6": (1.20, 4.50),  # Together /v1/models, 2026-11-15
}
LIMIT_STATUSES = {"LimitsExceeded", "TimeExceeded", "HardTimeout"}


# --------------------------------------------------------------------------- prices

def table_price(model: str) -> tuple[float, float] | None:
    """(input, output) USD per 1M tokens from our tables, or None."""
    if model in PRICES_PER_MILLION:
        return PRICES_PER_MILLION[model]
    # inferno.client keys models without the litellm provider prefix.
    bare = model.split("/", 1)[1] if model.startswith("together_ai/") else model
    return inferno_client.PRICES_PER_MILLION.get(bare)


def litellm_knows_price(model: str) -> bool:
    import litellm

    try:
        info = litellm.get_model_info(model)
    except Exception:  # noqa: BLE001 - litellm raises various errors for unknown models
        return False
    return bool(info.get("input_cost_per_token")) and bool(info.get("output_cost_per_token"))


def litellm_info() -> dict:
    """litellm's version and where this process's litellm loaded its price map from."""
    from importlib.metadata import version

    import litellm  # noqa: F401 - loading litellm loads the price map
    from litellm.litellm_core_utils.get_model_cost_map import get_model_cost_map_source_info

    src = get_model_cost_map_source_info()
    return {
        "litellm_version": version("litellm"),
        "price_map_source": src.get("source"),  # "local" = bundled with this litellm version
        "price_map_revision": src.get("source_revision"),
    }


def price_known(model: str) -> bool:
    return table_price(model) is not None or litellm_knows_price(model)


def _usage(response) -> tuple[int | None, int | None]:
    usage = response.get("usage") if isinstance(response, dict) else getattr(response, "usage", None)
    if usage is None:
        return None, None
    get = usage.get if isinstance(usage, dict) else lambda k: getattr(usage, k, None)
    return get("prompt_tokens"), get("completion_tokens")


def cost_of_call(model: str, response, litellm_cost: Callable | None = None) -> dict:
    """{"cost": float, "price_source": "litellm" | "table" | "unknown"} for one model response."""
    if litellm_cost is None:
        import litellm

        litellm_cost = litellm.cost_calculator.completion_cost
    try:
        cost = litellm_cost(response, model=model)
        if cost and cost > 0:
            return {"cost": float(cost), "price_source": "litellm"}
    except Exception:  # noqa: BLE001 - unknown model / missing price
        pass
    price = table_price(model)
    tin, tout = _usage(response)
    if price is not None and tin is not None and tout is not None:
        return {"cost": (tin * price[0] + tout * price[1]) / 1e6, "price_source": "table"}
    # mini-swe-agent adds extra["cost"] to its running total, so it must be a number;
    # price_source "unknown" makes the task's cost_usd null in our results.
    return {"cost": 0.0, "price_source": "unknown"}


def _priced_model_class():
    from minisweagent.models.litellm_model import LitellmModel

    class PricedLitellmModel(LitellmModel):
        """LitellmModel whose per-call cost falls back to our price table."""

        def _calculate_cost(self, response) -> dict:
            return cost_of_call(self.config.model_name, response)

    return PricedLitellmModel


def __getattr__(name):  # lazy, so importing this module doesn't import litellm
    if name == "PricedLitellmModel":
        return _priced_model_class()
    raise AttributeError(name)


# --------------------------------------------------------------------------- trajectory -> metrics

def trajectory_metrics(traj: dict | None) -> dict:
    """steps, tokens and cost from a mini-swe-agent trajectory (saved after every step)."""
    if not traj:
        return dict(steps=0, tokens_in=0, tokens_out=0, cost_usd=0.0, price_source="no_calls",
                    tokens_complete=True)
    tin = tout = calls = 0
    cost, sources, complete = 0.0, set(), True
    for msg in traj.get("messages", []):
        extra = msg.get("extra") or {}
        if "response" not in extra:
            continue
        calls += 1
        a, b = _usage(extra["response"])
        if a is None or b is None:
            complete = False
        tin += a or 0
        tout += b or 0
        cost += extra.get("cost", 0.0) or 0.0
        sources.add(extra.get("price_source", "unknown"))
    steps = traj.get("info", {}).get("model_stats", {}).get("api_calls", calls)
    if calls == 0:
        cost_usd, source = 0.0, "no_calls"
    elif "unknown" in sources:
        cost_usd, source = None, "unknown"
    else:
        cost_usd, source = cost, "+".join(sorted(sources))
    return dict(steps=steps, tokens_in=tin, tokens_out=tout, cost_usd=cost_usd,
                price_source=source, tokens_complete=complete)


def classify(exit_status: str | None) -> str:
    """submitted | limit | error: the coarse run-time signal the cascade escalates on."""
    if exit_status == "Submitted":
        return "submitted"
    if exit_status in LIMIT_STATUSES:
        return "limit"
    return "error"


def which_limit(exit_status: str | None, steps: int, cost: float | None, limits: "Limits") -> str | None:
    if exit_status in ("TimeExceeded", "HardTimeout"):
        return "time"
    if exit_status != "LimitsExceeded":
        return None
    if steps >= limits.step_limit:
        return "steps"
    if cost is not None and cost >= limits.cost_cap:
        return "cost"
    return "steps_or_cost"


# --------------------------------------------------------------------------- docker + task runner

@dataclass
class Limits:
    cost_cap: float = DEFAULT_COST_CAP
    step_limit: int = DEFAULT_STEP_LIMIT
    timeout_s: int = DEFAULT_TIMEOUT_S


@dataclass
class TaskRun:
    """What happened when one task ran (before cleanup)."""

    exit_status: str
    patch: str = ""
    trajectory: dict | None = None
    error: str | None = None
    seconds: float = 0.0
    pull_seconds: float = 0.0
    litellm: dict | None = None  # litellm_info() as seen by the worker


class Docker(Protocol):
    def pull(self, image: str, timeout: int) -> None: ...
    def remove_containers(self, label: str) -> int: ...
    def remove_image(self, image: str) -> None: ...


class DockerCli:
    def __init__(self, executable: str = os.getenv("MSWEA_DOCKER_EXECUTABLE", "docker")):
        self.exe = executable

    def _run(self, *args, timeout: int = 120) -> subprocess.CompletedProcess:
        return subprocess.run([self.exe, *args], capture_output=True, text=True, timeout=timeout)

    def pull(self, image: str, timeout: int) -> None:
        r = self._run("pull", image, timeout=timeout)
        if r.returncode != 0:
            raise RuntimeError(f"docker pull {image} failed: {r.stderr.strip()[-500:]}")

    def remove_containers(self, label: str) -> int:
        r = self._run("ps", "-aq", "--filter", f"label={LABEL_KEY}={label}")
        ids = r.stdout.split()
        if ids:
            rm = self._run("rm", "-f", *ids)
            if rm.returncode != 0:
                raise RuntimeError(f"docker rm -f failed: {rm.stderr.strip()[-500:]}")
        return len(ids)

    def remove_image(self, image: str) -> None:
        r = self._run("rmi", image)
        if r.returncode != 0 and "No such image" not in r.stderr:
            raise RuntimeError(f"docker rmi {image} failed: {r.stderr.strip()[-500:]}")


def image_name(instance: dict) -> str:
    from minisweagent.run.benchmarks.swebench import get_swebench_docker_image_name

    return get_swebench_docker_image_name(instance)


# (instance, model, limits, label, work_dir) -> TaskRun
TaskRunner = Callable[[dict, str, Limits, str, Path], TaskRun]


class SubprocessTaskRunner:
    """Pulls the image, runs the worker in its own process group, kills it on timeout."""

    def __init__(self, docker: Docker, pull_timeout: int = DEFAULT_PULL_TIMEOUT_S,
                 kill_grace: int = KILL_GRACE_S, worker_cmd: list[str] | None = None):
        self.docker = docker
        self.pull_timeout = pull_timeout
        self.kill_grace = kill_grace
        self.worker_cmd = worker_cmd or [sys.executable, "-m", "inferno.swebench_run", "--worker"]

    def __call__(self, instance: dict, model: str, limits: Limits, label: str, work_dir: Path) -> TaskRun:
        t0 = time.monotonic()
        self.docker.pull(image_name(instance), self.pull_timeout)
        pull_seconds = time.monotonic() - t0

        work_dir.mkdir(parents=True, exist_ok=True)
        iid = instance["instance_id"]
        spec = {
            "instance": {k: instance[k] for k in ("instance_id", "problem_statement")
                         if k in instance} | {"image_name": image_name(instance)},
            "model": model,
            "limits": vars(limits),
            "label": label,
            "traj_path": str(work_dir / f"{iid}.traj.json"),
            "result_path": str(work_dir / f"{iid}.result.json"),
        }
        spec_path = work_dir / f"{iid}.spec.json"
        spec_path.write_text(json.dumps(spec))
        Path(spec["result_path"]).unlink(missing_ok=True)

        t1 = time.monotonic()
        proc = subprocess.Popen([*self.worker_cmd, str(spec_path)], start_new_session=True)
        try:
            proc.wait(timeout=limits.timeout_s + self.kill_grace)
            killed = False
        except subprocess.TimeoutExpired:
            os.killpg(proc.pid, signal.SIGKILL)  # the worker and anything it started
            proc.wait()
            killed = True
        except BaseException:  # Ctrl-C: don't leave the worker running
            os.killpg(proc.pid, signal.SIGKILL)
            proc.wait()
            raise
        seconds = time.monotonic() - t1

        traj = _read_json(Path(spec["traj_path"]))
        result = _read_json(Path(spec["result_path"])) or {}
        lit = result.get("litellm")  # written early, so it survives a kill
        if killed:
            return TaskRun("HardTimeout", "", traj, f"worker killed after {seconds:.0f}s",
                           seconds, pull_seconds, lit)
        if result.get("exit_status") is None:
            return TaskRun("WorkerCrashed", "", traj, f"worker exited {proc.returncode} without a result",
                           seconds, pull_seconds, lit)
        return TaskRun(result["exit_status"], result.get("submission") or "",
                       traj, result.get("error"), seconds, pull_seconds, lit)


def _read_json(path: Path, default=None):
    try:
        return json.loads(path.read_text())
    except (OSError, json.JSONDecodeError):
        return default


# --------------------------------------------------------------------------- worker (child process)

def build_config(model: str, limits: Limits, label: str, traj_path: str | None = None) -> dict:
    """mini-swe-agent's swebench.yaml with our model, limits and container label merged in."""
    from minisweagent.config import get_config_from_spec
    from minisweagent.run.benchmarks.swebench import DEFAULT_CONFIG_FILE
    from minisweagent.utils.serialize import recursive_merge

    ours = {
        "agent": {
            "step_limit": limits.step_limit,
            "cost_limit": limits.cost_cap,
            "wall_time_limit_seconds": limits.timeout_s,
            "output_path": traj_path,
        },
        "model": {
            "model_name": model,
            "model_class": "inferno.swebench_run.PricedLitellmModel",
            "cost_tracking": "ignore_errors",  # we price calls ourselves; see cost_of_call
        },
        "environment": {
            "environment_class": "docker",
            # --rm as in mini's default, plus a label so the parent can always find it.
            "run_args": ["--rm", "--label", f"{LABEL_KEY}={label}"],
            # The container kills itself eventually even if every cleanup fails.
            "container_timeout": f"{limits.timeout_s + KILL_GRACE_S + 600}s",
            "pull_timeout": 300,  # the parent has already pulled it
        },
    }
    return recursive_merge(get_config_from_spec(DEFAULT_CONFIG_FILE), ours)


def _load_api_keys() -> None:
    from dotenv import load_dotenv

    load_dotenv(REPO_ROOT / ".env")
    # litellm's Together provider reads TOGETHERAI_API_KEY; our .env uses TOGETHER_API_KEY.
    if os.environ.get("TOGETHER_API_KEY") and not os.environ.get("TOGETHERAI_API_KEY"):
        os.environ["TOGETHERAI_API_KEY"] = os.environ["TOGETHER_API_KEY"]


def worker(spec_path: Path) -> int:
    """Child process: run one task with mini-swe-agent and write {exit_status, submission}."""
    from minisweagent.agents.default import DefaultAgent
    from minisweagent.models import get_model
    from minisweagent.run.benchmarks.swebench import get_sb_environment

    spec = json.loads(spec_path.read_text())
    _load_api_keys()
    limits = Limits(**spec["limits"])
    config = build_config(spec["model"], limits, spec["label"], spec["traj_path"])
    config["agent"]["output_path"] = Path(spec["traj_path"])
    result_path = Path(spec["result_path"])
    result: dict = {}
    env = None
    try:
        lit = litellm_info()
        result_path.write_text(json.dumps({"litellm": lit}))  # no exit_status yet: not finished
        if lit["price_map_source"] != "local":
            raise RuntimeError(f"litellm loaded its price map from {lit['price_map_source']!r}, not the "
                               "copy bundled with its version; LITELLM_LOCAL_MODEL_COST_MAP was not honoured")
        env = get_sb_environment(config, spec["instance"])
        agent = DefaultAgent(get_model(config=config["model"]), env, **config["agent"])
        info = agent.run(spec["instance"]["problem_statement"])
        result = {"exit_status": info.get("exit_status"), "submission": info.get("submission") or "",
                  "litellm": lit}
    except Exception as e:  # noqa: BLE001 - recorded as an error result
        result = {"exit_status": type(e).__name__, "submission": "",
                  "error": f"{e}\n{traceback.format_exc()[-3000:]}"}
    finally:
        if "litellm" not in result:
            result["litellm"] = _read_json(result_path, {}).get("litellm")
        result_path.write_text(json.dumps(result))
        if env is not None:
            env.cleanup()
    return 0


# --------------------------------------------------------------------------- batch

def latest_by_id(records: Iterable[dict]) -> dict[str, dict]:
    """Last line per instance_id wins (a --retry-errors rerun appends a newer line)."""
    out: dict[str, dict] = {}
    for r in records:
        out[r["instance_id"]] = r
    return out


def write_preds(preds_file: Path, records: Iterable[dict]) -> None:
    """The exact `swebench eval` predictions format, written atomically."""
    preds = {
        iid: {"model_name_or_path": r["model"], "instance_id": iid, "model_patch": r["patch"] or ""}
        for iid, r in latest_by_id(records).items()
    }
    preds_file.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=preds_file.parent, prefix=preds_file.name, suffix=".tmp")
    with os.fdopen(fd, "w") as f:
        json.dump(preds, f, indent=2)
        f.flush()
        os.fsync(f.fileno())
    os.replace(tmp, preds_file)


def cleanup(docker: Docker, label: str, image: str) -> dict:
    """Remove the task's containers, then its image. Never raises; reports what failed."""
    report = {"containers_removed": None, "image_removed": False, "cleanup_error": None}
    errors = []
    try:
        report["containers_removed"] = docker.remove_containers(label)
    except Exception as e:  # noqa: BLE001
        errors.append(f"containers: {e}")
    try:
        docker.remove_image(image)
        report["image_removed"] = True
    except Exception as e:  # noqa: BLE001
        errors.append(f"image: {e}")
    report["cleanup_error"] = "; ".join(errors) or None
    return report


@dataclass
class BatchOutcome:
    ran: list[str] = field(default_factory=list)
    skipped: list[str] = field(default_factory=list)
    cleanup_failed: list[str] = field(default_factory=list)


def run_tasks(
    instances: list[dict],
    run_id: str,
    model: str,
    runner: TaskRunner,
    docker: Docker,
    limits: Limits = Limits(),
    results_dir: Path = RESULTS_DIR,
    git_commit: str | None = None,
    git_dirty: bool | None = None,
    retry_errors: bool = False,
    limit: int | None = None,
    mini_version: str | None = None,
    litellm_version: str | None = None,
) -> BatchOutcome:
    run_file = results_dir / f"{run_id}.jsonl"
    preds_file = results_dir / f"{run_id}.preds.json"
    work_root = results_dir / run_id

    done = latest_by_id(load_results(run_file))
    todo, outcome = [], BatchOutcome()
    for inst in instances:
        prev = done.get(inst["instance_id"])
        if prev is not None and not (retry_errors and prev.get("outcome") == "error"):
            outcome.skipped.append(inst["instance_id"])
        else:
            todo.append(inst)
    if limit is not None:
        todo = todo[:limit]
    if outcome.skipped:
        print(f"Skipping {len(outcome.skipped)} task(s) already in {run_file.name}.")

    for i, inst in enumerate(todo, 1):
        iid = inst["instance_id"]
        image = image_name(inst)
        label = f"{run_id}.{iid}.{uuid.uuid4().hex[:8]}"
        print(f"[{i}/{len(todo)}] {iid} ... ", end="", flush=True)
        try:
            try:
                run = runner(inst, model, limits, label, work_root)
            except Exception as e:  # noqa: BLE001 - e.g. docker pull failed
                run = TaskRun(type(e).__name__, error=str(e)[-2000:])
        finally:  # also on Ctrl-C, which then propagates without recording the task
            report = cleanup(docker, label, image)

        m = trajectory_metrics(run.trajectory)
        lit = run.litellm or {}
        record = {
            "run_id": run_id,
            "instance_id": iid,
            "repo": repo_of(inst),
            "model": model,
            "exit_status": run.exit_status,
            "outcome": classify(run.exit_status),
            "limit_hit": which_limit(run.exit_status, m["steps"], m["cost_usd"], limits),
            "patch": run.patch,
            "steps": m["steps"],
            "tokens_in": m["tokens_in"],
            "tokens_out": m["tokens_out"],
            "tokens_complete": m["tokens_complete"],
            "cost_usd": m["cost_usd"],
            "price_source": m["price_source"],  # litellm / table / litellm+table / unknown / no_calls
            "seconds": round(run.seconds, 1),
            "pull_seconds": round(run.pull_seconds, 1),
            "limits": vars(limits),
            "image": image,
            **report,
            "error": run.error,
            "git_commit": git_commit,
            "git_dirty": git_dirty,
            "mini_swe_agent_version": mini_version,
            "litellm_version": lit.get("litellm_version") or litellm_version,
            "price_map_source": lit.get("price_map_source"),  # None: the worker never got that far
            "price_map_revision": lit.get("price_map_revision"),
            "finished_at": dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds"),
        }
        append_result(run_file, record)
        write_preds(preds_file, load_results(run_file))
        outcome.ran.append(iid)
        if report["cleanup_error"]:
            outcome.cleanup_failed.append(iid)
        cost = "unknown" if m["cost_usd"] is None else f"${m['cost_usd']:.4f}"
        print(f"{run.exit_status} | {m['steps']} steps | {cost} | {run.seconds:.0f}s"
              + (f" | CLEANUP FAILED: {report['cleanup_error']}" if report["cleanup_error"] else ""))
    return outcome


def summarize(records: list[dict]) -> str:
    latest = list(latest_by_id(records).values())
    n = len(latest)
    by = {k: sum(r["outcome"] == k for r in latest) for k in ("submitted", "limit", "error")}
    costs = [r["cost_usd"] for r in latest]
    known = [c for c in costs if c is not None]
    cost = f"${sum(known):.4f}" + (f" (+{len(costs) - len(known)} unknown)" if len(known) < n else "")
    return "\n".join([
        f"Tasks recorded:   {n}",
        f"Submitted:        {by['submitted']}",
        f"Limit hit:        {by['limit']}",
        f"Error:            {by['error']}",
        f"Non-empty patch:  {sum(bool(r['patch']) for r in latest)}",
        f"Tokens in / out:  {sum(r['tokens_in'] for r in latest)} / {sum(r['tokens_out'] for r in latest)}",
        f"Cost:             {cost}",
    ])


def load_instances(task_file: Path = TASKS_FILE) -> list[dict]:
    """The sampled rows (task-file order), with problem statements from the dataset."""
    from datasets import load_dataset

    tasks = load_task_file(task_file)
    rows = {r["instance_id"]: r for r in load_dataset(tasks["dataset"], split=tasks["split"])}
    missing = [i for i in tasks["instance_ids"] if i not in rows]
    if missing:
        raise SystemExit(f"error: {len(missing)} task(s) not in {tasks['dataset']}: {missing}")
    return [dict(rows[i]) for i in tasks["instance_ids"]]


def main(argv: list[str] | None = None) -> int:
    argv = sys.argv[1:] if argv is None else argv
    if argv[:1] == ["--worker"]:
        return worker(Path(argv[1]))

    p = argparse.ArgumentParser(description="Run mini-swe-agent on the 25-task SWE-bench Verified sample.")
    p.add_argument("--model", required=True, help="litellm model name, e.g. together_ai/openai/gpt-oss-120b")
    p.add_argument("--run-id", required=True)
    p.add_argument("--cost-cap", type=float, default=DEFAULT_COST_CAP, help="USD per task (default 0.50)")
    p.add_argument("--step-limit", type=int, default=DEFAULT_STEP_LIMIT, help="model calls per task (default 50)")
    p.add_argument("--timeout", type=int, default=DEFAULT_TIMEOUT_S, help="seconds per task (default 1200)")
    p.add_argument("--pull-timeout", type=int, default=DEFAULT_PULL_TIMEOUT_S)
    p.add_argument("--limit", type=int, help="run at most N tasks not already done")
    p.add_argument("--retry-errors", action="store_true", help="rerun tasks whose last result is an error")
    p.add_argument("--allow-unknown-cost", action="store_true",
                   help="run even if the model's price is unknown (the cost cap then can't trigger)")
    p.add_argument("--tasks", type=Path, default=TASKS_FILE)
    args = p.parse_args(argv)
    if args.cost_cap <= 0 or args.step_limit < 1 or args.timeout < 1:
        p.error("--cost-cap, --step-limit and --timeout must be positive")
    if args.limit is not None and args.limit < 1:
        p.error("--limit must be at least 1")
    if not price_known(args.model) and not args.allow_unknown_cost:
        p.error(f"no price known for {args.model!r} (litellm or PRICES_PER_MILLION), so the cost cap "
                "can't work. Add it to the table, or pass --allow-unknown-cost.")

    run_file = RESULTS_DIR / f"{args.run_id}.jsonl"
    models = {r["model"] for r in load_results(run_file)}
    if models - {args.model}:
        p.error(f"{run_file.name} already has results for {sorted(models)}; use another --run-id")

    import minisweagent

    parent_litellm = litellm_info()
    if parent_litellm["price_map_source"] != "local":
        print(f"error: litellm loaded a {parent_litellm['price_map_source']!r} price map, not the bundled one",
              file=sys.stderr)
        return 2
    commit, dirty = git_info()
    if dirty:
        print(f"warning: uncommitted changes; results will be tagged {commit} but may not match it",
              file=sys.stderr)
    limits = Limits(args.cost_cap, args.step_limit, args.timeout)
    instances = load_instances(args.tasks)
    print(f"Run {args.run_id} | {args.model} | {len(instances)} tasks | cap ${limits.cost_cap}, "
          f"{limits.step_limit} steps, {limits.timeout_s}s | commit {commit} | "
          f"litellm {parent_litellm['litellm_version']} (bundled prices)")

    docker = DockerCli()
    outcome = run_tasks(
        instances, args.run_id, args.model, SubprocessTaskRunner(docker, args.pull_timeout), docker,
        limits, git_commit=commit, git_dirty=dirty, retry_errors=args.retry_errors,
        limit=args.limit, mini_version=minisweagent.__version__,
        litellm_version=parent_litellm["litellm_version"],
    )
    print("\n" + summarize(load_results(run_file)))
    print(f"Predictions: {RESULTS_DIR / f'{args.run_id}.preds.json'}")
    if outcome.cleanup_failed:
        print(f"\nCleanup failed for {len(outcome.cleanup_failed)} task(s): "
              f"{', '.join(outcome.cleanup_failed)}. Check `docker ps -a` / `docker images`.")
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
