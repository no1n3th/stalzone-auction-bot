"""Filter chain: artifact match, sample size, wall, profit (base + mid)."""

from __future__ import annotations

from config.artifacts import ArtifactConfig
from models.lot import Lot
from scanner.filters import (
    ArtifactMatchFilter,
    CompetitorWallFilter,
    DedupFilter,
    ProfitFilter,
    SampleSizeFilter,
    ScanContext,
)

FEE = 0.05
EARNINGS = {0: 10.0, 1: 12.0, 2: 16.0, 3: 14.0, 4: 18.0, 5: 23.0}


def art(**kw) -> ArtifactConfig:
    base = dict(
        item_id="y5vw",
        name="Скорлупа",
        category="illiquid",
        min_quality=2,
        upgrades=(0, 15),
        profit_threshold_pct=30.0,
    )
    base.update(kw)
    return ArtifactConfig(**base)


def lot(price=100.0, quality=2, upgrade=0) -> Lot:
    return Lot(item_id="y5vw", price=price, quality=quality, upgrade=upgrade)


def make_ctx(**kw) -> ScanContext:
    base = dict(
        artifact=art(),
        fee=FEE,
        fee_pct=5.0,
        earnings_by_quality=EARNINGS,
        min_sample_size=5,
        threshold_mode="net",
        mid_low_mult=1.01,
        mid_high_mult=0.80,
        mid_top_mult=0.90,
    )
    base.update(kw)
    ctx = ScanContext(**base)
    for k, v in kw.items():
        setattr(ctx, k, v)
    return ctx


class TestArtifactMatch:
    def test_quality_below_min_rejected(self):
        ctx = make_ctx()
        assert not ArtifactMatchFilter().check(lot(quality=1), ctx)

    def test_tracked_upgrades_accepted(self):
        ctx = make_ctx()
        assert ArtifactMatchFilter().check(lot(upgrade=0), ctx)
        assert ArtifactMatchFilter().check(lot(upgrade=15), ctx)

    def test_mid_levels_considered(self):
        ctx = make_ctx()
        for lvl in (2, 5, 9, 10, 12, 14):
            assert ArtifactMatchFilter().check(lot(upgrade=lvl), ctx)
        assert not ArtifactMatchFilter().check(lot(upgrade=1), ctx)


class TestSampleSize:
    def test_base_needs_samples_and_avg(self):
        ctx = make_ctx(samples={(2, 0): 3}, avg_cache={(2, 0): 900.0})
        assert not SampleSizeFilter().check(lot(upgrade=0), ctx)  # 3 < 5
        ctx = make_ctx(samples={(2, 0): 5}, avg_cache={(2, 0): 900.0})
        assert SampleSizeFilter().check(lot(upgrade=0), ctx)
        ctx = make_ctx(samples={(2, 0): 5}, avg_cache={})
        assert not SampleSizeFilter().check(lot(upgrade=0), ctx)  # no avg

    def test_invalidated_group_rejected(self):
        ctx = make_ctx(
            samples={(2, 0): 9},
            avg_cache={(2, 0): 900.0},
            invalidated={(2, 0)},
        )
        assert not SampleSizeFilter().check(lot(upgrade=0), ctx)

    def test_mid_requires_valid_base(self):
        """Mid rules price against P_ref(base) — the base must be valid."""
        ctx = make_ctx(
            samples={(2, 0): 3, (2, 15): 10},
            avg_cache={(2, 0): 1000.0, (2, 15): 5000.0},
        )
        assert not SampleSizeFilter().check(lot(upgrade=5), ctx)  # base +0 invalid
        assert SampleSizeFilter().check(lot(upgrade=11), ctx)  # base +15 valid


class TestWall:
    def test_not_cheapest_skipped(self):
        ctx = make_ctx(market_min={(2, 0): 100.0})
        assert not CompetitorWallFilter().check(lot(150.0), ctx)
        assert CompetitorWallFilter().check(lot(100.0), ctx)
        assert CompetitorWallFilter().check(lot(99.0), ctx)


class TestProfit:
    def test_base_profitable(self):
        # threshold 30% explicit (illiquid); net mode: threshold excludes fee
        ctx = make_ctx(samples={}, avg_cache={(2, 0): 1000.0})
        assert not ProfitFilter().check(lot(900.0), ctx)
        assert ProfitFilter().check(lot(700.0), ctx)

    def test_liquid_uses_quality_table(self):
        ctx = make_ctx(
            artifact=art(category="liquid", profit_threshold_pct=None),
            samples={},
            avg_cache={(0, 0): 1000.0},
            threshold_mode="legacy",
        )
        assert ProfitFilter().check(lot(800.0, quality=0), ctx)
        assert not ProfitFilter().check(lot(900.0, quality=0), ctx)

    def test_mid_low(self):
        ctx = make_ctx(
            samples={},
            avg_cache={(2, 0): 1000.0, (2, 15): 5000.0},
            market_min={(2, 15): 4500.0},
        )
        assert ProfitFilter().check(lot(700.0, upgrade=5), ctx)
        assert not ProfitFilter().check(lot(750.0, upgrade=5), ctx)

    def test_mid_high(self):
        ctx = make_ctx(
            samples={},
            avg_cache={(2, 0): 1000.0, (2, 15): 5000.0},
            market_min={(2, 15): 4500.0},
        )
        assert ProfitFilter().check(lot(2900.0, upgrade=10), ctx)
        assert not ProfitFilter().check(lot(3000.0, upgrade=11), ctx)

    def test_mid_top_requires_min15(self):
        ctx = make_ctx(
            samples={},
            avg_cache={(2, 0): 1000.0, (2, 15): 5000.0},
            market_min={(2, 15): 4500.0},
        )
        assert ProfitFilter().check(lot(3200.0, upgrade=12), ctx)
        ctx2 = make_ctx(
            samples={},
            avg_cache={(2, 0): 1000.0, (2, 15): 5000.0},
            market_min={(2, 15): 3100.0},
        )
        assert not ProfitFilter().check(lot(3200.0, upgrade=12), ctx2)

    def test_mid_without_refs_skipped(self):
        ctx = make_ctx(samples={}, avg_cache={(2, 0): 1000.0})
        assert not ProfitFilter().check(lot(100.0, upgrade=12), ctx)


class TestDedup:
    def test_sent_skipped(self):
        ctx = make_ctx(sent_checker=lambda sig: True)
        assert not DedupFilter().check(lot(), ctx)
        ctx = make_ctx(sent_checker=lambda sig: False)
        assert DedupFilter().check(lot(), ctx)
