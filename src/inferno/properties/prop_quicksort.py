"""quicksort(arr): returns the elements of arr in sorted order (duplicates included)."""

from collections import Counter

import importlib
import os

from hypothesis import given, settings
from hypothesis import strategies as st

SETTINGS = settings(max_examples=200, deadline=None, derandomize=True, database=None)
_PACKAGE = os.environ.get("INFERNO_PROGRAM_PACKAGE", "python_programs")
quicksort = importlib.import_module(f"{_PACKAGE}.quicksort").quicksort

# Small value range so duplicates are common.
ARRS = st.lists(st.integers(min_value=-20, max_value=20), max_size=40)


@SETTINGS
@given(ARRS)
def test_output_is_ordered(arr):
    out = quicksort(list(arr))
    assert all(out[i] <= out[i + 1] for i in range(len(out) - 1))


@SETTINGS
@given(ARRS)
def test_output_is_a_permutation_of_input(arr):
    assert Counter(quicksort(list(arr))) == Counter(arr)
