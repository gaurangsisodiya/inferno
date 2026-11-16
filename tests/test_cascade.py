"""Tests for the cascade simulation. Small fake run files and reports only."""

import inspect
import json
import os

os.environ.setdefault("LITELLM_LOCAL_MODEL_COST_MAP", "True")

import pytest
from hypothesis import given, strategies as st

from inferno import cascade as c

CHEAP_MODEL = "prov/cheap-model"
STRONG_MODEL = "prov/org/strong-model"
IDS = ["a__a-1", "a__a-2", "b__b-1", "b__b-2"]


def rec(iid, model, run_id, *, outcome="submitted", limit_hit=None, patch="diff --git x", error=None,
        cost=0.1):
    return {"run_id": run_id, "instance_id": iid, "model": model, "outcome": outcome,
            "limit_hit": limit_hit, "patch": patch, "error": error, "cost_usd": cost,
            "price_source": "litellm", "tokens_complete": True, "harness": "backticks-v2"}


def report(ids, resolved, empty=(), errors=()):
    completed = [i for i in ids if i not in empty and i not in errors]
    return {"completed_ids": completed, "empty_patch_ids": list(empty), "error_ids": list(errors),
            "resolved_ids": list(resolved), "completed_instances": len(completed),
            "resolved_instances": len(resolved), "error_instances": len(errors)}


def cheap_records():
    return {
        "a__a-1": rec("a__a-1", CHEAP_MODEL, "cheap", cost=0.10),                       # kept
        "a__a-2": rec("a__a-2", CHEAP_MODEL, "cheap", outcome="limit", limit_hit="steps",
                      patch="", cost=0.30),                                              # limit
        "b__b-1": rec("b__b-1", CHEAP_MODEL, "cheap", patch="  \n", cost=0.05),          # empty
        "b__b-2": rec("b__b-2", CHEAP_MODEL, "cheap", cost=0.20),                       # kept
    }


def strong_records():
    return {i: rec(i, STRONG_MODEL, "strong", cost=1.0) for i in IDS}


# --------------------------------------------------------------------------- the decision

@pytest.mark.parametrize("record,expected", [
    (rec("x", CHEAP_MODEL, "r"), None),
    (rec("x", CHEAP_MODEL, "r", outcome="limit", limit_hit="steps", patch=""), "limit"),
    (rec("x", CHEAP_MODEL, "r", outcome="limit", limit_hit="cost"), "limit"),  # even with a patch
    (rec("x", CHEAP_MODEL, "r", outcome="error", patch="", error="boom"), "error"),
    (rec("x", CHEAP_MODEL, "r", error="cleanup failed"), "error"),
    (rec("x", CHEAP_MODEL, "r", patch=""), "empty_patch"),
    (rec("x", CHEAP_MODEL, "r", patch=None), "empty_patch"),
    (rec("x", CHEAP_MODEL, "r", patch=" \n\t"), "empty_patch"),
])
def test_escalation_reason(record, expected):
    assert c.escalation_reason(record) == expected


def test_escalation_takes_only_the_run_record():
    assert list(inspect.signature(c.escalation_reason).parameters) == ["cheap_record"]


@given(st.sets(st.sampled_from(IDS)), st.sets(st.sampled_from(IDS)))
def test_grading_results_never_affect_escalation(cheap_resolved, strong_resolved):
    """Any grading outcome gives the same escalation decisions (and so the same cascade cost)."""
    cheap, strong = cheap_records(), strong_records()
    tasks = c.build_tasks(IDS, cheap, strong, cheap_resolved, strong_resolved)
    assert [t.escalation for t in tasks] == [None, "limit", "empty_patch", None]
    assert [round(t.cascade_cost, 6) for t in tasks] == [0.10, 1.30, 1.05, 0.20]
    # extra fields that only grading could know are ignored by the decision
    graded = {i: {**r, "resolved": i in cheap_resolved} for i, r in cheap.items()}
    assert [c.escalation_reason(graded[i]) for i in IDS] == [t.escalation for t in tasks]


# --------------------------------------------------------------------------- simulation

def by_name(rows):
    return {r["strategy"]: r for r in rows}


def test_strategies():
    # cheap resolves a-1 only; strong resolves a-2, b-1 and a-1 (not b-2)
    tasks = c.build_tasks(IDS, cheap_records(), strong_records(),
                          {"a__a-1"}, {"a__a-1", "a__a-2", "b__b-1"})
    s = by_name(c.strategies(tasks))

    assert s["cheap"]["resolved"] == 1 and s["cheap"]["resolve_rate"] == 0.25
    assert s["cheap"]["total_cost_usd"] == pytest.approx(0.65)
    assert s["cheap"]["escalated"] is None

    assert s["strong"]["resolved"] == 3
    assert s["strong"]["total_cost_usd"] == pytest.approx(4.0)
    assert s["strong"]["cost_per_resolved_usd"] == pytest.approx(4.0 / 3)

    # cascade keeps a-1 (resolved) and b-2 (not), escalates a-2 and b-1 (both resolved by strong)
    assert s["cascade"]["resolved"] == 3
    assert s["cascade"]["escalated"] == 2
    assert s["cascade"]["total_cost_usd"] == pytest.approx(0.65 + 2.0)
    assert s["cascade"]["cost_per_task_usd"] == pytest.approx(2.65 / 4)

    # oracle: a-1 cheap, a-2 strong, b-1 strong, b-2 cheap (nobody resolves it)
    assert s["oracle"]["resolved"] == 3
    assert s["oracle"]["escalated"] == 2
    assert s["oracle"]["total_cost_usd"] == pytest.approx(0.10 + 1.0 + 1.0 + 0.20)
    assert "upper bound" in s["oracle"]["note"]


def test_escalated_task_is_scored_on_the_strong_run():
    # strong resolves nothing: escalations add cost and no resolves
    tasks = c.build_tasks(IDS, cheap_records(), strong_records(), {"a__a-1", "a__a-2"}, set())
    s = by_name(c.strategies(tasks))
    assert s["cascade"]["resolved"] == 1  # a-2 was resolved by cheap but escalated anyway
    assert s["strong"]["cost_per_resolved_usd"] is None


def test_resolved_ids_requires_every_task_graded():
    rep = report(IDS[:3], resolved=["a__a-1"])
    with pytest.raises(c.InputError, match="not graded"):
        c.resolved_ids(rep, IDS, "r.json")
    rep = report(IDS, resolved=["a__a-1", "zzz-other"], empty=["b__b-1"], errors=["b__b-2"])
    assert c.resolved_ids(rep, IDS, "r.json") == {"a__a-1"}


# --------------------------------------------------------------------------- end to end

def write_inputs(tmp_path, cheap_resolved, strong_resolved, extra_cheap_lines=()):
    results, graded = tmp_path / "results", tmp_path / "results" / "graded"
    graded.mkdir(parents=True)
    tasks_file = tmp_path / "tasks.json"
    tasks_file.write_text(json.dumps({"dataset": "fake/ds", "split": "test", "instance_ids": IDS}))
    lines = list(extra_cheap_lines) + list(cheap_records().values())
    (results / "cheap.jsonl").write_text("".join(json.dumps(r) + "\n" for r in lines))
    (results / "strong.jsonl").write_text(
        "".join(json.dumps(r) + "\n" for r in strong_records().values()))
    (graded / "prov__cheap-model.grade-cheap.json").write_text(
        json.dumps(report(IDS, cheap_resolved, empty=["a__a-2", "b__b-1"])))
    (graded / "prov__org__strong-model.grade-strong.json").write_text(
        json.dumps(report(IDS, strong_resolved)))
    (graded / "gold.gold-410.json").write_text(json.dumps(report(["a__a-1"], ["a__a-1"])))
    return results, tasks_file


def run_main(results, tasks_file):
    return c.main(["--results-dir", str(results), "--tasks-file", str(tasks_file),
                   "--cheap-run", "cheap", "--strong-run", "strong", "--swebench-version", "4.1.0"])


def test_main_writes_outputs(tmp_path, capsys):
    # an older errored line for a-1 is superseded by the later submitted one
    old = rec("a__a-1", CHEAP_MODEL, "cheap", outcome="error", patch="", error="api")
    results, tasks_file = write_inputs(tmp_path, ["a__a-1"], ["a__a-2"], extra_cheap_lines=[old])
    assert run_main(results, tasks_file) == 0

    s = json.loads((results / "summary.json").read_text())
    assert s["swebench_version"] == "4.1.0"
    assert "git_commit" in s and "git_dirty" in s
    assert s["grader_check"]["passed"] is True
    assert s["escalation_reasons"] == {"empty_patch": 1, "limit": 1}
    assert by_name(s["strategies"])["cascade"]["resolved"] == 2
    assert [r["strategy"] for r in s["strategies"]] == ["cheap", "strong", "cascade", "oracle"]

    md = (results / "summary.md").read_text()
    assert "| cascade | 2/4 | 50% |" in md
    assert "oracle (upper bound)" in md
    assert (results / "cascade.png").read_bytes()[:8] == b"\x89PNG\r\n\x1a\n"
    assert "| Strategy |" in capsys.readouterr().out


def test_main_grading_changes_scores_not_decisions(tmp_path):
    """Two opposite gradings: the per-task escalations and cascade costs are identical."""
    outs = []
    for i, (cr, sr) in enumerate([(IDS, IDS), ([], [])]):
        results, tasks_file = write_inputs(tmp_path / str(i), cr, sr)
        assert run_main(results, tasks_file) == 0
        outs.append(json.loads((results / "summary.json").read_text()))
    decisions = [[(t["escalation"], t["cascade_cost_usd"]) for t in s["per_task"]] for s in outs]
    assert decisions[0] == decisions[1]
    assert by_name(outs[0]["strategies"])["cascade"]["resolved"] != \
        by_name(outs[1]["strategies"])["cascade"]["resolved"]


def test_main_refuses_missing_or_mismatched_report(tmp_path, capsys):
    results, tasks_file = write_inputs(tmp_path, [], [])
    (results / "graded" / "prov__org__strong-model.grade-strong.json").rename(
        results / "graded" / "other__model.grade-strong.json")
    assert run_main(results, tasks_file) == 1
    assert "wrong model" in capsys.readouterr().out
    assert not (results / "summary.json").exists()


def test_main_refuses_run_missing_a_task(tmp_path, capsys):
    results, tasks_file = write_inputs(tmp_path, [], [])
    lines = (results / "strong.jsonl").read_text().splitlines()[:-1]
    (results / "strong.jsonl").write_text("\n".join(lines) + "\n")
    assert run_main(results, tasks_file) == 1
    assert "no record" in capsys.readouterr().out
