# SWE-bench failure analysis

The failures of the two graded runs ([summary](summary.md)):

- `strong-v1` (Kimi-K3.1-Pro): its 3 unresolved tasks.
- `cheap-v1` (gpt-oss-120b): its 6 submitted-but-unresolved patches, plus one of its 13 step-limit runs as an example.

The evidence comes from:

- the trajectories in `results/swebench/<run>/`;
- the swebench 4.1.0 grading logs (`report.json` and `test_output.txt` per task, in `~/swebench-eval/logs/run_evaluation/grade-<run>/`);
- the gold patches from `princeton-nlp/SWE-Bench_Verified`.

Each task gets one cause:

- **wrong file:** the patch edited code that isn't where the bug is.
- **wrong fix:** right place, but the target (FAIL_TO_PASS) tests still fail.
- **broke other tests:** the target tests pass, but PASS_TO_PASS tests now fail.
- **gave up / ran out:** hit the step limit or never converged.

## Failures

| Model | Task | Cause | Evidence | Patch location vs gold |
|---|---|---|---|---|
| Kimi | django-15916 | wrong fix | FAIL_TO_PASS `FormFieldCallbackTests.test_custom_callback_in_meta` fails (`TextInput != Textarea`); 149/149 PASS_TO_PASS pass | Same function as gold (`modelform_factory`, `django/forms/models.py`). The model only stops passing a `None` callback. Gold also reads `Meta.formfield_callback` in `ModelFormOptions` and the metaclass, so a callback on `Meta` is still ignored. |
| Kimi | xarray-6938 | wrong fix | FAIL_TO_PASS `test_variable.py::TestIndexVariable::test_to_index_variable_copy` fails (`assert a is not b`) | Model adds a `.copy()` in `Dataset.swap_dims` (`xarray/core/dataset.py`). Gold fixes `to_index_variable` itself in `xarray/core/variable.py` (and only adds a type hint in `swap_dims`). The model fixed the caller from the issue; the test checks the helper. |
| Kimi | sympy-20916 | wrong fix | FAIL_TO_PASS `test_super_sub` fails: `split_super_sub("w𝟙") == ("w", [], ["𝟙"])` | Same line as gold (`_name_with_digits_p`, `sympy/printing/conventions.py`). Model wrote `([0-9]+)` for the digits; gold uses `(\d+)`, which also matches Unicode digits like 𝟙. |
| gpt-oss | django-12193 | wrong file | FAIL_TO_PASS `CheckboxInputTest.test_get_context_does_not_mutate_attrs` fails | Model passes a copy of `attrs` from `SplitArrayWidget` (`django/contrib/postgres/forms/array.py`), where the bug shows up. Gold stops `CheckboxInput.get_context` mutating `attrs` (`django/forms/widgets.py`). |
| gpt-oss | django-14493 | broke other tests | FAIL_TO_PASS passes (1/1); 30 of 36 PASS_TO_PASS in `staticfiles_tests.test_storage` fail with `RuntimeError: Max post-process passes exceeded.` | Same spot as gold (`HashedFilesMixin.post_process`, `django/contrib/staticfiles/storage.py`). Gold adds `substitutions = False` before the loop. The model moved it out of the loop, so it is never reset between passes. |
| gpt-oss | django-14672 | wrong fix | 1 of 168 FAIL_TO_PASS fails: `test_reverse_inherited_m2m_with_through_fields_list_hashable` (`('event', 'invitee') != ['event', 'invitee']`) | Same file and class as gold (`ManyToManyRel`, `django/db/models/fields/reverse_related.py`). The model converts `through_fields` to a tuple in `__init__`, which changes the stored value. Gold applies `make_hashable` only inside the `identity` property. |
| gpt-oss | django-15916 | wrong fix | Same failing test as Kimi: `test_custom_callback_in_meta` | Same change and same function as Kimi's patch. Gold's `Meta`/metaclass part is missing. |
| gpt-oss | matplotlib-25332 | wrong file | FAIL_TO_PASS `test_pickle.py::test_complete[png]` fails | Model resets `_align_label_groups` at the end of `Figure.align_labels` (`lib/matplotlib/figure.py`), which discards the alignment. Gold makes `cbook.Grouper` picklable with `__getstate__`/`__setstate__` (`lib/matplotlib/cbook.py`). |
| gpt-oss | sympy-20916 | wrong file | FAIL_TO_PASS `test_super_sub` fails | Model adds subscript forms for all Greek letters in `sympy/printing/pretty/pretty_symbology.py`. Gold changes the name-with-digits regex in `sympy/printing/conventions.py` (the line Kimi found). |
| gpt-oss | django-11299 (step-limit example) | gave up / ran out | 50 steps and an empty submission. 23 of the 50 were format errors (`Expected exactly 1 action, found N`). The run ended with `sed -i '1i import re'` and then `sed -i '5i import re'` on `constraints.py`, inserting the same import twice. | Model rewrote `CheckConstraint._get_check_sql` with a regex in `django/db/models/constraints.py`. Gold passes `simple_col` through `Query._add_q` in `django/db/models/sql/query.py`. 20 of its 28 commands were reads/greps. It looked at `query.py` 4 times but never edited it. Kimi resolved this task. |

**Totals:**

| Cause | Kimi (of 3) | gpt-oss submitted (of 6) |
|---|---:|---:|
| wrong file | 0 | 3 |
| wrong fix | 3 | 2 |
| broke other tests | 0 | 1 |

All 13 gpt-oss step-limit runs are *gave up / ran out*.

## Patterns

**Kimi's misses are all near misses.** Every failure is in the gold file, two of them in the gold function, and none broke a passing test. In each one it fixed the spot the issue pointed at and stopped short:

- the factory but not the `Meta` path;
- the caller (`swap_dims`) but not the helper (`to_index_variable`);
- an ASCII digit class (`[0-9]`) where the hidden test uses a Unicode digit.

**gpt-oss's submitted failures are mostly in the wrong place.** Half (3 of 6) patch where the symptom shows up rather than where the bug is: the postgres widget, `align_labels` and the pretty-printer tables. Its one right-place fix with a side effect (django-14493) broke 30 storage tests that the module's own test file would have shown.

**Both models fail django-15916 and sympy-20916.** On django-15916 they wrote the same incomplete change.

**gpt-oss's step-limit runs are dominated by format errors.**

- **The share of the budget:** across all 13 runs, 259 of 650 steps (40%) were format errors.
- **What the errors were:** across all 25 gpt-oss trajectories, the "found N" counts show nearly all were replies with several command blocks. Only 27 replies had none. This persisted despite the backticks-v2 format rules.
- **`apply_patch`:** it still appears among the final commands of 4 runs (django-11299, django-12143, sphinx-9281, sympy-13551). That tool doesn't exist in the container.

So gpt-oss used only about 30 real commands per run before the 50-step limit. Kimi resolved 12 of these 13 tasks in its own runs.
