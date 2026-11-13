"""is_valid_parenthesization(parens): parens is a string of '(' and ')';
returns whether they are properly nested."""

import importlib
import os

from hypothesis import given, settings
from hypothesis import strategies as st

SETTINGS = settings(max_examples=200, deadline=None, derandomize=True, database=None)
_PACKAGE = os.environ.get("INFERNO_PROGRAM_PACKAGE", "python_programs")
is_valid_parenthesization = importlib.import_module(
    f"{_PACKAGE}.is_valid_parenthesization"
).is_valid_parenthesization

# Properly nested strings, built from the grammar  S -> "" | "(" S ")" | S S.
NESTED = st.recursive(
    st.just(""),
    lambda s: st.builds(lambda x: f"({x})", s) | st.builds(lambda x, y: x + y, s, s),
    max_leaves=20,
)


def reduces_to_empty(s: str) -> bool:
    """Repeatedly delete adjacent "()" pairs. Only properly nested strings vanish."""
    while "()" in s:
        s = s.replace("()", "")
    return s == ""


@SETTINGS
@given(NESTED)
def test_nested_strings_are_valid(s):
    assert is_valid_parenthesization(s) is True


@SETTINGS
@given(st.text(alphabet="()", max_size=30))
def test_agrees_with_pair_deletion(s):
    assert bool(is_valid_parenthesization(s)) == reduces_to_empty(s)


@SETTINGS
@given(NESTED, st.integers(min_value=1, max_value=5))
def test_extra_open_parens_are_invalid(s, n):
    assert not is_valid_parenthesization(s + "(" * n)
