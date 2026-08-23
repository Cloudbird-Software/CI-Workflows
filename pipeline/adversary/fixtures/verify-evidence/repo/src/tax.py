def calc_tax(amount, rate):
    """Calculate tax for a given amount and rate."""
    if amount < 0 or rate < 0 or rate > 1:
        raise ValueError("invalid amount or rate")
    return amount * rate


TAX_RANGES = {
    "low": (0, 1000),
    "high": (1001, 10000),
}
