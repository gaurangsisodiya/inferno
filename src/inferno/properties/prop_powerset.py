"""powerset(arr): arr has no duplicates; returns every subset of arr, each once."""

import importlib
import os

from hypothesis import given, settings
from hypothesis import strategies as st

SETTINGS = settings(max_examples=200, deadline=None, derandomize=True, database=None)
_PACKAGE = os.environ.get("INFERNO_PROGRAM_PACKAGE", "python_programs")
powerset = importlib.import_module(f"{_PACKAGE}.powerset").powerset

ARRS = st.lists(st.integers(-50, 50), unique=True, max_size=8)


@SETTINGS
@given(ARRS)
def test_has_two_to_the_n_subsets(arr):
    assert len(powerset(list(arr))) == 2 ** len(arr)


@SETTINGS
@given(ARRS)
def test_each_item_is_a_subset_without_repeats(arr):
    for subset in powerset(list(arr)):
        assert len(set(subset)) == len(subset)
        assert set(subset) <= set(arr)


@SETTINGS
@given(ARRS)
def test_subsets_are_all_different_and_include_empty_and_full(arr):
    subsets = [frozenset(s) for s in powerset(list(arr))]
    assert len(set(subsets)) == len(subsets)
    assert frozenset() in subsets and frozenset(arr) in subsets
