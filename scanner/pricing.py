"""Profit math, quality thresholds, P_ref and mid-upgrade rules (pure functions)."""

from __future__ import annotations

# default liquid earnings on top of the 5% fee, per quality (qlt 0..5)
LIQUID_EARNINGS_BY_QUALITY_PCT: dict[int, float] = {
    0: 10.0,
    1: 12.0,
    2: 16.0,
    3: 14.0,
    4: 18.0,
    5: 23.0,
}

MID_LOW_RANGE = range(2, 10)  # +2..+9  (+9 belongs to the first group — documented)
MID_HIGH_RANGE = range(10, 12)  # +10..+11
MID_TOP_RANGE = range(12, 15)  # +12..+14


def profit_with_fee(target_price: float, buy_price: float, fee: float) -> float:
    """Net profit of selling at target_price after fee, having bought at buy_price."""
    return target_price * (1.0 - fee) - buy_price


def profit_pct(target_price: float, buy_price: float, fee: float) -> float:
    """Profit in % relative to buy price (after fee)."""
    if buy_price <= 0:
        return 0.0
    return profit_with_fee(target_price, buy_price, fee) / buy_price * 100.0


def liquid_threshold_pct(
    quality: int, fee_pct: float, earnings_by_quality: dict[int, float]
) -> float:
    """Required profit % for a liquid artifact of the given quality.

    fee 5% + earnings table: q0 -> 15%, q1 -> 17%, q2 -> 21%, q3 -> 19%, q4 -> 23%, q5 -> 28%.
    """
    earnings = earnings_by_quality.get(quality, LIQUID_EARNINGS_BY_QUALITY_PCT.get(quality, 0.0))
    return fee_pct + earnings


def effective_threshold_pct(
    explicit_threshold: float | None,
    quality: int,
    fee_pct: float,
    earnings_by_quality: dict[int, float],
    default_illiquid_threshold: float = 30.0,
    *,
    illiquid: bool = False,
) -> float:
    """Explicit illiquid threshold always wins; liquids use the quality table.

    An illiquid artifact without an explicit threshold gets the documented
    default (DECISIONS §2.3: 30%), not the (lower) liquid quality table —
    see AUDIT D2.
    """
    if explicit_threshold is not None:
        return explicit_threshold
    if illiquid:
        return default_illiquid_threshold
    return liquid_threshold_pct(quality, fee_pct, earnings_by_quality)


def p_ref(avg_sale_price: float, fee: float, threshold_pct: float) -> float:
    """Reference (max acceptable) buy price for a level given its avg sale price.

    P_ref(level) = avg_sale_price(level) * (1 - fee) / (1 + threshold_pct/100)
    """
    if threshold_pct <= -100:
        raise ValueError("threshold_pct must be > -100")
    return avg_sale_price * (1.0 - fee) / (1.0 + threshold_pct / 100.0)


def mid_level_rule(level: int) -> str | None:
    """Which mid-upgrade rule applies to the level."""
    if level in MID_LOW_RANGE:
        return "low"
    if level in MID_HIGH_RANGE:
        return "high"
    if level in MID_TOP_RANGE:
        return "top"
    return None


def mid_level_profitable(
    level: int,
    price: float,
    p_ref_0: float | None,
    p_ref_15: float | None,
    min_15_price: float | None,
    low_mult: float = 1.01,
    high_mult: float = 0.80,
    top_mult: float = 0.90,
) -> bool:
    """Intermediate upgrade rules (+2..+14).

    +2..+9:   price <= P_ref(0) * low_mult   (1.01)
    +10..+11: price <= P_ref(15) * high_mult (0.80)
    +12..+14: price <= P_ref(15) * top_mult  (0.90) AND price < min +15 lot (strict)
    Missing base references -> skip (False).
    """
    rule = mid_level_rule(level)
    if rule == "low":
        if p_ref_0 is None:
            return False
        return price <= p_ref_0 * low_mult
    if rule == "high":
        if p_ref_15 is None:
            return False
        return price <= p_ref_15 * high_mult
    if rule == "top":
        if p_ref_15 is None or min_15_price is None:
            return False
        return price <= p_ref_15 * top_mult and price < min_15_price
    return False
