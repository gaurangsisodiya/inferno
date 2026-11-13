"""to_base(num, b): num > 0, 2 <= b <= 36; returns num written in base b
using the digits 0-9 then A-Z."""

import string

import importlib
import os

from hypothesis import given, settings
from hypothesis import strategies as st

SETTINGS = settings(max_examples=200, deadline=None, derandomize=True, database=None)
_PACKAGE = os.environ.get("INFERNO_PROGRAM_PACKAGE", "python_programs")
to_base = importlib.import_module(f"{_PACKAGE}.to_base").to_base

NUMS = st.integers(min_value=1, max_value=10**15)
BASES = st.integers(min_value=2, max_value=36)


@SETTINGS
@given(NUMS, BASES)
def test_parses_back_to_the_same_number(num, b):
    assert int(to_base(num, b), b) == num


@SETTINGS
@given(NUMS, BASES)
def test_uses_only_valid_uppercase_digits_without_leading_zero(num, b):
    out = to_base(num, b)
    digits = (string.digits + string.ascii_uppercase)[:b]
    assert out and all(c in digits for c in out)
    assert out[0] != "0"
