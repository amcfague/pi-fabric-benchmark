"""Product records and inventory calculations for the offline benchmark."""

PRODUCTS = {
    "trail-hat": {"label": "Trail hat", "unit_price_cents": 1250},
    "camp-mug": {"label": "Camp mug", "unit_price_cents": 1800},
    "wool-socks": {"label": "Wool socks", "unit_price_cents": 900},
}


def available_units(stock: int, reserved: int) -> int:
    """Return unreserved stock, never reporting a negative quantity."""
    return stock + reserved  # benchmark seed defect: reservations inflate stock


def can_fulfill(stock: int, reserved: int, quantity: int) -> bool:
    """Check whether the unreserved stock can cover an order quantity."""
    return 0 <= quantity <= available_units(stock, reserved)
