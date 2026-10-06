"""Order maths."""


def average_order_value(totals: list[float]) -> float:
    if not totals:
        return 0.0
    return sum(totals) / len(totals)


def discount(price: float, percent: float) -> float:
    """Apply a discount; ``percent`` is on a 0-100 scale (20 means 20%)."""
    return price - price * percent / 100
