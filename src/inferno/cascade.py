"""Simulate a cheap-then-strong cascade on the graded SWE-bench runs.

    uv run python -m inferno.cascade

Inputs (results/swebench/):
  cheap-v1.jsonl, strong-v1.jsonl   per-task run records from swebench_run
  graded/<model>.grade-<run>.json   swebench run_evaluation reports for each run
  graded/gold.gold-410.json         the grader sanity check (gold patches)

The cascade is decided from RUN-TIME SIGNALS ONLY. A task is escalated to the
strong model when the cheap run hit a limit, errored, or submitted an empty
patch; otherwise the cheap patch is kept. `escalation_reason` takes a single
run record and nothing else, so grading results cannot reach the decision.
Grading is used afterwards, only to score each strategy.

    cascade cost per task = cheap cost + (strong cost if escalated)

The "oracle" row routes each task with hindsight (the cheaper model that
resolves it, if either does). It needs grading results, so it is an upper
bound for comparison, not a strategy you could run.

Outputs: results/swebench/summary.json, summary.md and cascade.png.
"""

from __future__ import annotations

import argparse
import json
import subprocess
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

from inferno.run_all import git_info, load_results
from inferno.swebench_run import RESULTS_DIR, latest_by_id
from inferno.swebench_tasks import TASKS_FILE, load_task_file

CHEAP_RUN = "cheap-v1"
STRONG_RUN = "strong-v1"
GRADED_DIR = RESULTS_DIR / "graded"
GOLD_REPORT = GRADED_DIR / "gold.gold-410.json"
GRADER_PYTHON = Path.home() / "swebench-eval-410" / "bin" / "python"

RULE = ("escalate to the strong model when the cheap run hit a limit, errored, "
        "or submitted an empty patch; otherwise keep the cheap patch")
ORACLE_NOTE = "upper bound: routes with hindsight from grading results, not a real strategy"


# --------------------------------------------------------------------------- the decision

def escalation_reason(cheap_record: dict) -> str | None:
    """Why the cascade escalates this task ('error' | 'limit' | 'empty_patch'), or None to keep it.

    Uses only fields the runner wrote at run time. Takes no grading input by design.
    """
    if cheap_record.get("outcome") == "error" or cheap_record.get("error"):
        return "error"
    if cheap_record.get("outcome") == "limit" or cheap_record.get("limit_hit"):
        return "limit"
    if not (cheap_record.get("patch") or "").strip():
        return "empty_patch"
    return None


# --------------------------------------------------------------------------- inputs

class InputError(Exception):
    pass


def load_run(path: Path, task_ids: list[str]) -> dict[str, dict]:
    """Latest record per task (retries append newer lines), checked to cover every task once."""
    latest = latest_by_id(load_results(path))
    missing = [t for t in task_ids if t not in latest]
    if missing:
        raise InputError(f"{path.name}: no record for {len(missing)} task(s): {missing[:5]}")
    models = {latest[t]["model"] for t in task_ids}
    if len(models) != 1:
        raise InputError(f"{path.name}: more than one model in the run: {sorted(models)}")
    return {t: latest[t] for t in task_ids}


def model_slug(model: str) -> str:
    """How swebench names a report file: '/' in model_name_or_path becomes '__'."""
    return model.replace("/", "__")


def find_report(graded_dir: Path, run_id: str, model: str) -> Path:
    """graded/<model>.grade-<run_id>.json, and only that."""
    path = graded_dir / f"{model_slug(model)}.grade-{run_id}.json"
    if not path.exists():
        others = sorted(p.name for p in graded_dir.glob(f"*.grade-{run_id}.json"))
        hint = f" (found {others}: wrong model?)" if others else ""
        raise InputError(f"no grading report {path}{hint}")
    return path


def resolved_ids(report: dict, task_ids: list[str], name: str) -> set[str]:
    """Which of our tasks the grader resolved, after checking it graded every one of them.

    A task counts as graded when it was completed, had an empty patch (swebench skips
    those, never resolved) or errored in the harness (never resolved).
    """
    graded = set(report["completed_ids"]) | set(report["empty_patch_ids"]) | set(report["error_ids"])
    ungraded = [t for t in task_ids if t not in graded]
    if ungraded:
        raise InputError(f"{name}: {len(ungraded)} task(s) not graded: {ungraded[:5]}")
    return set(report["resolved_ids"]) & set(task_ids)


def grader_check(path: Path) -> dict:
    """The gold-patch sanity run: every gold patch it completed should resolve."""
    rep = json.loads(path.read_text())
    completed, resolved = rep["completed_instances"], rep["resolved_instances"]
    return {
        "report": path.name,
        "completed": completed,
        "resolved": resolved,
        "resolved_ids": sorted(rep["resolved_ids"]),
        "passed": completed > 0 and resolved == completed and rep["error_instances"] == 0,
    }


def swebench_version(grader_python: Path) -> str:
    """The swebench version installed in the grader's venv."""
    try:
        out = subprocess.run(
            [str(grader_python), "-I", "-c",
             "import importlib.metadata as m; print(m.version('swebench'))"],
            capture_output=True, text=True, check=True, timeout=60,
        )
    except (OSError, subprocess.SubprocessError) as e:
        raise InputError(f"can't read the swebench version from {grader_python} ({e}); "
                         "pass --swebench-version") from e
    return out.stdout.strip()


# --------------------------------------------------------------------------- simulation

@dataclass
class Task:
    instance_id: str
    cheap_cost: float
    strong_cost: float
    cheap_resolved: bool
    strong_resolved: bool
    escalation: str | None  # from the cheap run record only

    @property
    def escalated(self) -> bool:
        return self.escalation is not None

    @property
    def cascade_resolved(self) -> bool:
        return self.strong_resolved if self.escalated else self.cheap_resolved

    @property
    def cascade_cost(self) -> float:
        return self.cheap_cost + (self.strong_cost if self.escalated else 0.0)

    @property
    def oracle_choice(self) -> str:
        """Cheapest model that resolves the task; cheap if neither does."""
        if self.cheap_resolved or not self.strong_resolved:
            return "cheap"
        return "strong"


def build_tasks(task_ids: list[str], cheap: dict[str, dict], strong: dict[str, dict],
                cheap_resolved: set[str], strong_resolved: set[str]) -> list[Task]:
    return [
        Task(
            instance_id=t,
            cheap_cost=float(cheap[t]["cost_usd"]),
            strong_cost=float(strong[t]["cost_usd"]),
            cheap_resolved=t in cheap_resolved,
            strong_resolved=t in strong_resolved,
            escalation=escalation_reason(cheap[t]),
        )
        for t in task_ids
    ]


def metrics(name: str, resolved: list[bool], costs: list[float], escalated: int | None,
            note: str | None = None) -> dict:
    n, k, total = len(resolved), sum(resolved), sum(costs)
    return {
        "strategy": name,
        "tasks": n,
        "resolved": k,
        "resolve_rate": k / n if n else 0.0,
        "total_cost_usd": total,
        "cost_per_task_usd": total / n if n else 0.0,
        "cost_per_resolved_usd": total / k if k else None,
        "escalated": escalated,
        "note": note,
    }


def strategies(tasks: list[Task]) -> list[dict]:
    oracle_cost = [t.cheap_cost if t.oracle_choice == "cheap" else t.strong_cost for t in tasks]
    return [
        metrics("cheap", [t.cheap_resolved for t in tasks], [t.cheap_cost for t in tasks], None),
        metrics("strong", [t.strong_resolved for t in tasks], [t.strong_cost for t in tasks], None),
        metrics("cascade", [t.cascade_resolved for t in tasks], [t.cascade_cost for t in tasks],
                sum(t.escalated for t in tasks)),
        metrics("oracle", [t.cheap_resolved or t.strong_resolved for t in tasks], oracle_cost,
                sum(t.oracle_choice == "strong" for t in tasks), ORACLE_NOTE),
    ]


def cost_caveats(name: str, records: dict[str, dict]) -> list[str]:
    """Tasks whose recorded cost may be an undercount."""
    out = []
    for t, r in records.items():
        if r.get("price_source") == "unknown":
            out.append(f"{name} {t}: price unknown, cost recorded as 0")
        elif r.get("tokens_complete") is False:
            out.append(f"{name} {t}: some calls had no token usage; cost is a lower bound")
    return out


# --------------------------------------------------------------------------- outputs

def build_summary(*, task_file: dict, cheap: dict[str, dict], strong: dict[str, dict],
                  cheap_report: Path, strong_report: Path, gold: dict,
                  version: str, commit: str | None, dirty: bool | None) -> dict:
    task_ids = task_file["instance_ids"]
    cheap_res = resolved_ids(json.loads(cheap_report.read_text()), task_ids, cheap_report.name)
    strong_res = resolved_ids(json.loads(strong_report.read_text()), task_ids, strong_report.name)
    tasks = build_tasks(task_ids, cheap, strong, cheap_res, strong_res)
    reasons: dict[str, int] = {}
    for t in tasks:
        if t.escalation:
            reasons[t.escalation] = reasons.get(t.escalation, 0) + 1

    def run_info(records: dict[str, dict], report: Path) -> dict:
        r = next(iter(records.values()))
        return {"run_id": r["run_id"], "model": r["model"], "harness": r.get("harness"),
                "report": report.name}

    return {
        "generated_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "git_commit": commit,
        "git_dirty": dirty,
        "swebench_version": version,
        "dataset": task_file.get("dataset"),
        "split": task_file.get("split"),
        "tasks": len(task_ids),
        "runs": {"cheap": run_info(cheap, cheap_report), "strong": run_info(strong, strong_report)},
        "grader_check": gold,
        "rule": RULE,
        "escalation_reasons": dict(sorted(reasons.items())),
        "strategies": strategies(tasks),
        "cost_caveats": cost_caveats("cheap", cheap) + cost_caveats("strong", strong),
        "per_task": [
            {"instance_id": t.instance_id, "escalation": t.escalation,
             "cheap_cost_usd": t.cheap_cost, "strong_cost_usd": t.strong_cost,
             "cheap_resolved": t.cheap_resolved, "strong_resolved": t.strong_resolved,
             "cascade_resolved": t.cascade_resolved, "cascade_cost_usd": t.cascade_cost,
             "oracle_choice": t.oracle_choice}
            for t in tasks
        ],
    }


def _usd(x: float | None) -> str:
    return "n/a" if x is None else f"${x:.4f}"


def summary_markdown(s: dict) -> str:
    cheap, strong = s["runs"]["cheap"], s["runs"]["strong"]
    lines = [
        "# SWE-bench Verified: cheap, strong and cascade",
        "",
        f"{s['tasks']} tasks from {s['dataset']} ({s['split']}). "
        f"Cheap: `{cheap['model']}` ({cheap['run_id']}). Strong: `{strong['model']}` ({strong['run_id']}).",
        "",
        "| Strategy | Resolved | Resolve rate | Total cost | Cost / task | Cost / resolved | Escalated |",
        "|---|---:|---:|---:|---:|---:|---:|",
    ]
    for r in s["strategies"]:
        name = "oracle (upper bound)*" if r["strategy"] == "oracle" else r["strategy"]
        esc = "-" if r["escalated"] is None else str(r["escalated"])
        lines.append(
            f"| {name} | {r['resolved']}/{r['tasks']} | {100 * r['resolve_rate']:.0f}% "
            f"| {_usd(r['total_cost_usd'])} | {_usd(r['cost_per_task_usd'])} "
            f"| {_usd(r['cost_per_resolved_usd'])} | {esc} |"
        )
    reasons = ", ".join(f"{k} {v}" for k, v in s["escalation_reasons"].items()) or "none"
    g = s["grader_check"]
    lines += [
        "",
        f"**Cascade rule** (run-time signals only): {s['rule']}. "
        "Cascade cost per task = cheap cost + strong cost if escalated.",
        f"Escalation reasons: {reasons}.",
        "",
        "\\* The oracle routes each task with hindsight to the cheaper model that resolves it "
        "(cheap if neither does), paying only that model. It uses grading results, so it is "
        "an upper bound, not a strategy.",
        "",
        f"Graded with swebench {s['swebench_version']}. Grader check ({g['report']}): "
        f"{g['resolved']}/{g['completed']} gold patches resolved, "
        f"{'passed' if g['passed'] else 'FAILED'}. "
        f"Generated at commit `{s['git_commit']}`{' (dirty)' if s['git_dirty'] else ''}.",
    ]
    if s["cost_caveats"]:
        lines += ["", "Cost caveats:"] + [f"- {c}" for c in s["cost_caveats"]]
    return "\n".join(lines) + "\n"


# Colours: the dataviz skill's validated categorical slots 1-3 (all-pairs pass, light mode).
_COLOURS = {"cheap": "#2a78d6", "strong": "#eb6834", "cascade": "#1baf7a"}


def plot(s: dict, out: Path) -> None:
    import matplotlib

    matplotlib.use("Agg")
    import matplotlib.pyplot as plt

    rows = [r for r in s["strategies"] if r["strategy"] in _COLOURS]
    models = {"cheap": s["runs"]["cheap"]["model"], "strong": s["runs"]["strong"]["model"]}
    fig, ax = plt.subplots(figsize=(7, 4.6), dpi=150)
    fig.patch.set_facecolor("#fcfcfb")
    ax.set_facecolor("#fcfcfb")
    for r in rows:
        x, y = r["total_cost_usd"], 100 * r["resolve_rate"]
        ax.scatter(x, y, s=110, color=_COLOURS[r["strategy"]], edgecolor="#fcfcfb", linewidth=2,
                   zorder=3, label=r["strategy"])
        who = models.get(r["strategy"], f"{r['escalated']} escalated")
        ax.annotate(f"{r['strategy']}\n{who.split('/')[-1]}\n{y:.0f}%, ${x:.2f}",
                    (x, y), xytext=(10, -4), textcoords="offset points", va="top",
                    fontsize=8.5, color="#0b0b0b")
    xmax = max(r["total_cost_usd"] for r in rows)
    ax.set_xlim(0, xmax * 1.45)
    ax.set_ylim(0, 105)
    ax.set_xlabel(f"Total cost for {s['tasks']} tasks (USD)", color="#52514e")
    ax.set_ylabel("Resolve rate (%)", color="#52514e")
    ax.set_title("SWE-bench Verified: resolve rate vs total cost", loc="left", fontsize=11,
                 color="#0b0b0b")
    ax.grid(color="#e1e0d9", linewidth=0.6)
    ax.set_axisbelow(True)
    for side in ("top", "right"):
        ax.spines[side].set_visible(False)
    for side in ("left", "bottom"):
        ax.spines[side].set_color("#c3c2b7")
    ax.tick_params(colors="#52514e", labelsize=8.5)
    ax.legend(frameon=False, fontsize=8.5, loc="lower right")
    fig.tight_layout()
    out.parent.mkdir(parents=True, exist_ok=True)
    fig.savefig(out, facecolor=fig.get_facecolor())
    plt.close(fig)


# --------------------------------------------------------------------------- CLI

def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(description="Cascade simulation over graded SWE-bench runs.")
    p.add_argument("--results-dir", type=Path, default=RESULTS_DIR)
    p.add_argument("--graded-dir", type=Path, default=None, help="default: <results-dir>/graded")
    p.add_argument("--tasks-file", type=Path, default=TASKS_FILE)
    p.add_argument("--cheap-run", default=CHEAP_RUN)
    p.add_argument("--strong-run", default=STRONG_RUN)
    p.add_argument("--gold-report", type=Path, default=None,
                   help="default: <graded-dir>/gold.gold-410.json")
    p.add_argument("--swebench-version", default=None,
                   help=f"default: read from {GRADER_PYTHON}")
    p.add_argument("--grader-python", type=Path, default=GRADER_PYTHON)
    args = p.parse_args(argv)

    graded = args.graded_dir or args.results_dir / "graded"
    gold_path = args.gold_report or graded / GOLD_REPORT.name
    try:
        task_file = load_task_file(args.tasks_file)
        ids = task_file["instance_ids"]
        cheap = load_run(args.results_dir / f"{args.cheap_run}.jsonl", ids)
        strong = load_run(args.results_dir / f"{args.strong_run}.jsonl", ids)
        cheap_report = find_report(graded, args.cheap_run, next(iter(cheap.values()))["model"])
        strong_report = find_report(graded, args.strong_run, next(iter(strong.values()))["model"])
        version = args.swebench_version or swebench_version(args.grader_python)
        commit, dirty = git_info()
        s = build_summary(task_file=task_file, cheap=cheap, strong=strong,
                          cheap_report=cheap_report, strong_report=strong_report,
                          gold=grader_check(gold_path), version=version,
                          commit=commit, dirty=dirty)
    except (InputError, OSError, KeyError, json.JSONDecodeError) as e:
        print(f"error: {e}")
        return 1

    out = args.results_dir
    (out / "summary.json").write_text(json.dumps(s, indent=2) + "\n")
    md = summary_markdown(s)
    (out / "summary.md").write_text(md)
    plot(s, out / "cascade.png")
    print(md)
    print(f"Wrote {out / 'summary.json'}, {out / 'summary.md'}, {out / 'cascade.png'}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
