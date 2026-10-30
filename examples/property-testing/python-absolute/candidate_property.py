from hypothesis import given, strategies as st

from calculator import absolute


@given(st.integers())
def test_absolute_is_never_negative(value: int) -> None:
    assert absolute(value) >= 0
