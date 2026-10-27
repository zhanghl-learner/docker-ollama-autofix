"""Behavioral tests for the order-pricing example."""

import sys
import unittest
from pathlib import Path

# Keep the src-layout fixture self-contained in local and isolated workspaces.
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from order_service import line_total


class PricingTests(unittest.TestCase):
    """Cover ordinary, boundary, and invalid order lines."""

    def test_regular_quantity_has_no_discount(self) -> None:
        self.assertEqual(line_total(250, 3), 750)

    def test_bulk_discount_starts_at_ten_units(self) -> None:
        self.assertEqual(line_total(250, 10), 2250)

    def test_bulk_discount_continues_above_boundary(self) -> None:
        self.assertEqual(line_total(250, 12), 2700)

    def test_invalid_quantity_is_rejected(self) -> None:
        with self.assertRaises(ValueError):
            line_total(250, 0)


if __name__ == "__main__":
    unittest.main()
