"""Profit formula, quality thresholds, P_ref, mid-upgrade rules."""

from __future__ import annotations

import pytest

from scanner.pricing import (
    LIQUID_EARNINGS_BY_QUALITY_PCT,
    effective_threshold_pct,
    liquid_threshold_pct,
    mid_level_profitable,
    mid_level_rule,
    p_ref,
    profit_pct,
    profit_with_fee,
)

FEE = 0.05
FEE_PCT = 5.0


class TestProfitFormula:
    def test_profit_with_fee(self):
        assert profit_with_fee(1000, 800, FEE) == pytest.approx(150.0)

    def test_profit_pct(self):
        assert profit_pct(1000, 800, FEE) == pytest.approx(18.75)

    def test_profit_pct_zero_buy(self):
        assert profit_pct(1000, 0, FEE) == 0.0

    @pytest.mark.parametrize("target,buy", [(100, 50), (50, 100), (1000, 999.9)])
    def test_hypothesis_sanity(self, target, buy):
        # net profit == revenue after fee - cost
        assert profit_with_fee(target, buy, FEE) == pytest.approx(target * 0.95 - buy)


class TestQualityThresholds:
    # fee 5% + earnings => thresholds per quality
    @pytest.mark.parametrize(
        "quality,expected",
        [(0, 15.0), (1, 17.0), (2, 21.0), (3, 19.0), (4, 23.0), (5, 28.0)],
    )
    def test_liquid_thresholds(self, quality, expected):
        assert liquid_threshold_pct(quality, FEE_PCT, LIQUID_EARNINGS_BY_QUALITY_PCT) == expected

    def test_illiquid_explicit_priority(self):
        assert effective_threshold_pct(200.0, 5, FEE_PCT, LIQUID_EARNINGS_BY_QUALITY_PCT) == 200.0
        assert effective_threshold_pct(30.0, 0, FEE_PCT, LIQUID_EARNINGS_BY_QUALITY_PCT) == 30.0
        assert effective_threshold_pct(50.0, 3, FEE_PCT, LIQUID_EARNINGS_BY_QUALITY_PCT) == 50.0

    def test_liquid_uses_table_when_no_explicit(self):
        assert effective_threshold_pct(None, 4, FEE_PCT, LIQUID_EARNINGS_BY_QUALITY_PCT) == 23.0


class TestPRef:
    def test_p_ref(self):
        # 1000 avg, fee 5%, threshold 25% => 1000*0.95/1.25 = 760
        assert p_ref(1000, FEE, 25.0) == pytest.approx(760.0)

    def test_p_ref_invalid(self):
        with pytest.raises(ValueError):
            p_ref(1000, FEE, -100.0)


class TestMidLevels:
    @pytest.mark.parametrize(
        "level,rule",
        [
            (2, "low"),
            (5, "low"),
            (9, "low"),  # +9 belongs to the first group
            (10, "high"),
            (11, "high"),
            (12, "top"),
            (14, "top"),
            (0, None),
            (1, None),
            (15, None),
        ],
    )
    def test_rule_mapping(self, level, rule):
        assert mid_level_rule(level) == rule

    def test_low_range_ok(self):
        assert mid_level_profitable(5, 100.0, p_ref_0=100.0, p_ref_15=None, min_15_price=None)

    def test_low_range_boundary(self):
        # exactly P_ref(0)*1.01 passes; above fails
        assert mid_level_profitable(9, 101.0, p_ref_0=100.0, p_ref_15=None, min_15_price=None)
        assert not mid_level_profitable(2, 101.01, p_ref_0=100.0, p_ref_15=None, min_15_price=None)

    def test_high_range(self):
        assert mid_level_profitable(10, 80.0, p_ref_0=None, p_ref_15=100.0, min_15_price=None)
        assert not mid_level_profitable(11, 80.01, p_ref_0=None, p_ref_15=100.0, min_15_price=None)

    def test_top_range_strict_min15(self):
        # must be <= P_ref(15)*0.90 AND strictly below min +15 lot
        assert mid_level_profitable(13, 89.0, p_ref_0=None, p_ref_15=100.0, min_15_price=95.0)
        assert not mid_level_profitable(13, 95.0, p_ref_0=None, p_ref_15=100.0, min_15_price=95.0)
        assert not mid_level_profitable(14, 91.0, p_ref_0=None, p_ref_15=100.0, min_15_price=95.0)

    def test_missing_refs_skip(self):
        assert not mid_level_profitable(5, 1.0, p_ref_0=None, p_ref_15=None, min_15_price=None)
        assert not mid_level_profitable(12, 1.0, p_ref_0=None, p_ref_15=100.0, min_15_price=None)

    def test_custom_multipliers(self):
        assert mid_level_profitable(
            2, 105.0, p_ref_0=100.0, p_ref_15=None, min_15_price=None, low_mult=1.10
        )


# --- AUDIT D2 / D3 ---


def test_illiquid_without_explicit_uses_default_30():
    from scanner.pricing import effective_threshold_pct

    earnings = {q: 10.0 + q for q in range(6)}
    for q in range(6):
        assert effective_threshold_pct(None, q, 5.0, earnings, illiquid=True) == 30.0


def test_net_mode_threshold_excludes_fee():
    """D3 (owner: commission counts once — net = 0.95*sale)."""
    from scanner.filters import ScanContext

    class _A:
        item_id = "x"
        extra_profit_pct = 0.0
        category = "liquid"
        profit_threshold_pct = None

    ctx = ScanContext(
        fee=5.0,
        fee_pct=5.0,
        earnings_by_quality={0: 10.0},
        min_sample_size=5,
        threshold_mode="net",
        artifact=_A(),
    )

    assert ctx.threshold_for(0) == 10.0  # 15% (5+10) minus the fee


def test_legacy_mode_keeps_double_counted_threshold():
    from scanner.filters import ScanContext

    class _A:
        item_id = "x"
        extra_profit_pct = 0.0
        category = "liquid"
        profit_threshold_pct = None

    ctx = ScanContext(
        fee=5.0,
        fee_pct=5.0,
        earnings_by_quality={0: 10.0},
        min_sample_size=5,
        threshold_mode="legacy",
        artifact=_A(),
    )

    assert ctx.threshold_for(0) == 15.0
