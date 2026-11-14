"""Tests for the SWE-bench runner. Fakes only: no Docker, no model calls, no dataset download."""

import json
import os
import random
import subprocess
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
            "cost": cost, "price_source": source}})
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
    "psf__requests-1": sr.TaskRun("Submitted", "diff --git a/x b/x\n", traj(3), seconds=12.0,
                                  litellm={"litellm_version": "9.9.9", "price_map_source": "local",
                                           "price_map_revision": "abc123"}),
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
    assert (ok["steps"], ok["tokens_in"], ok["tokens_out"], ok["tokens_cached"]) == (3, 3000, 300, 0)
    assert ok["cost_usd"] == pytest.approx(0.03) and ok["price_source"] == "litellm"
    assert ok["seconds"] == 12.0 and ok["limit_hit"] is None

    assert (lim["outcome"], lim["limit_hit"], lim["steps"]) == ("limit", "steps", 50)
    assert (err["outcome"], err["exit_status"], err["patch"]) == ("error", "RuntimeError", "")
    assert "model API down" in err["error"] and err["steps"] == 0 and err["cost_usd"] == 0.0

    for r in by.values():
        assert r["git_commit"] == "abc" and r["git_dirty"] is False
        assert r["model"] == "together_ai/openai/gpt-oss-120b" and r["run_id"] == "r1"
        assert r["limits"] == {"cost_cap": 1.5, "step_limit": 50, "timeout_s": 1200}
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
    assert "Tokens cached:    0 (0% of input)" in text


def traj_with_cached(per_call):
    """A trajectory whose calls report these cached_tokens values (None: field absent)."""
    t = traj(len(per_call))
    calls = [m for m in t["messages"] if "response" in (m.get("extra") or {})]
    for msg, c in zip(calls, per_call):
        if c is not None:
            msg["extra"]["response"]["usage"]["prompt_tokens_details"] = {"cached_tokens": c}
    return t


def test_tokens_cached_sums_reported_cache_hits_across_calls(tmp_path):
    plan = {
        "psf__requests-1": sr.TaskRun("Submitted", "d", traj_with_cached([0, 600, None, 300])),
        "psf__requests-2": sr.TaskRun("LimitsExceeded", "", traj_with_cached([None, None])),
        "psf__requests-3": RuntimeError("no calls made"),
    }
    go(tmp_path, plan=plan)
    by = {r["instance_id"]: r["tokens_cached"] for r in rows(tmp_path)}
    assert by == {"psf__requests-1": 900, "psf__requests-2": 0, "psf__requests-3": 0}

    text = sr.summarize(sr.load_results(tmp_path / "r1.jsonl"))
    assert "Tokens cached:    900 (15% of input)" in text  # 900 of 6 calls x 1000 prompt tokens


def test_tokens_cached_uses_the_same_rules_as_pricing():
    # Bad values count as 0, exactly as in _cached_tokens().
    t = traj_with_cached([250, "lots", -5, True])
    assert sr.trajectory_metrics(t)["tokens_cached"] == 250
    assert sr.trajectory_metrics(None)["tokens_cached"] == 0


def test_summary_notes_lines_written_before_tokens_cached_existed():
    base = dict(outcome="submitted", patch="", tokens_in=100, tokens_out=1, cost_usd=0.0)
    rows_ = [dict(base, instance_id="a", tokens_cached=40), dict(base, instance_id="b")]
    assert "Tokens cached:    40 (20% of input) (+1 task(s) not recorded)" in sr.summarize(rows_)


# --------------------------------------------------------------------------- cost


class Resp:
    def __init__(self, tin, tout):
        self.usage = type("U", (), {"prompt_tokens": tin, "completion_tokens": tout})()


def raises(*a, **k):
    raise ValueError("model not mapped")


def test_cost_uses_litellm_when_it_knows_the_price():
    c = sr.cost_of_call("x/y", Resp(10, 10), litellm_cost=lambda r, model: 0.002)
    assert c == {"cost": 0.002, "price_source": "litellm"}


def test_cost_falls_back_to_our_table():
    c = sr.cost_of_call("together_ai/openai/gpt-oss-120b", Resp(1_000_000, 1_000_000), litellm_cost=raises)
    assert c["price_source"] == "table" and c["cost"] == pytest.approx(0.75)
    zero = sr.cost_of_call("together_ai/openai/gpt-oss-120b", Resp(1000, 0), litellm_cost=lambda r, model: 0.0)
    assert zero["price_source"] == "table" and zero["cost"] > 0  # litellm's 0.0 isn't trusted


def test_kimi_k26_table_price_charges_cached_input_at_the_full_rate():
    kimi = "together_ai/moonshotai/Kimi-K2.6"
    assert sr.table_price(kimi) == sr.Price(1.20, 4.50, cached_input=None)
    c = sr.cost_of_call(kimi, Resp(1_000_000, 1_000_000), litellm_cost=raises)
    assert c["price_source"] == "table" and c["cost"] == pytest.approx(1.20 + 4.50)

    # 900k of the 1M prompt tokens were cache hits: still priced at $1.20/1M, not $0.20.
    cached = Resp(1_000_000, 0)
    cached.usage.prompt_tokens_details = {"cached_tokens": 900_000}
    assert sr.cost_of_call(kimi, cached, litellm_cost=raises)["cost"] == pytest.approx(1.20)


KIMI_K31 = "together_ai/moonshotai/Kimi-K3.1-Pro"


def with_cached(resp, cached):
    resp.usage.prompt_tokens_details = {"cached_tokens": cached}
    return resp


def test_kimi_k31_pro_is_in_the_table_with_a_cached_rate():
    assert sr.table_price(KIMI_K31) == sr.Price(2.00, 10.00, cached_input=0.20)
    assert sr.price_known(KIMI_K31)


def test_kimi_k31_pro_without_cache_hits_charges_full_input():
    c = sr.cost_of_call(KIMI_K31, Resp(1_000_000, 1_000_000), litellm_cost=raises)
    assert c == {"cost": pytest.approx(2.00 + 10.00), "price_source": "table"}


def test_kimi_k31_pro_charges_cached_tokens_at_the_cached_rate():
    # 1M prompt tokens of which 900k cached: 100k x $2.00 + 900k x $0.20, plus 1M output x $10.
    c = sr.cost_of_call(KIMI_K31, with_cached(Resp(1_000_000, 1_000_000), 900_000), litellm_cost=raises)
    assert c["cost"] == pytest.approx(0.20 + 0.18 + 10.00) and c["price_source"] == "table"


def test_cached_tokens_read_from_a_dict_response_like_the_trajectory():
    resp = {"usage": {"prompt_tokens": 1_000_000, "completion_tokens": 0,
                      "prompt_tokens_details": {"cached_tokens": 500_000}}}
    assert sr.cost_of_call(KIMI_K31, resp, litellm_cost=raises)["cost"] == pytest.approx(1.00 + 0.10)


@pytest.mark.parametrize("details", [None, {}, {"cached_tokens": None}, {"cached_tokens": "lots"},
                                     {"cached_tokens": -5}, {"cached_tokens": True}])
def test_missing_or_bad_cached_counts_mean_full_price(details):
    resp = Resp(1_000_000, 0)
    resp.usage.prompt_tokens_details = details
    assert sr.cost_of_call(KIMI_K31, resp, litellm_cost=raises)["cost"] == pytest.approx(2.00)


def test_cached_count_above_prompt_tokens_is_clamped():
    c = sr.cost_of_call(KIMI_K31, with_cached(Resp(1_000, 0), 5_000), litellm_cost=raises)
    assert c["cost"] == pytest.approx(1_000 * 0.20 / 1e6)  # never negative full-rate tokens


def test_litellm_still_wins_over_the_cached_table_rate():
    c = sr.cost_of_call(KIMI_K31, with_cached(Resp(1_000_000, 0), 900_000), litellm_cost=lambda r, model: 0.42)
    assert c == {"cost": 0.42, "price_source": "litellm"}


def test_inferno_client_prices_still_work_as_fallback(monkeypatch):
    monkeypatch.setitem(sr.inferno_client.PRICES_PER_MILLION, "acme/model-x", (1.0, 2.0))
    assert sr.table_price("together_ai/acme/model-x") == sr.Price(1.0, 2.0, None)


def test_default_cost_cap_is_150():
    assert sr.DEFAULT_COST_CAP == 1.50 and sr.Limits().cost_cap == 1.50


def test_unknown_price_is_marked_unknown_not_zero(tmp_path):
    c = sr.cost_of_call("nobody/knows", Resp(10, 10), litellm_cost=raises)
    assert c["price_source"] == "unknown"
    plan = {"psf__requests-1": sr.TaskRun("Submitted", "d", traj(2, source="unknown", cost=0.0))}
    go(tmp_path, plan=plan, instances=INSTANCES[:1])
    r = rows(tmp_path)[0]
    assert r["cost_usd"] is None and r["price_source"] == "unknown"
    assert "(+1 unknown)" in sr.summarize([r])


def test_price_known():
    assert sr.table_price("together_ai/openai/gpt-oss-120b") == sr.Price(0.15, 0.60)
    assert not sr.price_known("nobody/knows-this-model-xyz")


def test_priced_model_class_extends_the_textbased_model():
    from minisweagent.models import get_model_class
    from minisweagent.models.litellm_model import LitellmModel
    from minisweagent.models.litellm_textbased_model import LitellmTextbasedModel

    cls = get_model_class("m", sr.MODEL_CLASS_PATH)
    assert issubclass(cls, LitellmTextbasedModel)
    assert cls._calculate_cost is not LitellmModel._calculate_cost
    assert get_model_class("m", sr.MODEL_CLASS) is LitellmTextbasedModel  # the name we record


class TextResp:
    """Just enough of a litellm response for _parse_actions and cost_of_call."""

    def __init__(self, content, tin=100, tout=10):
        msg = type("M", (), {"content": content, "tool_calls": None})()
        self.choices = [type("C", (), {"message": msg, "finish_reason": "stop"})()]
        self.usage = type("U", (), {"prompt_tokens": tin, "completion_tokens": tout})()


def test_built_model_parses_one_backtick_block_and_prices_with_the_table(monkeypatch):
    from minisweagent.exceptions import FormatError
    from minisweagent.models import get_model

    cfg = sr.build_config("together_ai/openai/gpt-oss-120b", sr.Limits(), "lab")
    model = get_model(config=cfg["model"])  # no network: the constructor makes no call
    assert type(model).__name__ == "PricedTextbasedModel"

    ok = TextResp("THOUGHT: look\n\n```mswea_bash_command\nls -la\n```")
    assert model._parse_actions(ok) == [{"command": "ls -la"}]
    with pytest.raises(FormatError):  # two commands in one reply is rejected, as mini intends
        model._parse_actions(TextResp("```mswea_bash_command\nls\n```\n```mswea_bash_command\npwd\n```"))

    import litellm
    monkeypatch.setattr(litellm.cost_calculator, "completion_cost", raises)
    assert model._calculate_cost(TextResp("x", 1_000_000, 1_000_000)) == \
        {"cost": pytest.approx(0.75), "price_source": "table"}


# --------------------------------------------------------------------------- config + worker process


def test_build_config_uses_backticks_config_with_our_limits():
    from minisweagent.config import builtin_config_dir, get_config_from_spec

    cfg = sr.build_config("together_ai/openai/gpt-oss-120b", sr.Limits(0.25, 7, 60), "lab", "/t.json")
    mini = get_config_from_spec(builtin_config_dir / "benchmarks" / "swebench_backticks.yaml")
    assert sr.AGENT_CONFIG == "swebench_backticks" and sr.MODEL_CLASS == "litellm_textbased"
    assert cfg["agent"]["instance_template"].startswith(mini["agent"]["instance_template"].rstrip())
    assert "mswea_bash_command" in cfg["agent"]["system_template"]
    assert cfg["model"]["format_error_template"].startswith(mini["model"]["format_error_template"].rstrip())
    assert cfg["model"]["model_class"] == sr.MODEL_CLASS_PATH
    assert "parallel_tool_calls" not in cfg["model"].get("model_kwargs", {})
    assert cfg["agent"]["step_limit"] == 7 and cfg["agent"]["cost_limit"] == 0.25
    assert cfg["agent"]["wall_time_limit_seconds"] == 60
    assert cfg["model"]["cost_tracking"] == "ignore_errors"
    assert cfg["model"]["model_name"] == "together_ai/openai/gpt-oss-120b"
    assert cfg["environment"]["cwd"] == "/testbed"
    assert cfg["environment"]["run_args"] == ["--rm", "--label", f"{sr.LABEL_KEY}=lab"]
    # mini's own env vars kept, plus BASH_ENV so commands run in the testbed conda env.
    assert cfg["environment"]["env"]["PAGER"] == "cat"
    assert cfg["environment"]["env"]["BASH_ENV"] == "/root/.bashrc"


def test_result_lines_record_the_harness(tmp_path):
    go(tmp_path)
    for r in rows(tmp_path):
        assert r["harness"] == "backticks-v2"
        assert (r["agent_config"], r["model_class"]) == ("swebench_backticks", "litellm_textbased")


@pytest.mark.parametrize("old", [
    {},  # tool-calling run: no harness fields at all
    {"agent_config": "swebench_backticks", "model_class": "litellm_textbased"},  # backticks-v1
    {"harness": "backticks-v1"},
])
def test_refuses_to_mix_harnesses_in_one_run_file(tmp_path, monkeypatch, capsys, old):
    monkeypatch.setattr(sr, "RESULTS_DIR", tmp_path)
    line = {"instance_id": "a", "model": "together_ai/openai/gpt-oss-120b", "outcome": "error", **old}
    (tmp_path / "old.jsonl").write_text(json.dumps(line) + "\n")
    with pytest.raises(SystemExit):
        sr.main(["--model", "together_ai/openai/gpt-oss-120b", "--run-id", "old"])
    assert "another harness" in capsys.readouterr().err


def test_harness_of_old_lines():
    assert sr.harness_of({}) == "toolcall-v1"
    assert sr.harness_of({"agent_config": "swebench_backticks"}) == "backticks-v1"
    assert sr.harness_of({"harness": "backticks-v2"}) == "backticks-v2"


def mini_backticks_config():
    from minisweagent.config import builtin_config_dir, get_config_from_spec

    return get_config_from_spec(builtin_config_dir / "benchmarks" / "swebench_backticks.yaml")


def test_command_format_rules_are_appended_after_minis_text():
    mini = mini_backticks_config()
    cfg = sr.build_config("m", sr.Limits(), "lab")
    for got, orig in [(cfg["agent"]["instance_template"], mini["agent"]["instance_template"]),
                      (cfg["model"]["format_error_template"], mini["model"]["format_error_template"])]:
        assert got.startswith(orig.rstrip("\n"))  # mini's original text first, unchanged
        assert got.endswith(sr.COMMAND_FORMAT_RULES)
    rules = sr.COMMAND_FORMAT_RULES
    for must in ["real line breaks", "\\n", '\\"', "JSON", "closing ``` on its own line",
                 "no apply_patch", "sed -i", "heredoc"]:
        assert must in rules, must
    assert cfg["agent"]["max_consecutive_format_errors"] == 5


def test_rules_are_plain_text_for_jinja_and_their_examples_parse():
    from jinja2 import StrictUndefined, Template

    rules = sr.COMMAND_FORMAT_RULES
    assert "{{" not in rules and "{%" not in rules and "{#" not in rules
    assert Template(rules, undefined=StrictUndefined).render().rstrip() == rules.rstrip()
    # Each example block is one valid action under mini's own regex.
    from minisweagent.models.litellm_textbased_model import LitellmTextbasedModelConfig
    import re
    blocks = re.findall(LitellmTextbasedModelConfig.model_fields["action_regex"].default, rules, re.DOTALL)
    assert [b.split("\n")[0] for b in blocks] == ["sed -i 's/old text/new text/' path/to/file.py",
                                                  "python - <<'EOF'"]


def test_format_error_message_shows_minis_text_then_the_rules():
    from minisweagent.exceptions import FormatError
    from minisweagent.models import get_model

    model = get_model(config=sr.build_config("m", sr.Limits(), "lab")["model"])
    escaped = 'THOUGHT: x\n\n```mswea_bash_command\napply_patch <<\'PATCH\'\\n*** Begin Patch\\nPATCH\\n"]}'
    with pytest.raises(FormatError) as e:
        model._parse_actions(TextResp(escaped))
    text = e.value.messages[0]["content"]
    assert text.index("Expected exactly 1 action, found 0.") < text.index("## Command format (strict)")
    assert "Please always provide EXACTLY ONE action in triple backticks, found 0 actions." in text


def test_instance_template_renders_in_a_real_agent():
    """DefaultAgent renders the template with StrictUndefined; our addition must not break that."""
    from minisweagent.agents.default import DefaultAgent
    from minisweagent.models import get_model

    cfg = sr.build_config("m", sr.Limits(), "lab")

    class FakeEnv:
        def get_template_vars(self, **kw):
            return {"cwd": "/testbed"}

    agent = DefaultAgent(get_model(config=cfg["model"]), FakeEnv(), **cfg["agent"])
    agent.extra_template_vars["task"] = "the issue"
    text = agent._render_template(agent.config.instance_template)
    assert "the issue" in text and text.rstrip().endswith("EOF\n```")
    assert agent.config.max_consecutive_format_errors == 5


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


# --------------------------------------------------------------------------- reproducible litellm prices


def test_result_lines_record_litellm_version_and_price_sources(tmp_path):
    sr.run_tasks(INSTANCES, "r1", "m", FakeRunner(PLAN), FakeDocker(), results_dir=tmp_path,
                 litellm_version="1.0-parent")
    by = {r["instance_id"]: r for r in rows(tmp_path)}
    ok, err = by["psf__requests-1"], by["psf__requests-3"]
    assert (ok["litellm_version"], ok["price_map_source"], ok["price_map_revision"]) == \
        ("9.9.9", "local", "abc123")  # what the worker that priced the calls saw
    assert ok["price_source"] == "litellm"
    # The worker never reported (runner raised): fall back to the parent's version, source unknown.
    assert (err["litellm_version"], err["price_map_source"]) == ("1.0-parent", None)
    assert err["price_source"] == "no_calls"

    mixed = {"psf__requests-1": sr.TaskRun("Submitted", "d", {"messages": [
        {"extra": {"response": {"usage": {"prompt_tokens": 1, "completion_tokens": 1}},
                   "cost": 0.1, "price_source": s}} for s in ("litellm", "table")]})}
    sr.run_tasks(INSTANCES[:1], "r2", "m", FakeRunner(mixed), FakeDocker(), results_dir=tmp_path)
    assert json.loads((tmp_path / "r2.jsonl").read_text())["price_source"] == "litellm+table"


def test_importing_the_runner_makes_litellm_use_its_bundled_price_map():
    """Fresh interpreter with the variable unset and a dead download URL: prices must come from
    the copy bundled with the installed litellm, so nothing is fetched."""
    from importlib.metadata import version

    env = {k: v for k, v in os.environ.items() if k != "LITELLM_LOCAL_MODEL_COST_MAP"}
    env["LITELLM_MODEL_COST_MAP_URL"] = "http://127.0.0.1:9/never.json"
    code = ("import json, inferno.swebench_run as s, os; "
            "print(json.dumps([s.litellm_info(), os.environ['LITELLM_LOCAL_MODEL_COST_MAP']]))")
    out = subprocess.run([sys.executable, "-c", code], env=env, capture_output=True, text=True, timeout=120)
    assert out.returncode == 0, out.stderr[-2000:]
    info, flag = json.loads(out.stdout.strip().splitlines()[-1])
    assert flag == "True"
    assert info["price_map_source"] == "local" and info["litellm_version"] == version("litellm")


def test_worker_refuses_a_downloaded_price_map(tmp_path, monkeypatch):
    monkeypatch.setattr(sr, "_load_api_keys", lambda: None)
    monkeypatch.setattr(sr, "litellm_info", lambda: {"litellm_version": "x", "price_map_source": "remote",
                                                     "price_map_revision": None})
    called = []
    import minisweagent.run.benchmarks.swebench as mini_sb
    monkeypatch.setattr(mini_sb, "get_sb_environment", lambda *a: called.append(a))
    spec = {"instance": instance(1), "model": "m", "limits": vars(sr.Limits()), "label": "l",
            "traj_path": str(tmp_path / "t.json"), "result_path": str(tmp_path / "r.json")}
    (tmp_path / "s.json").write_text(json.dumps(spec))
    sr.worker(tmp_path / "s.json")
    result = json.loads((tmp_path / "r.json").read_text())
    assert result["exit_status"] == "RuntimeError" and "bundled" in result["error"]
    assert result["litellm"]["price_map_source"] == "remote"
    assert called == []  # no container was started


def test_hard_timeout_keeps_the_litellm_info_the_worker_wrote_early(tmp_path):
    cmd = write_fake_worker(tmp_path, (
        "json.dump({'litellm': {'litellm_version': '1.2.3', 'price_map_source': 'local'}},"
        " open(spec['result_path'], 'w'))\n"
        "time.sleep(60)\n"
    ))
    run = sr.SubprocessTaskRunner(FakeDocker(), kill_grace=0, worker_cmd=cmd)(
        instance(1), "m", sr.Limits(timeout_s=1), "lab", tmp_path / "w")
    assert run.exit_status == "HardTimeout"
    assert run.litellm == {"litellm_version": "1.2.3", "price_map_source": "local"}
