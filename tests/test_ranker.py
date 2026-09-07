"""Unit tests for magicformula.ranker (SPEC.md sections 4, 6, 12).

All test data is synthetic with the correct answer worked out by hand in
the comments, per section 12's instruction for the ranker's tie-breaking
and buffer logic specifically.
"""

from __future__ import annotations

import pytest

from magicformula.formulas import RankingMetrics
from magicformula.ranker import RankedStock, apply_rank_buffer, rank_universe, select_basket


def _metrics(symbol: str, roce: float, ey: float, excluded: bool = False) -> RankingMetrics:
    return RankingMetrics(
        symbol=symbol,
        ebit=1.0,
        capital_employed=1.0,
        roce=roce,
        ev=1.0,
        ey=ey,
        roce_mode_used="standard",
        excluded=excluded,
    )


# --- rank_universe: global mode, combined rank and tie-break --------------


def test_rank_universe_global_matches_hand_computed_order():
    # rank_roce (desc): A=1(.30) D=2(.25) B=3(.20) C=4(.10)
    # rank_ey   (desc): C=1(.30) B=2(.20) A=3(.10) D=4(.05)
    # combined: A=1+3=4  B=3+2=5  C=4+1=5  D=2+4=6
    # B and C tie at 5; higher EY wins -> C (.30) beats B (.20).
    # final order: A, C, B, D
    metrics = [
        _metrics("A", roce=0.30, ey=0.10),
        _metrics("B", roce=0.20, ey=0.20),
        _metrics("C", roce=0.10, ey=0.30),
        _metrics("D", roce=0.25, ey=0.05),
    ]

    ranked = rank_universe(metrics, mode="global")

    assert [r.symbol for r in ranked] == ["A", "C", "B", "D"]
    assert [r.position for r in ranked] == [1, 2, 3, 4]
    by_symbol = {r.symbol: r for r in ranked}
    assert by_symbol["A"].rank_roce == 1 and by_symbol["A"].rank_ey == 3
    assert by_symbol["A"].combined_rank == 4
    assert by_symbol["C"].combined_rank == 5
    assert by_symbol["B"].combined_rank == 5


def test_rank_universe_excludes_flagged_stocks():
    metrics = [
        _metrics("A", roce=0.30, ey=0.10),
        _metrics("EXCLUDED", roce=0.99, ey=0.99, excluded=True),
    ]
    ranked = rank_universe(metrics, mode="global")
    assert [r.symbol for r in ranked] == ["A"]


# --- rank_universe: within_bucket mode ------------------------------------


def test_rank_universe_within_bucket_ranks_each_bucket_independently():
    # large bucket: L1 roce=.30/ey=.10, L2 roce=.10/ey=.30
    #   rank_roce: L1=1 L2=2 ; rank_ey: L2=1 L1=2 ; combined tie at 3 each ->
    #   higher ey wins -> L2 first. Order: L2(pos1), L1(pos2)
    # small bucket: S1 roce=.05/ey=.05, S2 roce=.04/ey=.04
    #   S1 dominates on both -> S1(pos1), S2(pos2)
    metrics = [
        _metrics("L1", roce=0.30, ey=0.10),
        _metrics("L2", roce=0.10, ey=0.30),
        _metrics("S1", roce=0.05, ey=0.05),
        _metrics("S2", roce=0.04, ey=0.04),
    ]
    cap_bucket = {"L1": "large", "L2": "large", "S1": "small", "S2": "small"}

    ranked = rank_universe(metrics, mode="within_bucket", cap_bucket_by_symbol=cap_bucket)

    large = [r for r in ranked if r.cap_bucket == "large"]
    small = [r for r in ranked if r.cap_bucket == "small"]
    assert [r.symbol for r in sorted(large, key=lambda r: r.position)] == ["L2", "L1"]
    assert [r.position for r in sorted(large, key=lambda r: r.position)] == [1, 2]
    assert [r.symbol for r in sorted(small, key=lambda r: r.position)] == ["S1", "S2"]


def test_rank_universe_within_bucket_requires_full_cap_bucket_mapping():
    metrics = [_metrics("A", roce=0.30, ey=0.10)]
    with pytest.raises(ValueError):
        rank_universe(metrics, mode="within_bucket", cap_bucket_by_symbol={})


# --- select_basket ----------------------------------------------------------


def test_select_basket_returns_top_n_by_position():
    ranked = [
        RankedStock(
            symbol=f"S{i}", cap_bucket=None, roce=0.0, ey=0.0,
            rank_roce=i, rank_ey=i, combined_rank=i * 2, position=i,
        )
        for i in [3, 1, 2, 5, 4]
    ]
    basket = select_basket(ranked, top_n=3)
    assert [r.symbol for r in basket] == ["S1", "S2", "S3"]


# --- apply_rank_buffer: turnover control (SPEC.md section 6) --------------


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


def test_apply_rank_buffer_holds_sells_and_buys_by_construction():
    # top_n=10, buffer_multiplier=1.5 -> hold_threshold=15
    ranked = [_stub_ranked(i) for i in range(1, 21)]  # S1..S20, positions 1..20

    previous_holdings = {"S12", "S20", "S_gone"}  # S_gone isn't in `ranked` at all

    result = apply_rank_buffer(previous_holdings, ranked, top_n=10, buffer_multiplier=1.5)

    # S12: held, position 12 <= 15 -> kept
    # S20: held, position 20 > 15 -> sold
    # S_gone: held, absent from ranked entirely -> sold
    assert result.sells == {"S20", "S_gone"}

    # New (not previously held) candidates only bought if position <= top_n=10
    assert result.buys == {f"S{i}" for i in range(1, 11)}

    # Final holdings = kept + bought
    assert result.holdings == {f"S{i}" for i in range(1, 11)} | {"S12"}


def test_apply_rank_buffer_does_not_buy_new_stock_inside_buffer_but_outside_top_n():
    # S13 sits inside the 15-position hold threshold but was never held, and
    # its position (13) is outside top_n (10) - buying only triggers inside
    # top_n, so S13 should neither be bought nor end up in holdings.
    ranked = [_stub_ranked(i) for i in range(1, 21)]
    result = apply_rank_buffer(previous_holdings=set(), ranked=ranked, top_n=10, buffer_multiplier=1.5)
    assert "S13" not in result.buys
    assert "S13" not in result.holdings
    assert result.holdings == {f"S{i}" for i in range(1, 11)}
