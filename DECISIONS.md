# Decisions

One entry per key decision: the context, the choice, why, and the trade-off. The numbers come from the committed files in `results/`.

## 1. Run model-written code in a Docker sandbox with no network

- **Context:** The model's candidate programs are untrusted. They can loop forever, use up memory, or read files. Some buggy QuixBugs programs already loop forever.
- **Choice:** Every test run is a fresh `docker run --rm` with these settings:
  - `--network none`, 512 MB memory and CPU/PID limits;
  - a read-only root filesystem, `--cap-drop ALL` and a non-root user;
  - a 5 s per-test timeout and a 120 s hard limit;
  - only the candidate and its test files mounted, read-only. Any mount that overlaps `~/inferno-private` (the reference solutions) is refused.
- **Why:** The host and the answers stay out of reach regardless of what the model writes, and a hang costs at most 120 s.
- **Trade-off:** Each test run pays for container startup, and Docker is required (on WSL, through Docker Desktop). Programs that need the network or write files can't be tested, though none of QuixBugs' do.

## 2. Resumable JSONL runners that record the git commit

- **Context:** Runs make many paid API calls and take minutes to hours (QuixBugs, validation, SWE-bench). Crashes, API errors and a sleeping laptop all happen.
- **Choice:** Each finished item is appended as one JSON line to `results/.../<run_id>.jsonl`, and re-running the same run id skips finished items.
  - A truncated last line is skipped.
  - In SWE-bench runs, errors are written with `outcome: error`. `--retry-errors` appends a newer line, and the latest line per task wins.
  - Every line records `git_commit` and `git_dirty`, and the runner warns about a dirty tree.
- **Why:** No work is lost or paid for twice. Each result can be traced to the exact code that produced it, and the committed files are the source for every reported number.
- **Trade-off:** There is no database, so readers must apply "latest line wins" (strong-v1.jsonl has 33 lines for 25 tasks). Recording a commit only helps if you commit before running.

## 3. Validate fixes with Hypothesis properties

- **Context:** QuixBugs has only a few test cases per program, so a fix can pass them and still be wrong.
- **Choice:** Hand-written properties for 10 programs, each a general statement about the output checked on about 200 generated inputs, plus a re-run of the original tests. `scripts/check_properties.py` proves each property passes on the reference program and fails on the buggy one.
- **Why:** It is an independent check that doesn't depend on the benchmark's tests, and the self-check ensures the properties really separate correct from buggy.
- **Trade-off:**
  - Writing properties is manual, so only 10 of 40 programs have them.
  - They caught 0 wrong fixes in both runs, so their ability to catch one isn't shown by our data.
  - They run on the host only for the self-check, which reads the private references.

## 4. AST renaming with an alias for the memorization check

- **Context:** QuixBugs is public, and a model may recall the known fix from names like `bitcount`.
- **Choice:** `rename.py` uses `ast` with scope tracking to rename functions, parameters and variables to neutral names (`f_1`, `a_1`, `v_1`), and drops comments and docstrings.
  - Tests still import the original name, so a hidden alias line (`bitcount = f_1; bitcount.__name__ = ...`) is appended only at test time.
  - Test output is scrubbed of the original names.
- **Why:** The renaming is correct by construction (no regex renaming of strings or attributes), and the tests run unchanged. The model never sees the original names.
- **Trade-off:** The algorithm's structure stays recognisable, so this only partly tests memorization. Dropping docstrings also removes information the original prompt had. Result: 34/40 first try renamed, against 38/40 original.

## 5. mini-swe-agent rather than SWE-agent

- **Context:** We needed an agent scaffold for SWE-bench Verified that runs on a laptop and is small enough to read.
- **Choice:** mini-swe-agent 2.4.6, used as a library (`DefaultAgent`, `DockerEnvironment`, its SWE-bench configs) inside our own runner.
- **Why:**
  - It has a bash-only loop with no custom tool server.
  - It uses the prebuilt `swebench/sweb.eval.*` images.
  - It reaches Together through litellm.
  - Its code is short enough to verify behaviour from the source, which we did for the format-error path, configs and cleanup.
  - Our runner adds per-task limits, a hard timeout in a worker process, and container and image cleanup.
- **Trade-off:** There are no specialised edit or search tools, so models edit with `sed` or Python heredocs, which is where many format and editing errors came from. A richer scaffold might score higher.

## 6. The plain-text harness after the tool-call failures

- **Context:** Under mini's default tool-calling `swebench.yaml`, gpt-oss on Together returned dozens of parallel tool calls per reply, some with invalid JSON, so every step was a format error. The first plain-text run then hit repeated format errors from escaped newlines and missing fences.
- **Choice:** mini's plain-text `swebench_backticks` config with the `litellm_textbased` model class (one fenced command per reply). We then added `backticks-v2`:
  - strict command-format rules appended to the instance and format-error prompts;
  - 5 consecutive format errors allowed instead of 3;
  - the harness name recorded on every line, and a run file can't mix harnesses.
- **Why:** gpt-oss could finish tasks with it (12 of its 25 runs submitted), and both models used the same prompts.
- **Trade-off:**
  - Format errors still used 259 of 650 steps (40%) in gpt-oss's 13 step-limit runs.
  - The prompt overrides mean the scores belong to this harness, not stock mini-swe-agent.
  - Kimi was never tried with tool calling.

## 7. litellm's bundled price map

- **Context:** litellm downloads its latest price map at import by default, so the cost of the same run could change from day to day. Some models aren't in the map at all.
- **Choice:**
  - Set `LITELLM_LOCAL_MODEL_COST_MAP=True` so the map bundled with the pinned litellm (1.109.1, in `uv.lock`) is used; the worker refuses to run with a non-local map.
  - Each line records the litellm version, the map's source and revision, and the `price_source`.
  - When litellm doesn't know a model, our own table prices it, including cached-input rates.
  - A model with no known price is refused unless `--allow-unknown-cost`.
- **Why:** Costs are reproducible and every number says where its price came from.
- **Trade-off:** The bundled map can be stale. Kimi-K3.1-Pro isn't in it, so all 25 Kimi tasks are priced from our table ($2.00 / $10.00 per 1M, $0.20 cached, from Together's listing on 2026-11-15). Kimi's cost is only as accurate as that table.

## 8. The cheap and strong models

- **Context:** The cascade needs a cheap model and a strong one with a large price gap. One API key and provider keeps setup simple.
- **Choice:** Both on Together:
  - **cheap:** gpt-oss-120b, $0.15 / $0.60 per 1M tokens, also used for QuixBugs;
  - **strong:** Kimi-K3.1-Pro, $2.00 / $10.00 per 1M, $0.20 cached input.
- **Why:** Both are open-weight models on the same provider and harness, with a list-price gap of about 13x on input and 17x on output. gpt-oss had already fixed 38/40 QuixBugs programs on the first try.
- **Trade-off:** The list-price gap overstated the real one. With cached input, Kimi averaged $0.15 per task against gpt-oss's $0.11, because gpt-oss used all 50 steps on 13 tasks. That is a large part of why the cascade lost.

## 9. Local grading with swebench 4.1.0, not Modal

- **Context:**
  - swebench 5.0.2's Modal path crashes: its Modal code reads `TestSpec.setup_env_script`, which the 5.x `TestSpec` no longer has. It is the same in 5.0.0 and 5.0.1.
  - 5.0.2's local path pulls every task image and never removes one. That is about 4 GB per image, on a laptop with about 60 GB free.
- **Choice:** swebench 4.1.0, the latest release with both a working Modal path and `--namespace` / `--cache_level` / `--clean`, run locally in its own venv. It uses `--namespace swebench --cache_level env --clean false --max_workers 1`, with one run per model and a gold-patch sanity check (1/1 resolved).
- **Why:**
  - It uses the same prebuilt images as the agent runs.
  - It removes each image right after grading it, so peak disk is about one image.
  - It is free, and doesn't depend on 4.1.0's Modal image installing an unpinned `swebench`, which would now get 5.x.
- **Trade-off:** It is slower: every image is pulled again for each model. 4.1.0 builds its test scripts from its own specs, so its scores may differ slightly from 5.x's. We pinned an older grader.

## 10. Escalate on run-time signals only

- **Context:** A cascade can only use what is known when the cheap run finishes. Grading results aren't available in real use, and using them would leak the answer into the simulation.
- **Choice:**
  - `escalation_reason(cheap_record)` takes only the cheap run's record. It escalates on a limit hit, an error or an empty patch, and keeps the cheap patch otherwise.
  - Grading only scores the result afterwards.
  - Tests check that any grading outcome gives the same decisions.
  - An "oracle" row that does use grading is reported separately, labelled as an upper bound.
- **Why:** The cascade's numbers are what a real deployment would get.
- **Trade-off:** These signals can't tell a wrong patch from a right one. 6 of the 11 kept gpt-oss patches were wrong, and Kimi would have fixed 4 of them, so the cascade resolved 18/25 against Kimi's 22/25. A run-time test signal would be the next thing to add.
