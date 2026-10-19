# Money helpers. On-chain amounts are int atomic units (EURC 6 decimals, tCZK 2 = haléře);
# mandate limits are int euro cents (SPEC §6.1). tCZK -> EUR at the fixed demo rate.
from decimal import ROUND_CEILING, Decimal

from bank import config


def decimals(asset: str) -> int:
    return config.ASSETS[asset]["decimals"]


def to_atomic(asset: str, amount: str | Decimal) -> int:
    """'5.00' -> 5000000 for EURC. Raises on more precision than the asset has, or on amount <= 0."""
    q = Decimal(str(amount)) * (Decimal(10) ** decimals(asset))
    if q != q.to_integral_value():
        raise ValueError(f"{amount} has more than {decimals(asset)} decimals for {asset}")
    if q <= 0:
        raise ValueError(f"amount must be positive, got {amount}")
    return int(q)


def to_eur_cents(asset: str, atomic: int) -> int:
    """Value in euro cents for mandate checks; rounded up so limits are never undercounted."""
    if atomic < 0:
        raise ValueError("negative amount")
    if asset == "EURC":
        cents = Decimal(atomic) / Decimal(10_000)
    elif asset == "tCZK":
        cents = Decimal(atomic) / Decimal(config.CZK_PER_EUR_DEMO)  # haléře / (Kč per €) = cents
    else:
        raise ValueError(f"unknown asset {asset}")
    return int(cents.to_integral_value(rounding=ROUND_CEILING))


def from_eur_cents(asset: str, cents: int) -> int:
    """Euro cents -> atomic units of the asset at the demo rate (5 € -> 12500 haléřů)."""
    if asset == "EURC":
        return cents * 10_000
    if asset == "tCZK":
        return cents * config.CZK_PER_EUR_DEMO
    raise ValueError(f"unknown asset {asset}")


def fmt(asset: str, atomic: int) -> str:
    """'5,00 €', '0,225 €', '125,00 Kč' — at least 2 decimals, Czech decimal comma."""
    value = Decimal(atomic).scaleb(-decimals(asset))
    places = max(2, -value.normalize().as_tuple().exponent)
    text = f"{value:.{places}f}".replace(".", ",")
    return f"{text} {'Kč' if asset == 'tCZK' else '€'}"


def per_item_split(total: int, count: int, accepted: int) -> tuple[int, int]:
    """Pay per accepted item (SPEC §8): rounded down, the remainder goes back to the buyer.
    total*accepted//count instead of (total//count)*accepted: same result whenever the price divides
    evenly (5 € / 20 items), but a fully accepted deal pays the seller the whole price, no dust.
    Returns (to_seller, back_to_buyer); they always sum to total."""
    if total < 0 or count <= 0 or not 0 <= accepted <= count:
        raise ValueError("bad item counts")
    to_seller = total * accepted // count
    return to_seller, total - to_seller
