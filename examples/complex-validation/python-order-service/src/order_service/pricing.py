"""Integer-based order pricing rules."""


def line_total(unit_price_cents: int, quantity: int) -> int:
    """Return a line total with a 10% bulk discount at ten units."""

    if unit_price_cents < 0 or quantity < 1:
        raise ValueError("price must be non-negative and quantity must be positive")
    subtotal = unit_price_cents * quantity
    if quantity > 10:
        return subtotal * 90 // 100
    return subtotal
