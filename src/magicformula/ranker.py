"""Ranking, combined score, and turnover-buffer logic (SPEC.md sections 4 and 6).

Operates on magicformula.formulas.RankingMetrics. Exclusion (EBIT<=0, EV<=0,
sector/liquidity/listing-age exclusions from section 5) must already have
happened upstream - anything with metrics.excluded=True here is dropped
before ranking, never ranked or scored.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Literal

from magicformula.formulas import RankingMetrics

RankMode = Literal["within_bucket", "global"]


@dataclass
class RankedStock:
    symbol: str
    cap_bucket: str | None
    roce: float
    ey: float
    rank_roce: int
    rank_ey: int
    combined_rank: int
    position: int  # 1-indexed final order, after tie-break; 1 = best


@dataclass
class RebalanceResult:
    holdings: set[str]
    buys: set[str]
    sells: set[str]


def _dense_rank_descending(
    items: list[RankingMetrics], key: Literal["roce", "ey"]
) -> dict[str, int]:
    """Rank descending by key (rank 1 = highest). Ties broken by symbol for
    a deterministic, stable order - SPEC.md doesn't define single-metric
    tie-breaking, only the combined-rank tie-break (section 4)."""
    ordered = sorted(items, key=lambda m: (-getattr(m, key), m.symbol))
    return {m.symbol: i + 1 for i, m in enumerate(ordered)}


def _rank_group(items: list[RankingMetrics]) -> list[RankedStock]:
    rank_roce = _dense_rank_descending(items, "roce")
    rank_ey = _dense_rank_descending(items, "ey")

    scored = []
    for m in items:
        combined = rank_roce[m.symbol] + rank_ey[m.symbol]
        scored.append((m, combined))

    # Tie-break: lower combined_rank wins; among equal combined_rank,
    # higher EY wins (SPEC.md section 4).
    scored.sort(key=lambda pair: (pair[1], -pair[0].ey, pair[0].symbol))

    return [
        RankedStock(
            symbol=m.symbol,
            cap_bucket=None,
            roce=m.roce,
            ey=m.ey,
            rank_roce=rank_roce[m.symbol],
            rank_ey=rank_ey[m.symbol],
            combined_rank=combined,
            position=i + 1,
        )
        for i, (m, combined) in enumerate(scored)
    ]


def rank_universe(
    metrics: list[RankingMetrics],
    mode: RankMode = "within_bucket",
    cap_bucket_by_symbol: dict[str, str] | None = None,
) -> list[RankedStock]:
    """Rank an already-filtered universe. cap_bucket_by_symbol tags every
    result with its cap bucket for reporting; in within_bucket mode it also
    determines the ranking groups. Required for within_bucket mode.
    """
    eligible = [m for m in metrics if not m.excluded]
    cap_bucket_by_symbol = cap_bucket_by_symbol or {}

    if mode == "within_bucket":
        if not all(m.symbol in cap_bucket_by_symbol for m in eligible):
            raise ValueError(
                "within_bucket mode requires cap_bucket_by_symbol for every "
                "eligible symbol"
            )
        buckets: dict[str, list[RankingMetrics]] = {}
        for m in eligible:
            buckets.setdefault(cap_bucket_by_symbol[m.symbol], []).append(m)

        results: list[RankedStock] = []
        for bucket_name in sorted(buckets):
            for ranked in _rank_group(buckets[bucket_name]):
                ranked.cap_bucket = bucket_name
                results.append(ranked)
        return results

    if mode == "global":
        results = _rank_group(eligible)
        for ranked in results:
            ranked.cap_bucket = cap_bucket_by_symbol.get(ranked.symbol)
        return results

    raise ValueError(f"unknown rank mode: {mode!r}")


def select_basket(ranked: list[RankedStock], top_n: int) -> list[RankedStock]:
    """Top N by final position. In within_bucket mode, ranked already
    contains per-bucket positions, so callers wanting per-bucket baskets
    should filter by cap_bucket before calling this."""
    return sorted(ranked, key=lambda r: r.position)[:top_n]


def apply_rank_buffer(
    previous_holdings: set[str],
    ranked: list[RankedStock],
    top_n: int,
    buffer_multiplier: float = 1.5,
) -> RebalanceResult:
    """Turnover control (SPEC.md section 6): a held stock is only sold if it
    falls outside top_n * buffer_multiplier; a new stock is only bought if
    it's inside top_n. Stocks that dropped out of `ranked` entirely (e.g.
    newly excluded) are treated as falling outside any threshold and sold.
    """
    position_by_symbol = {r.symbol: r.position for r in ranked}
    hold_threshold = top_n * buffer_multiplier

    holdings: set[str] = set()
    buys: set[str] = set()
    sells: set[str] = set()

    for symbol in previous_holdings:
        position = position_by_symbol.get(symbol)
        if position is not None and position <= hold_threshold:
            holdings.add(symbol)
        else:
            sells.add(symbol)

    for r in ranked:
        if r.symbol in previous_holdings:
            continue
        if r.position <= top_n:
            buys.add(r.symbol)
            holdings.add(r.symbol)

    return RebalanceResult(holdings=holdings, buys=buys, sells=sells)
