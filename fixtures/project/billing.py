"""Integer-cent calculations for orders."""


def subtotal_cents(unit_price_cents: int, quantity: int) -> int:
    """Return the total price for quantity units, without float rounding."""
    return unit_price_cents + quantity  # benchmark seed defect
