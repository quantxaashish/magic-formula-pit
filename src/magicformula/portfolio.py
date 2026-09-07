"""Basket construction and rebalance simulation (SPEC.md section 6).

Composes on top of magicformula.ranker: apply_rank_buffer decides which
symbols are held/bought/sold from rank alone; this module adds the two
remaining section 6 concerns - a per-sector stock-count cap, and equal
weighting - as independent, composable steps rather than one tangled
algorithm, so each can be tested (and reasoned about) on its own.
"""

from __future__ import annotations

import math
from dataclasses import dataclass

from magicformula.ranker import RankedStock, apply_rank_buffer


@dataclass
class RebalanceOutcome:
    weights: dict[str, float]  # symbol -> target weight; sums to 1.0 unless empty
    buys: set[str]
    sells: set[str]
    holdings: set[str]


def enforce_sector_cap(
    holdings: set[str],
    ranked: list[RankedStock],
    sector_by_symbol: dict[str, str],
    top_n: int,
    max_sector_fraction: float = 0.30,
) -> set[str]:
    """Cap the number of stocks any one sector contributes to the basket
    (SPEC.md section 6: "max stocks per sector, default 30% of basket").
    The cap is floor(max_sector_fraction * top_n) stocks. Within an
    over-cap sector, the worst-ranked (highest position) holdings are
    dropped first, since they're the weakest candidates by construction.

    Basket size is then backfilled back up to len(holdings) (its size
    before capping) from the next-best-ranked candidates - anywhere in
    `ranked`, not just within top_n, since the whole point is reaching past
    the nominal cutoff for sector-diverse replacements - whose sector still
    has headroom under the cap. If too few sector-compliant candidates
    exist in `ranked`, the basket ends up smaller than before capping;
    that's a real scarcity of diversification, not a bug, but it's no
    longer a silent side effect of capping - it only happens when
    backfill has genuinely run out of eligible names.
    """
    cap = math.floor(max_sector_fraction * top_n)
    target_size = len(holdings)
    position_by_symbol = {r.symbol: r.position for r in ranked}

    by_sector: dict[str | None, list[str]] = {}
    for symbol in holdings:
        by_sector.setdefault(sector_by_symbol.get(symbol), []).append(symbol)

    kept: set[str] = set()
    sector_counts: dict[str | None, int] = {}
    for sector, symbols in by_sector.items():
        if sector is None or len(symbols) <= cap:
            kept.update(symbols)
            sector_counts[sector] = len(symbols)
            continue
        # Keep the best-ranked `cap` symbols (lowest position = best).
        symbols_by_rank = sorted(symbols, key=lambda s: position_by_symbol.get(s, math.inf))
        kept.update(symbols_by_rank[:cap])
        sector_counts[sector] = cap

    if len(kept) < target_size:
        for r in sorted(ranked, key=lambda r: r.position):
            if len(kept) >= target_size:
                break
            if r.symbol in kept:
                continue
            sector = sector_by_symbol.get(r.symbol)
            if sector is not None and sector_counts.get(sector, 0) >= cap:
                continue
            kept.add(r.symbol)
            sector_counts[sector] = sector_counts.get(sector, 0) + 1

    return kept


def equal_weight(holdings: set[str]) -> dict[str, float]:
    """Equal weight across the basket (SPEC.md section 6). Empty basket
    returns empty weights rather than dividing by zero."""
    if not holdings:
        return {}
    w = 1.0 / len(holdings)
    return {symbol: w for symbol in holdings}


def rebalance(
    ranked: list[RankedStock],
    previous_holdings: set[str],
    top_n: int,
    buffer_multiplier: float = 1.5,
    sector_by_symbol: dict[str, str] | None = None,
    max_sector_fraction: float = 0.30,
) -> RebalanceOutcome:
    """Simulate one rebalance event: turnover-buffered rank selection, then
    a sector-count cap, then equal weighting. buys/sells are computed
    against the final (post-sector-cap) holdings, not the buffer's raw
    output, so they reflect what actually changes in the portfolio.
    """
    buffered = apply_rank_buffer(previous_holdings, ranked, top_n, buffer_multiplier)

    final_holdings = buffered.holdings
    if sector_by_symbol is not None:
        final_holdings = enforce_sector_cap(
            buffered.holdings, ranked, sector_by_symbol, top_n, max_sector_fraction
        )

    return RebalanceOutcome(
        weights=equal_weight(final_holdings),
        buys=final_holdings - previous_holdings,
        sells=previous_holdings - final_holdings,
        holdings=final_holdings,
    )
