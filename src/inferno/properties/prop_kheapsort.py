"""kheapsort(arr, k): arr has unique elements, each at most k places from its
sorted position; yields the elements in sorted order."""

from collections import Counter

import importlib
import os

from hypothesis import given, settings
from hypothesis import strategies as st

SETTINGS = settings(max_examples=200, deadline=None, derandomize=True, database=None)
_PACKAGE = os.environ.get("INFERNO_PROGRAM_PACKAGE", "python_programs")
kheapsort = importlib.import_module(f"{_PACKAGE}.kheapsort").kheapsort


@st.composite
def almost_sorted(draw):
    """Shuffle each block of k+1 consecutive sorted values: nothing moves more than k places."""
    values = sorted(draw(st.lists(st.integers(-1000, 1000), unique=True, max_size=40)))
    k = draw(st.integers(min_value=0, max_value=6))
    arr = []
    for start in range(0, len(values), k + 1):
        arr += draw(st.permutations(values[start:start + k + 1]))
    return arr, k


@SETTINGS
@given(almost_sorted())
def test_output_is_ordered(case):
    arr, k = case
    out = list(kheapsort(list(arr), k))
    assert all(out[i] <= out[i + 1] for i in range(len(out) - 1))


@SETTINGS
@given(almost_sorted())
def test_output_is_a_permutation_of_input(case):
    arr, k = case
    assert Counter(kheapsort(list(arr), k)) == Counter(arr)
