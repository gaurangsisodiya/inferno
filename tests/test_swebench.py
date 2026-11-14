"""Tests for the SWE-bench runner. Fakes only: no Docker, no model calls, no dataset download."""

import json
import os
import random
import sys
import time

# litellm otherwise downloads its price map at import time.
os.environ.setdefault("LITELLM_LOCAL_MODEL_COST_MAP", "True")

import pytest

from inferno import swebench_run as sr
from inferno.swebench_tasks import TASKS_FILE, repo_of, sample_tasks

# --------------------------------------------------------------------------- sample


def fake_dataset():
    rows = [{"instance_id": f"django__django-{i}", "repo": "django/django"} for i in range(200)]
    rows += [{"instance_id": f"sympy__sympy-{i}", "repo": "sympy/sympy"} for i in range(60)]
    rows += [{"instance_id": f"psf__requests-{i}", "repo": "psf/requests"} for i in range(8)]
    rows += [{"instance_id": f"pallets__flask-{i}", "repo": "pallets/flask"} for i in range(3)]
    return rows


def test_sample_is_deterministic_and_order_independent():
    rows = fake_dataset()
    first = sample_tasks(rows)
    shuffled = rows[:]
    random.Random(7).shuffle(shuffled)
    assert sample_tasks(shuffled) == first
    assert len(first) == 25 and len(set(first)) == 25
    assert sample_tasks(rows, seed=43) != first


def test_sample_caps_each_repo_at_8():
    counts = {}
    for iid in sample_tasks(fake_dataset()):
        repo = iid.split("__")[0]
        counts[repo] = counts.get(repo, 0) + 1
    assert max(counts.values()) <= 8


def test_sample_fills_up_when_the_cap_cannot_be_met():
    rows = [{"instance_id": f"django__django-{i}", "repo": "django/django"} for i in range(30)]
    rows += [{"instance_id": "pallets__flask-1", "repo": "pallets/flask"}]
    ids = sample_tasks(rows)
    assert len(ids) == 25 and "pallets__flask-1" in ids


def test_committed_sample_file():
    data = json.loads(TASKS_FILE.read_text())
    ids = data["instance_ids"]
    assert data["seed"] == 42 and len(ids) == len(set(ids)) == 25
    assert max(data["repo_counts"].values()) <= 8
    assert sum(data["repo_counts"].values()) == 25


def test_repo_of_falls_back_to_the_id():
    assert repo_of({"instance_id": "scikit-learn__scikit-learn-13142"}) == "scikit-learn/scikit-learn"


# --------------------------------------------------------------------------- fakes for the batch


def instance(n):
    return {"instance_id": f"psf__requests-{n}", "repo": "psf/requests", "problem_statement": "fix it"}


INSTANCES = [instance(n) for n in (1, 2, 3)]


def traj(steps, tokens_in=1000, tokens_out=100, cost=0.01, source="litellm"):
    msgs = [{"role": "system"}, {"role": "user"}]
    for _ in range(steps):
        msgs.append({"role": "assistant", "extra": {
            "response": {"usage": {"prompt_tokens": tokens_in, "completion_tokens": tokens_out}},
            "cost": cost, "cost_source": source}})
        msgs.append({"role": "tool"})
    return {"info": {"model_stats": {"api_calls": steps}}, "messages": msgs}


class FakeDocker:
    def __init__(self, fail_rmi=False):
        self.calls = []
        self.fail_rmi = fail_rmi

    def pull(self, image, timeout):
        self.calls.append(("pull", image))

    def remove_containers(self, label):
        self.calls.append(("rm", label))
        return 1

    def remove_image(self, image):
        self.calls.append(("rmi", image))
        if self.fail_rmi:
            raise RuntimeError("image is in use")


class FakeRunner:
    """`plan` maps instance_id -> TaskRun to return, or an exception to raise."""

    def __init__(self, plan):
        self.plan = plan
        self.ran = []
        self.labels = []

    def __call__(self, inst, model, limits, label, work_dir):
        self.ran.append(inst["instance_id"])
        self.labels.append(label)
        out = self.plan[inst["instance_id"]]
        if isinstance(out, BaseException):
            raise out
        return out


PLAN = {
    "psf__requests-1": sr.TaskRun("Submitted", "diff --git a/x b/x\n", traj(3), seconds=12.0),
    "psf__requests-2": sr.TaskRun("LimitsExceeded", "", traj(50)),
    "psf__requests-3": RuntimeError("model API down"),
}


def go(tmp_path, plan=PLAN, docker=None, instances=INSTANCES, **kw):
    runner = FakeRunner(plan)
    docker = docker or FakeDocker()
    outcome = sr.run_tasks(instances, "r1", "together_ai/openai/gpt-oss-120b", runner, docker,
                           results_dir=tmp_path, git_commit="abc", git_dirty=False, **kw)
    return outcome, runner, docker


def rows(tmp_path):
    return [json.loads(l) for l in (tmp_path / "r1.jsonl").read_text().splitlines()]


# --------------------------------------------------------------------------- batch behaviour


def test_one_line_per_task_with_all_fields(tmp_path):
    go(tmp_path)
    by = {r["instance_id"]: r for r in rows(tmp_path)}
    ok, lim, err = by["psf__requests-1"], by["psf__requests-2"], by["psf__requests-3"]

    assert (ok["outcome"], ok["exit_status"], ok["patch"]) == ("submitted", "Submitted", "diff --git a/x b/x\n")
    assert (ok["steps"], ok["tokens_in"], ok["tokens_out"]) == (3, 3000, 300)
    assert ok["cost_usd"] == pytest.approx(0.03) and ok["cost_source"] == "litellm"
    assert ok["seconds"] == 12.0 and ok["limit_hit"] is None

    assert (lim["outcome"], lim["limit_hit"], lim["steps"]) == ("limit", "steps", 50)
    assert (err["outcome"], err["exit_status"], err["patch"]) == ("error", "RuntimeError", "")
    assert "model API down" in err["error"] and err["steps"] == 0 and err["cost_usd"] == 0.0

    for r in by.values():
        assert r["git_commit"] == "abc" and r["git_dirty"] is False
        assert r["model"] == "together_ai/openai/gpt-oss-120b" and r["run_id"] == "r1"
        assert r["limits"] == {"cost_cap": 0.5, "step_limit": 50, "timeout_s": 1200}
        assert r["image"] == "docker.io/swebench/sweb.eval.x86_64." + r["instance_id"].replace("__", "_1776_")\
            + ":latest"


def test_image_removed_after_success_and_after_error(tmp_path):
    _, runner, docker = go(tmp_path)
    removed = [c for c in docker.calls if c[0] in ("rm", "rmi")]
    # Per task, in order: containers by label, then the image. Success and error alike.
    expected = []
    for inst, label in zip(INSTANCES, runner.labels):
        img = sr.image_name(inst)
        expected += [("rm", label), ("rmi", img)]
    assert removed == expected
    assert all(r["image_removed"] and r["containers_removed"] == 1 for r in rows(tmp_path))


def test_image_removed_on_hard_timeout(tmp_path):
    plan = {"psf__requests-1": sr.TaskRun("HardTimeout", "", traj(7), "worker killed")}
    _, _, docker = go(tmp_path, plan=plan, instances=INSTANCES[:1])
    assert [c[0] for c in docker.calls] == ["rm", "rmi"]
    r = rows(tmp_path)[0]
    assert (r["outcome"], r["limit_hit"], r["steps"]) == ("limit", "time", 7)


def test_ctrl_c_still_cleans_up_but_records_nothing(tmp_path):
    plan = {"psf__requests-1": KeyboardInterrupt()}
    with pytest.raises(KeyboardInterrupt):
        go(tmp_path, plan=plan, instances=INSTANCES[:1], docker=(d := FakeDocker()))
    assert [c[0] for c in d.calls] == ["rm", "rmi"]
    assert not (tmp_path / "r1.jsonl").exists()


def test_failed_rmi_is_recorded_and_reported(tmp_path):
    outcome, _, _ = go(tmp_path, docker=FakeDocker(fail_rmi=True), instances=INSTANCES[:1],
                       plan=PLAN)
    r = rows(tmp_path)[0]
    assert r["image_removed"] is False and "image is in use" in r["cleanup_error"]
    assert outcome.cleanup_failed == ["psf__requests-1"]


def test_resume_skips_finished_tasks(tmp_path):
    go(tmp_path, limit=1)
    assert [r["instance_id"] for r in rows(tmp_path)] == ["psf__requests-1"]

    outcome, runner, _ = go(tmp_path)
    assert runner.ran == ["psf__requests-2", "psf__requests-3"]
    assert outcome.skipped == ["psf__requests-1"]

    _, runner, docker = go(tmp_path)
    assert runner.ran == [] and docker.calls == []  # all done: nothing pulled or removed


def test_resume_ignores_a_half_written_line(tmp_path):
    go(tmp_path, instances=INSTANCES[:1])
    with (tmp_path / "r1.jsonl").open("a") as f:
        f.write('{"instance_id": "psf__requests-2", "pat')
    _, runner, _ = go(tmp_path)
    assert runner.ran == ["psf__requests-2", "psf__requests-3"]


def test_retry_errors_reruns_only_errors_and_latest_line_wins(tmp_path):
    go(tmp_path)
    fixed = dict(PLAN, **{"psf__requests-3": sr.TaskRun("Submitted", "diff3", traj(2))})
    _, runner, _ = go(tmp_path, plan=fixed, retry_errors=True)
    assert runner.ran == ["psf__requests-3"]
    preds = json.loads((tmp_path / "r1.preds.json").read_text())
    assert preds["psf__requests-3"]["model_patch"] == "diff3"


def test_preds_file_format(tmp_path):
    go(tmp_path)
    preds = json.loads((tmp_path / "r1.preds.json").read_text())
    assert set(preds) == {i["instance_id"] for i in INSTANCES}
    for iid, p in preds.items():
        assert set(p) == {"model_name_or_path", "instance_id", "model_patch"}
        assert p["instance_id"] == iid and p["model_name_or_path"] == "together_ai/openai/gpt-oss-120b"
        assert isinstance(p["model_patch"], str)
    assert preds["psf__requests-1"]["model_patch"] == "diff --git a/x b/x\n"
    assert preds["psf__requests-3"]["model_patch"] == ""


def test_preds_file_is_updated_after_every_task(tmp_path):
    seen = []

    class Watching(FakeRunner):
        def __call__(self, inst, *a):
            p = tmp_path / "r1.preds.json"
            seen.append(sorted(json.loads(p.read_text())) if p.exists() else [])
            return super().__call__(inst, *a)

    sr.run_tasks(INSTANCES, "r1", "m", Watching(PLAN), FakeDocker(), results_dir=tmp_path)
    assert seen == [[], ["psf__requests-1"], ["psf__requests-1", "psf__requests-2"]]


def test_summary(tmp_path):
    go(tmp_path)
    text = sr.summarize(sr.load_results(tmp_path / "r1.jsonl"))
    assert "Submitted:        1" in text and "Limit hit:        1" in text and "Error:            1" in text
    assert "Tokens in / out:  53000 / 5300" in text


# --------------------------------------------------------------------------- cost


class Resp:
    def __init__(self, tin, tout):
        self.usage = type("U", (), {"prompt_tokens": tin, "completion_tokens": tout})()


def raises(*a, **k):
    raise ValueError("model not mapped")


def test_cost_uses_litellm_when_it_knows_the_price():
    c = sr.cost_of_call("x/y", Resp(10, 10), litellm_cost=lambda r, model: 0.002)
    assert c == {"cost": 0.002, "cost_source": "litellm"}


def test_cost_falls_back_to_our_table():
    c = sr.cost_of_call("together_ai/openai/gpt-oss-120b", Resp(1_000_000, 1_000_000), litellm_cost=raises)
    assert c["cost_source"] == "table" and c["cost"] == pytest.approx(0.75)
    zero = sr.cost_of_call("together_ai/openai/gpt-oss-120b", Resp(1000, 0), litellm_cost=lambda r, model: 0.0)
    assert zero["cost_source"] == "table" and zero["cost"] > 0  # litellm's 0.0 isn't trusted


def test_kimi_k26_table_price_charges_cached_input_at_the_full_rate():
    kimi = "together_ai/moonshotai/Kimi-K2.6"
    assert sr.table_price(kimi) == (1.20, 4.50)
    c = sr.cost_of_call(kimi, Resp(1_000_000, 1_000_000), litellm_cost=raises)
    assert c["cost_source"] == "table" and c["cost"] == pytest.approx(1.20 + 4.50)

    # 900k of the 1M prompt tokens were cache hits: still priced at $1.20/1M, not $0.20.
    cached = Resp(1_000_000, 0)
    cached.usage.prompt_tokens_details = {"cached_tokens": 900_000}
    assert sr.cost_of_call(kimi, cached, litellm_cost=raises)["cost"] == pytest.approx(1.20)


def test_unknown_price_is_marked_unknown_not_zero(tmp_path):
    c = sr.cost_of_call("nobody/knows", Resp(10, 10), litellm_cost=raises)
    assert c["cost_source"] == "unknown"
    plan = {"psf__requests-1": sr.TaskRun("Submitted", "d", traj(2, source="unknown", cost=0.0))}
    go(tmp_path, plan=plan, instances=INSTANCES[:1])
    r = rows(tmp_path)[0]
    assert r["cost_usd"] is None and r["cost_source"] == "unknown"
    assert "(+1 unknown)" in sr.summarize([r])


def test_price_known():
    assert sr.table_price("together_ai/openai/gpt-oss-120b") == (0.15, 0.60)
    assert not sr.price_known("nobody/knows-this-model-xyz")


def test_priced_model_class_is_loadable_by_mini_swe_agent():
    from minisweagent.models import get_model_class
    from minisweagent.models.litellm_model import LitellmModel

    cls = get_model_class("m", "inferno.swebench_run.PricedLitellmModel")
    assert issubclass(cls, LitellmModel) and cls._calculate_cost is not LitellmModel._calculate_cost


# --------------------------------------------------------------------------- config + worker process


def test_build_config_merges_our_limits_into_swebench_yaml():
    cfg = sr.build_config("together_ai/openai/gpt-oss-120b", sr.Limits(0.25, 7, 60), "lab", "/t.json")
    assert cfg["agent"]["step_limit"] == 7 and cfg["agent"]["cost_limit"] == 0.25
    assert cfg["agent"]["wall_time_limit_seconds"] == 60
    assert "instance_template" in cfg["agent"]  # mini's own prompt is kept
    assert cfg["model"]["cost_tracking"] == "ignore_errors"
    assert cfg["model"]["model_name"] == "together_ai/openai/gpt-oss-120b"
    assert cfg["environment"]["cwd"] == "/testbed"
    assert cfg["environment"]["run_args"] == ["--rm", "--label", f"{sr.LABEL_KEY}=lab"]


def test_parser_refuses_unknown_price(capsys):
    with pytest.raises(SystemExit):
        sr.main(["--model", "nobody/knows-this-model-xyz", "--run-id", "x"])
    assert "--allow-unknown-cost" in capsys.readouterr().err


def write_fake_worker(tmp_path, body):
    script = tmp_path / "fake_worker.py"
    script.write_text("import json, sys, time\nspec = json.load(open(sys.argv[1]))\n" + body)
    return [sys.executable, str(script)]


def test_subprocess_runner_reads_worker_result_and_trajectory(tmp_path):
    cmd = write_fake_worker(tmp_path, (
        "json.dump(" + json.dumps(traj(2)) + ", open(spec['traj_path'], 'w'))\n"
        "json.dump({'exit_status': 'Submitted', 'submission': 'PATCH'}, open(spec['result_path'], 'w'))\n"
    ))
    docker = FakeDocker()
    run = sr.SubprocessTaskRunner(docker, worker_cmd=cmd)(instance(1), "m", sr.Limits(), "lab", tmp_path / "w")
    assert (run.exit_status, run.patch) == ("Submitted", "PATCH")
    assert sr.trajectory_metrics(run.trajectory)["steps"] == 2
    assert docker.calls == [("pull", sr.image_name(instance(1)))]


def test_subprocess_runner_kills_worker_at_the_hard_timeout(tmp_path):
    cmd = write_fake_worker(tmp_path, (
        "json.dump(" + json.dumps(traj(4)) + ", open(spec['traj_path'], 'w'))\n"
        "time.sleep(60)\n"
    ))
    t0 = time.monotonic()
    run = sr.SubprocessTaskRunner(FakeDocker(), kill_grace=0, worker_cmd=cmd)(
        instance(1), "m", sr.Limits(timeout_s=1), "lab", tmp_path / "w")
    assert time.monotonic() - t0 < 10
    assert run.exit_status == "HardTimeout"
    assert sr.trajectory_metrics(run.trajectory)["steps"] == 4  # saved progress survives the kill


def test_subprocess_runner_worker_crash(tmp_path):
    cmd = write_fake_worker(tmp_path, "sys.exit(3)\n")
    run = sr.SubprocessTaskRunner(FakeDocker(), worker_cmd=cmd)(instance(1), "m", sr.Limits(), "l", tmp_path)
    assert run.exit_status == "WorkerCrashed" and "exited 3" in run.error
    assert sr.classify(run.exit_status) == "error"


def test_trajectory_metrics_handles_missing_usage():
    t = traj(1)
    t["messages"][2]["extra"]["response"] = {}
    m = sr.trajectory_metrics(t)
    assert m["tokens_complete"] is False and m["tokens_in"] == 0
