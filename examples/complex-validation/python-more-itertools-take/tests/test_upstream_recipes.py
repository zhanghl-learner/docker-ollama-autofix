"""Behavioral contract retained from the pinned upstream recipe."""

import sys
import unittest
from pathlib import Path


sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from upstream_recipes import take


class TakeTests(unittest.TestCase):
    def test_take_returns_requested_prefix(self) -> None:
        self.assertEqual(take(3, range(10)), [0, 1, 2])

    def test_take_returns_every_item_when_input_is_short(self) -> None:
        self.assertEqual(take(10, range(3)), [0, 1, 2])

    def test_take_accepts_one_shot_iterators(self) -> None:
        self.assertEqual(take(2, iter(("a", "b", "c"))), ["a", "b"])


if __name__ == "__main__":
    unittest.main()
