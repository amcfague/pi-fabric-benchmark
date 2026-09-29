"""Shipping charges and an integration quote across checkout modules."""

import billing
import catalog

STANDARD_FEE_CENTS = 599
FREE_THRESHOLD_CENTS = 5000


def shipping_fee_cents(
    subtotal_cents: int, free_threshold_cents: int = FREE_THRESHOLD_CENTS
) -> int:
    """Charge standard shipping below the threshold; waive it at the threshold."""
    if subtotal_cents > free_threshold_cents:  # benchmark seed defect
        return 0
    return STANDARD_FEE_CENTS


def quote_order(
    stock: int, reserved: int, unit_price_cents: int, quantity: int
) -> dict[str, int | bool]:
    """Combine inventory, pricing, and shipping for one product order."""
    available = catalog.available_units(stock, reserved)
    subtotal = billing.subtotal_cents(unit_price_cents, quantity)
    return {
        "available_units": available,
        "order_fulfillable": catalog.can_fulfill(stock, reserved, quantity),
        "subtotal_cents": subtotal,
        "shipping_fee_cents": shipping_fee_cents(subtotal),
    }
