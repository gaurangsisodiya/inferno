"""bucketsort(arr, k): arr holds ints in [0, k); returns them in sorted order."""

from collections import Counter

import importlib
import os

from hypothesis import given, settings
from hypothesis import strategies as st

SETTINGS = settings(max_examples=200, deadline=None, derandomize=True, database=None)
_PACKAGE = os.environ.get("INFERNO_PROGRAM_PACKAGE", "python_programs")
bucketsort = importlib.import_module(f"{_PACKAGE}.bucketsort").bucketsort


@st.composite
def arr_and_k(draw):
    k = draw(st.integers(min_value=1, max_value=30))
    arr = draw(st.lists(st.integers(min_value=0, max_value=k - 1), max_size=50))
    return arr, k


@SETTINGS
@given(arr_and_k())
def test_output_is_ordered(case):
    arr, k = case
    out = bucketsort(list(arr), k)
    assert all(out[i] <= out[i + 1] for i in range(len(out) - 1))


@SETTINGS
@given(arr_and_k())
def test_output_is_a_permutation_of_input(case):
    arr, k = case
    assert Counter(bucketsort(list(arr), k)) == Counter(arr)
