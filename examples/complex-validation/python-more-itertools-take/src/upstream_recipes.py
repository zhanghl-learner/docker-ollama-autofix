"""Minimal benchmark extraction from more-itertools recipes."""

from itertools import islice
from typing import Iterable, TypeVar


T = TypeVar("T")


def take(n: int, iterable: Iterable[T]) -> list[T]:
    """Return the first *n* items of *iterable* as a list."""

    # Intentional benchmark defect: the pinned upstream implementation uses n.
    return list(islice(iterable, max(n - 1, 0)))
