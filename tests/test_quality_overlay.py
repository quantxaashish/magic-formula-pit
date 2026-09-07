"""Unit tests for magicformula.quality_overlay (SPEC.md section 8, Phase 2).

Synthetic data throughout for the pure signal functions, correct answers
worked out by hand - same pattern as test_ranker.py/test_portfolio.py.
Real-case validation against Ashok Leyland/VEDL/PAGEIND lives in
test_quality_overlay_real_cases.py, reusing the fixtures and findings
already built up earlier in this project rather than starting fresh.
"""

from datetime import date

from magicformula.quality_overlay import (
    QualityAdjustedStock,
    QualityInputs,
    QualityOverlayConfig,
    apply_quality_overlay,
    compute_accrual_flag,
    compute_f_score,
    compute_momentum,
    compute_promoter_pledge_flag,
    compute_z_score,
)
from magicformula.ranker import RankedStock


def _inputs(**overrides) -> QualityInputs:
    defaults = dict(
        symbol="TEST", net_profit=100.0, cash_from_operations=150.0,
        total_assets=1000.0, borrowings=200.0, other_liabilities=100.0,
        equity_capital=100.0, reserves=500.0, sales=500.0, ebit=100.0,
        market_cap=2000.0, promoter_pledge_percentage=None,
        prior_net_profit=50.0, prior_total_assets=1000.0, prior_borrowings=300.0,
        prior_equity_capital=100.0, prior_sales=400.0,
    )
    defaults.update(overrides)
    return QualityInputs(**defaults)


# --- F-score ----------------------------------------------------------------


def test_f_score_perfect_score_all_seven_tests_pass():
    # ROA = 100/1000 = 10% > 0; prior ROA = 50/1000 = 5% < 10% -> improved
    # CFO 150 > 0; CFO 150 > net profit 100
    # leverage = 200/1000=20%; prior leverage = 300/1000=30% -> improved (decreased)
    # equity_capital 100 <= prior 100 -> no new shares
    # turnover = 500/1000=0.5; prior = 400/1000=0.4 -> improved
    inputs = _inputs()
    result = compute_f_score(inputs)
    assert result.score == 7
    assert result.max_score == 7
    assert all(result.components.values())


def test_f_score_worst_case_all_seven_tests_fail():
    inputs = _inputs(
        net_profit=-50.0, cash_from_operations=-100.0,
        prior_net_profit=-20.0, prior_total_assets=1000.0,
        borrowings=400.0, prior_borrowings=200.0,
        equity_capital=150.0, prior_equity_capital=100.0,
        sales=300.0, prior_sales=400.0,
    )
    # ROA = -50/1000 = -5% -> fails (not > 0)
    # prior ROA = -20/1000 = -2%; current -5% < -2% -> NOT improved -> fails
    # CFO -100 -> fails (not > 0); CFO -100 > net profit -50? No -> fails
    # leverage = 400/1000=40%; prior=200/1000=20%; 40% < 20%? No -> fails
    # equity 150 <= prior 100? No -> new shares issued -> fails
    # turnover = 300/1000=0.3; prior=400/1000=0.4; 0.3 > 0.4? No -> fails
    result = compute_f_score(inputs)
    assert result.score == 0
    assert result.max_score == 7
    assert not any(v for v in result.components.values() if v is not None)


def test_f_score_missing_prior_year_data_marks_delta_tests_none_not_failed():
    inputs = _inputs(
        prior_net_profit=None, prior_total_assets=None,
        prior_borrowings=None, prior_equity_capital=None, prior_sales=None,
    )
    result = compute_f_score(inputs)
    # Only the 3 current-year-only tests (ROA>0, CFO>0, CFO>NP) are evaluable
    assert result.max_score == 3
    assert result.score == 3  # all three pass with the perfect-case current-year numbers
    assert result.components["roa_improved"] is None
    assert result.components["leverage_improved"] is None
    assert result.components["no_new_shares_issued"] is None
    assert result.components["turnover_improved"] is None


def test_f_score_missing_net_profit_marks_profitability_tests_none():
    inputs = _inputs(net_profit=None)
    result = compute_f_score(inputs)
    assert result.components["roa_positive"] is None
    assert result.components["cfo_exceeds_net_profit"] is None
    assert result.components["roa_improved"] is None
    # CFO > 0 doesn't need net_profit, still evaluable
    assert result.components["cfo_positive"] is True


# --- Z-score ------------------------------------------------------------


def test_z_score_hand_computed_safe_zone():
    # Z = 1.4*(500/1000) + 3.3*(100/1000) + 0.6*(2000/300) + 1.0*(500/1000)
    #   = 1.4*0.5 + 3.3*0.1 + 0.6*6.6667 + 1.0*0.5
    #   = 0.7 + 0.33 + 4.0 + 0.5 = 5.53
    inputs = _inputs(reserves=500.0, ebit=100.0, total_assets=1000.0,
                      market_cap=2000.0, borrowings=200.0, other_liabilities=100.0, sales=500.0)
    result = compute_z_score(inputs)
    assert result.score is not None
    assert round(result.score, 2) == 5.53
    assert result.zone == "safe"


def test_z_score_hand_computed_distress_zone():
    # Thin reserves, weak EBIT, heavy liabilities relative to market cap, low sales/TA
    # Z = 1.4*(10/1000) + 3.3*(5/1000) + 0.6*(100/900) + 1.0*(200/1000)
    #   = 0.014 + 0.0165 + 0.0667 + 0.2 = 0.297
    inputs = _inputs(reserves=10.0, ebit=5.0, total_assets=1000.0,
                      market_cap=100.0, borrowings=700.0, other_liabilities=200.0, sales=200.0)
    result = compute_z_score(inputs)
    assert result.score is not None
    assert round(result.score, 3) == 0.297
    assert result.zone == "distress"


def test_z_score_none_when_reserves_missing():
    inputs = _inputs(reserves=None)
    result = compute_z_score(inputs)
    assert result.score is None
    assert result.zone is None


def test_z_score_none_when_total_liabilities_is_zero():
    inputs = _inputs(borrowings=0.0, other_liabilities=0.0)
    result = compute_z_score(inputs)
    assert result.score is None


# --- Accrual flag ------------------------------------------------------


def test_accrual_flag_below_threshold_not_flagged():
    # (100 - 90) / 1000 = 0.01, well under the 0.10 default threshold
    inputs = _inputs(net_profit=100.0, cash_from_operations=90.0, total_assets=1000.0)
    assert compute_accrual_flag(inputs) is False


def test_accrual_flag_above_threshold_is_flagged():
    # (100 - (-50)) / 1000 = 0.15, above the 0.10 default threshold
    inputs = _inputs(net_profit=100.0, cash_from_operations=-50.0, total_assets=1000.0)
    assert compute_accrual_flag(inputs) is True


def test_accrual_flag_none_when_cfo_missing():
    inputs = _inputs(cash_from_operations=None)
    assert compute_accrual_flag(inputs) is None


# --- Promoter pledge -----------------------------------------------------


def test_promoter_pledge_flag_above_threshold():
    inputs = _inputs(promoter_pledge_percentage=40.1)
    assert compute_promoter_pledge_flag(inputs) is True


def test_promoter_pledge_flag_below_threshold():
    inputs = _inputs(promoter_pledge_percentage=15.0)
    assert compute_promoter_pledge_flag(inputs) is False


def test_promoter_pledge_flag_none_treated_as_not_flagged_not_ambiguous():
    # Deliberately a plain bool, not Optional - see the function's own
    # docstring for why None (no disclosure) must not read as "can't tell".
    inputs = _inputs(promoter_pledge_percentage=None)
    assert compute_promoter_pledge_flag(inputs) is False


# --- Momentum -------------------------------------------------------------


def test_momentum_hand_computed_positive_return():
    series = {date(2025, 1, 1): 100.0, date(2025, 7, 1): 120.0}
    momentum = compute_momentum(series, as_of=date(2025, 7, 1), lookback_days=181)
    assert momentum is not None
    assert round(momentum, 4) == 0.20


def test_momentum_none_when_either_endpoint_outside_tolerance():
    series = {date(2020, 1, 1): 100.0}  # far outside any real 180d lookback window
    momentum = compute_momentum(series, as_of=date(2025, 7, 1), lookback_days=180, max_days_tolerance=14)
    assert momentum is None


def test_momentum_never_uses_a_future_price():
    series = {date(2025, 1, 1): 100.0, date(2025, 12, 31): 999.0}
    momentum = compute_momentum(series, as_of=date(2025, 1, 1), lookback_days=180, max_days_tolerance=14)
    # The only eligible "now" price is date(2025,1,1) itself (100); the
    # Dec 31 value must never leak in even though it's in the dict.
    assert momentum is None  # no eligible "then" price 180 days earlier


# --- apply_quality_overlay: the passthrough guarantee ----------------------


def _ranked_stock(symbol: str, combined_rank: int) -> RankedStock:
    return RankedStock(
        symbol=symbol, cap_bucket="large", roce=0.2, ey=0.1,
        rank_roce=combined_rank, rank_ey=combined_rank, combined_rank=combined_rank,
        position=combined_rank,
    )


def test_apply_quality_overlay_with_everything_disabled_is_a_pure_passthrough():
    ranked = [_ranked_stock("A", 1), _ranked_stock("B", 2), _ranked_stock("C", 3)]
    # Deliberately terrible quality inputs for every stock - if the
    # default config filtered anything, this would catch it immediately.
    bad_inputs = {
        s.symbol: _inputs(
            symbol=s.symbol, net_profit=-999.0, cash_from_operations=-999.0,
            promoter_pledge_percentage=99.0,
        )
        for s in ranked
    }

    results = apply_quality_overlay(ranked, bad_inputs, QualityOverlayConfig())

    assert [r.ranked.symbol for r in results] == ["A", "B", "C"]  # order unchanged
    assert all(not r.excluded_by_overlay for r in results)  # nothing excluded
    assert all(r.exclusion_reason is None for r in results)


def test_apply_quality_overlay_missing_quality_data_is_never_excluded():
    ranked = [_ranked_stock("A", 1)]
    config = QualityOverlayConfig(
        enable_f_score_filter=True, min_f_score_fraction=0.9,
        enable_accrual_filter=True, enable_promoter_pledge_filter=True,
    )
    results = apply_quality_overlay(ranked, quality_inputs_by_symbol={}, config=config)
    assert results[0].excluded_by_overlay is False
    assert results[0].f_score is None


def test_apply_quality_overlay_f_score_filter_excludes_below_threshold():
    ranked = [_ranked_stock("GOOD", 1), _ranked_stock("BAD", 2)]
    inputs = {
        "GOOD": _inputs(symbol="GOOD"),  # perfect 7/7 case
        "BAD": _inputs(
            symbol="BAD", net_profit=-50.0, cash_from_operations=-100.0,
            prior_net_profit=-20.0, borrowings=400.0, prior_borrowings=200.0,
            equity_capital=150.0, sales=300.0,
        ),  # 0/7 case
    }
    config = QualityOverlayConfig(enable_f_score_filter=True, min_f_score_fraction=0.5)
    results = apply_quality_overlay(ranked, inputs, config)

    by_symbol = {r.ranked.symbol: r for r in results}
    assert by_symbol["GOOD"].excluded_by_overlay is False
    assert by_symbol["BAD"].excluded_by_overlay is True
    assert "F-score" in by_symbol["BAD"].exclusion_reason


def test_apply_quality_overlay_promoter_pledge_filter():
    ranked = [_ranked_stock("PLEDGED", 1)]
    inputs = {"PLEDGED": _inputs(symbol="PLEDGED", promoter_pledge_percentage=40.1)}
    config = QualityOverlayConfig(enable_promoter_pledge_filter=True, promoter_pledge_threshold=20.0)
    results = apply_quality_overlay(ranked, inputs, config)
    assert results[0].excluded_by_overlay is True
    assert "pledge" in results[0].exclusion_reason
