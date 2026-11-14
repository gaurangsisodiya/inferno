"""Pick a fixed 25-task sample of SWE-bench Verified.

    uv run python -m inferno.swebench_tasks          # writes data/swebench_25.json

The sample is deterministic: instances are sorted by instance_id, shuffled with
random.Random(42), then taken in that order, skipping any task whose repo
already has MAX_PER_REPO tasks in the sample. (Verified is ~46% django, so a
plain random 25 would be dominated by it.) If the cap makes 25 impossible,
the remaining slots are filled from the skipped tasks, in shuffled order.

The file stores only the IDs; swebench_run loads the problem statements from
the same dataset and refuses to run if any ID is missing.
"""

from __future__ import annotations

import argparse
import json
import random
from collections import Counter
from pathlib import Path

from inferno.client import REPO_ROOT

DATASET = "princeton-nlp/SWE-Bench_Verified"
SPLIT = "test"
SEED = 42
N_TASKS = 25
MAX_PER_REPO = 8
TASKS_FILE = REPO_ROOT / "data" / "swebench_25.json"


def repo_of(instance: dict) -> str:
    """'django/django' from the row, or from the ID ('django__django-11099') if the row has no repo."""
    if instance.get("repo"):
        return instance["repo"]
    owner, rest = instance["instance_id"].split("__", 1)
    return f"{owner}/{rest.rsplit('-', 1)[0]}"


def sample_tasks(
    instances: list[dict], n: int = N_TASKS, seed: int = SEED, max_per_repo: int = MAX_PER_REPO
) -> list[str]:
    """Deterministic sample of `n` instance IDs, at most `max_per_repo` per repo when possible.

    Independent of the order of `instances` (they are sorted by ID first).
    """
    ordered = sorted(instances, key=lambda r: r["instance_id"])
    random.Random(seed).shuffle(ordered)
    picked, skipped, per_repo = [], [], Counter()
    for row in ordered:
        if len(picked) == n:
            break
        repo = repo_of(row)
        if per_repo[repo] < max_per_repo:
            picked.append(row["instance_id"])
            per_repo[repo] += 1
        else:
            skipped.append(row["instance_id"])
    picked += skipped[: n - len(picked)]  # only when the cap can't be met
    return picked


def load_task_file(path: Path = TASKS_FILE) -> dict:
    return json.loads(path.read_text())


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Write the fixed SWE-bench Verified sample.")
    parser.add_argument("--out", type=Path, default=TASKS_FILE)
    args = parser.parse_args(argv)

    from datasets import load_dataset  # slow import; only needed here

    rows = [{"instance_id": r["instance_id"], "repo": r["repo"]}
            for r in load_dataset(DATASET, split=SPLIT)]
    ids = sample_tasks(rows)
    repo_by_id = {r["instance_id"]: r["repo"] for r in rows}
    counts = Counter(repo_by_id[i] for i in ids)
    data = {
        "dataset": DATASET,
        "split": SPLIT,
        "seed": SEED,
        "max_per_repo": MAX_PER_REPO,
        "dataset_size": len(rows),
        "repo_counts": dict(sorted(counts.items(), key=lambda kv: (-kv[1], kv[0]))),
        "instance_ids": ids,
    }
    args.out.parent.mkdir(parents=True, exist_ok=True)
    args.out.write_text(json.dumps(data, indent=2) + "\n")
    print(f"Wrote {len(ids)} tasks from {len(rows)} to {args.out}")
    for repo, c in data["repo_counts"].items():
        print(f"  {c:2d}  {repo}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
