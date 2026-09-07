"""Unit tests for magicformula.portfolio (SPEC.md section 6).

Synthetic data throughout, correct answer worked out by hand, matching the
approach in test_ranker.py.
"""

from __future__ import annotations

from magicformula.portfolio import enforce_sector_cap, equal_weight, rebalance
from magicformula.ranker import RankedStock


def _stub_ranked(position: int, symbol: str | None = None) -> RankedStock:
    return RankedStock(
        symbol=symbol or f"S{position}",
        cap_bucket=None,
        roce=0.0,
        ey=0.0,
        rank_roce=position,
        rank_ey=position,
        combined_rank=position * 2,
        position=position,
    )


# --- equal_weight ----------------------------------------------------------


def test_equal_weight_empty():
    assert equal_weight(set()) == {}


def test_equal_weight_splits_evenly_and_sums_to_one():
    weights = equal_weight({"A", "B", "C", "D"})
    assert weights == {"A": 0.25, "B": 0.25, "C": 0.25, "D": 0.25}
    assert sum(weights.values()) == 1.0


# --- enforce_sector_cap ------------------------------------------------------


def test_enforce_sector_cap_drops_worst_ranked_over_cap():
    # top_n=10, max_sector_fraction=0.3 -> cap = floor(3.0) = 3
    # TECH: S1..S5 (5 members, over cap) -> keep best-ranked 3: S1,S2,S3
    ranked = [_stub_ranked(i) for i in range(1, 6)]
    sector_by_symbol = {f"S{i}": "TECH" for i in range(1, 6)}
    holdings = {f"S{i}" for i in range(1, 6)}

    kept = enforce_sector_cap(holdings, ranked, sector_by_symbol, top_n=10, max_sector_fraction=0.30)

    assert kept == {"S1", "S2", "S3"}


def test_enforce_sector_cap_leaves_under_cap_sector_untouched():
    ranked = [_stub_ranked(i) for i in range(1, 3)]
    sector_by_symbol = {"S1": "TECH", "S2": "TECH"}
    holdings = {"S1", "S2"}

    kept = enforce_sector_cap(holdings, ranked, sector_by_symbol, top_n=10, max_sector_fraction=0.30)

    assert kept == {"S1", "S2"}  # 2 <= cap of 3, nothing dropped


def test_enforce_sector_cap_ignores_unmapped_sector():
    # A symbol with no sector mapping is never capped (unknown sector, not
    # "small" sector) - a conservative default given we can't judge
    # concentration for a sector we don't know.
    ranked = [_stub_ranked(i) for i in range(1, 6)]
    holdings = {f"S{i}" for i in range(1, 6)}

    kept = enforce_sector_cap(holdings, ranked, sector_by_symbol={}, top_n=10, max_sector_fraction=0.30)

    assert kept == holdings


# --- rebalance: buffer -> sector cap -> equal weight, composed -------------


def test_rebalance_applies_sector_cap_after_buffer_and_weights_evenly():
    # top_n=10 -> sector cap = floor(0.3*10) = 3
    # TECH = S1..S5, PHARMA = S6..S10, OTHER = S11..S20 (never reaches top_n)
    ranked = [_stub_ranked(i) for i in range(1, 21)]
    sector_by_symbol = {}
    for i in range(1, 6):
        sector_by_symbol[f"S{i}"] = "TECH"
    for i in range(6, 11):
        sector_by_symbol[f"S{i}"] = "PHARMA"
    for i in range(11, 21):
        sector_by_symbol[f"S{i}"] = "OTHER"

    outcome = rebalance(
        ranked,
        previous_holdings=set(),
        top_n=10,
        buffer_multiplier=1.5,
        sector_by_symbol=sector_by_symbol,
        max_sector_fraction=0.30,
    )

    # Buffer alone would select S1..S10 (size 10). Sector cap trims TECH and
    # PHARMA (5 members each) down to their best-ranked 3, dropping to 6 -
    # then backfills from the next-best-ranked OTHER candidates (S11, S12,
    # S13) until OTHER itself hits the same cap of 3, landing at 9 (one
    # short of 10: OTHER's own cap is the genuine limit on diversification
    # here, not a bug).
    assert outcome.holdings == {"S1", "S2", "S3", "S6", "S7", "S8", "S11", "S12", "S13"}
    assert outcome.buys == outcome.holdings  # nothing held before
    assert outcome.sells == set()
    assert outcome.weights == {s: 1 / 9 for s in outcome.holdings}


def test_rebalance_sells_a_buffer_kept_stock_if_sector_cap_drops_it():
    # S4 is already held and well inside the buffer's hold threshold on
    # rank alone - it only leaves the portfolio because its sector (TECH)
    # is over the cap, not because of turnover/rank logic. sells must
    # reflect that, not just falling-out-of-rank sells.
    ranked = [_stub_ranked(i) for i in range(1, 21)]
    sector_by_symbol = {}
    for i in range(1, 6):
        sector_by_symbol[f"S{i}"] = "TECH"
    for i in range(6, 11):
        sector_by_symbol[f"S{i}"] = "PHARMA"

    outcome = rebalance(
        ranked,
        previous_holdings={"S4"},
        top_n=10,
        buffer_multiplier=1.5,
        sector_by_symbol=sector_by_symbol,
        max_sector_fraction=0.30,
    )

    assert "S4" not in outcome.holdings
    assert "S4" in outcome.sells
