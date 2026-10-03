"""Filter chain for auction lots: match -> dedup -> sample -> wall -> profit."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Protocol

from config.artifacts import ArtifactConfig
from models.lot import Lot
from scanner.pricing import (
    effective_threshold_pct,
    mid_level_profitable,
    p_ref,
    profit_pct,
)


class SentChecker(Protocol):
    def __call__(self, signature: str) -> bool: ...


@dataclass
class ScanContext:
    artifact: ArtifactConfig
    fee: float
    fee_pct: float
    earnings_by_quality: dict[int, float]
    avg_cache: dict[tuple[int, int], float] = field(default_factory=dict)
    samples: dict[tuple[int, int], int] = field(default_factory=dict)
    market_min: dict[tuple[int, int], float] = field(default_factory=dict)
    invalidated: set[tuple[int, int]] = field(default_factory=set)
    min_sample_size: int = 5
    threshold_mode: str = "net"
    mid_low_mult: float = 1.01
    mid_high_mult: float = 0.80
    mid_top_mult: float = 0.90
    sent_checker: SentChecker | None = None

    def threshold_for(self, quality: int) -> float:
        explicit = self.artifact.profit_threshold_pct
        threshold = effective_threshold_pct(
            explicit,
            quality,
            self.fee_pct,
            self.earnings_by_quality,
            illiquid=self.artifact.category == "illiquid",
        )
        # AUDIT D3: profit_pct already nets out the fee once; in "net" mode the
        # liquid quality table (fee + earnings) is reduced back to pure net.
        # Explicit thresholds — and the illiquid default — are owner-set net
        # targets and are used as-is in both modes (matches test boundaries).
        if self.threshold_mode == "net" and explicit is None and self.artifact.category == "liquid":
            return max(0.0, threshold - self.fee_pct)
        return threshold

    def avg(self, quality: int, upgrade: int) -> float | None:
        return self.avg_cache.get((quality, upgrade))

    def sample(self, quality: int, upgrade: int) -> int:
        return self.samples.get((quality, upgrade), 0)


class Filter:
    def check(self, lot: Lot, ctx: ScanContext) -> bool:  # pragma: no cover - interface
        raise NotImplementedError


class BidLotFilter(Filter):
    """Only lots with a buyout price are tradeable for us."""

    def check(self, lot: Lot, ctx: ScanContext) -> bool:
        return lot.price > 0 and not lot.is_expired


class ArtifactMatchFilter(Filter):
    """Quality gate + tracked upgrade levels (base 0/15 and mid 2..14)."""

    def check(self, lot: Lot, ctx: ScanContext) -> bool:
        art = ctx.artifact
        if lot.quality < art.effective_min_quality:
            return False
        if lot.upgrade in art.upgrades:
            return True
        return 2 <= lot.upgrade <= 14  # intermediate levels are always considered


class DedupFilter(Filter):
    def check(self, lot: Lot, ctx: ScanContext) -> bool:
        if ctx.sent_checker is None:
            return True
        return not ctx.sent_checker(lot.signature)


class SampleSizeFilter(Filter):
    """Base levels need enough sales history and a valid (not invalidated) average.

    Mid levels need valid base references (P_ref(0) / P_ref(15) per rule).
    """

    def check(self, lot: Lot, ctx: ScanContext) -> bool:
        q, lvl = lot.quality, lot.upgrade
        if lvl in ctx.artifact.upgrades:
            if (q, lvl) in ctx.invalidated:
                return False
            return ctx.avg(q, lvl) is not None and ctx.sample(q, lvl) >= ctx.min_sample_size
        # mid level: require the base reference(s) used by its rule
        base = (q, 0) if 2 <= lvl <= 9 else (q, 15)
        if base in ctx.invalidated:
            return False
        return ctx.avg(*base) is not None and ctx.sample(*base) >= ctx.min_sample_size


class CompetitorWallFilter(Filter):
    """Skip lots that are not the cheapest in their group (a cheaper wall exists)."""

    def __init__(self, tolerance: float = 1.0) -> None:
        self.tolerance = tolerance

    def check(self, lot: Lot, ctx: ScanContext) -> bool:
        group_min = ctx.market_min.get((lot.quality, lot.upgrade))
        if group_min is None:
            return True
        return lot.price <= group_min * self.tolerance


class ProfitFilter(Filter):
    """Base: profit vs avg >= threshold. Mid: intermediate rules with P_ref."""

    def check(self, lot: Lot, ctx: ScanContext) -> bool:
        q, lvl = lot.quality, lot.upgrade
        if lvl in ctx.artifact.upgrades:
            avg = ctx.avg(q, lvl)
            if avg is None:
                return False
            threshold = ctx.threshold_for(q)
            return profit_pct(avg, lot.price, ctx.fee) >= threshold
        # intermediate upgrade
        threshold = ctx.threshold_for(q)
        avg0 = ctx.avg(q, 0)
        avg15 = ctx.avg(q, 15)
        p0 = p_ref(avg0, ctx.fee, threshold) if avg0 is not None else None
        p15 = p_ref(avg15, ctx.fee, threshold) if avg15 is not None else None
        min15 = ctx.market_min.get((q, 15))
        return mid_level_profitable(
            lvl,
            lot.price,
            p0,
            p15,
            min15,
            low_mult=ctx.mid_low_mult,
            high_mult=ctx.mid_high_mult,
            top_mult=ctx.mid_top_mult,
        )


def default_chain() -> list[Filter]:
    return [
        BidLotFilter(),
        ArtifactMatchFilter(),
        DedupFilter(),
        SampleSizeFilter(),
        CompetitorWallFilter(),
        ProfitFilter(),
    ]
