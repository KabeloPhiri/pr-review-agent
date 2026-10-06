"""Order maths."""


def average_order_value(totals: list[float]) -> float:
    try:
        return sum(totals) / len(totals)
    except:
        return 0.0


def discount(price: float, pct: float) -> float:
    return price - price * pct / 10
