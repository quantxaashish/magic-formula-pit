"""Unit tests for magicformula.backtest.

All synthetic, correct answer worked out by hand - same approach as
test_ranker.py/test_portfolio.py. No real historical data pull; that's
deliberately out of scope until the fundamentals pipeline is validated
(see docs/decisions/0003, 0004).
"""

from __future__ import annotations

from datetime import date
from pathlib import Path

import pytest

from magicformula.backtest import (
    clip_all_to_listing_dates,
    clip_to_listing_date,
    compute_annualized_volatility,
    compute_cagr,
    compute_equity_curve,
    compute_hit_rate,
    compute_max_drawdown,
    compute_period_return,
    compute_rolling_excess_return,
    compute_sharpe_ratio,
    compute_turnover,
    filter_point_in_time,
    run_backtest,
    run_rebalance_walk,
    write_basket_composition_csv,
)
from magicformula.data_fetch.fundamentals import FundamentalsRecord
from magicformula.portfolio import RebalanceOutcome
from magicformula.ranker import RankedStock


def _record(symbol: str, as_of_date: date) -> FundamentalsRecord:
    record = FundamentalsRecord(
        symbol=symbol,
        sector="SEC",
        broad_sector="BROAD",
        fiscal_year_end=date.fromordinal(as_of_date.toordinal() - 60),
        operating_profit=100,
        depreciation_amortization=0,
        total_assets=1000,
        other_liabilities=0,
        investments=0,
        cwip=0,
        borrowings=0,
        market_cap=1.0,
        source_url="https://example.invalid",
    )
    # as_of_date is derived from fiscal_year_end in __post_init__ (fiscal
    # year end + 60 days) - set it directly here so tests can pick exact
    # boundary values without reverse-engineering a fiscal year end.
    record.as_of_date = as_of_date
    return record


# --- filter_point_in_time ---------------------------------------------
#
# Decision 0011: the embargo's public-availability lag lives solely in
# FundamentalsRecord.as_of_date (fiscal_year_end + ANNUAL_FILING_LAG_DAYS)
# - filter_point_in_time itself no longer subtracts a second, independent
# lag from rebalance_date. The cutoff tested here is exactly
# rebalance_date, not rebalance_date minus some further buffer.


def test_filter_point_in_time_includes_record_exactly_at_the_cutoff():
    # A record whose as_of_date lands exactly on rebalance_date is
    # included (inclusive: "as of", not "strictly before").
    records = [_record("AT_CUTOFF", date(2020, 1, 1))]
    eligible = filter_point_in_time(records, date(2020, 1, 1))
    assert [r.symbol for r in eligible] == ["AT_CUTOFF"]


def test_filter_point_in_time_excludes_record_one_day_past_the_cutoff():
    records = [_record("TOO_RECENT", date(2020, 1, 2))]
    eligible = filter_point_in_time(records, date(2020, 1, 1))
    assert eligible == []


def test_filter_point_in_time_includes_well_old_record():
    records = [_record("OLD", date(2019, 1, 1))]
    eligible = filter_point_in_time(records, date(2020, 1, 1))
    assert [r.symbol for r in eligible] == ["OLD"]


# --- clip_to_listing_date / clip_all_to_listing_dates (decision 0006/0007) -
#
# The 3B Blackbio case that motivated this: a company can show
# fundamentals history predating its own NSE listing (screener.in reports
# the underlying entity's audited history, not years-since-listing).
# Without this clip, filter_point_in_time alone would happily pass an
# early, pre-listing record for an equally early rebalance date, ranking a
# stock nobody could have actually bought at the time.


def test_clip_to_listing_date_drops_pre_listing_fiscal_years():
    records = [
        _record("A", date(2015, 5, 30)),  # fiscal_year_end ~2015-03-31
        _record("A", date(2021, 5, 30)),  # fiscal_year_end ~2021-03-31
    ]
    clipped = clip_to_listing_date(records, listing_date=date(2020, 6, 1))
    assert [r.as_of_date for r in clipped] == [date(2021, 5, 30)]


def test_clip_to_listing_date_none_is_a_no_op():
    records = [_record("A", date(2015, 5, 30))]
    assert clip_to_listing_date(records, listing_date=None) == records


def test_clip_all_to_listing_dates_applies_per_symbol():
    records = [
        _record("PRELISTED", date(2015, 5, 30)),
        _record("PRELISTED", date(2021, 5, 30)),
        _record("NORMAL", date(2015, 5, 30)),  # no listing date on file - unclipped
    ]
    clipped = clip_all_to_listing_dates(records, {"PRELISTED": date(2020, 6, 1)})
    remaining = [(r.symbol, r.as_of_date) for r in clipped]
    assert remaining == [("PRELISTED", date(2021, 5, 30)), ("NORMAL", date(2015, 5, 30))]


# --- run_rebalance_walk -------------------------------------------------


def _rank_by_fixed_score(scores: dict[str, float]):
    """A trivial synthetic rank_fn: rank descending by a fixed score dict,
    only including symbols present in `eligible` at each call."""

    def rank_fn(eligible, rebalance_date):
        present = [r for r in eligible if r.symbol in scores]
        ordered = sorted(present, key=lambda r: -scores[r.symbol])
        return [
            RankedStock(
                symbol=r.symbol, cap_bucket=None, roce=0.0, ey=0.0,
                rank_roce=i + 1, rank_ey=i + 1, combined_rank=(i + 1) * 2,
                position=i + 1,
            )
            for i, r in enumerate(ordered)
        ]

    return rank_fn


def test_run_rebalance_walk_applies_embargo_and_carries_holdings_across_dates():
    # A and C are old enough to be eligible at both rebalances. B's
    # as_of_date (2020-09-01) is after the first rebalance date but
    # before the second - by hand, no extra lag subtracted beyond
    # as_of_date itself (decision 0011):
    #   date1 (2020-01-01): A,C eligible (as_of 2019-10-01); B excluded (as_of 2020-09-01 is later).
    #   date2 (2021-01-01): A,B,C all eligible (B's as_of_date has arrived by now).
    all_fundamentals = [
        _record("A", date(2019, 10, 1)),
        _record("B", date(2020, 9, 1)),
        _record("C", date(2019, 10, 1)),
    ]
    scores = {"A": 10, "B": 20, "C": 5}
    rank_fn = _rank_by_fixed_score(scores)

    steps = run_rebalance_walk(
        rebalance_dates=[date(2020, 1, 1), date(2021, 1, 1)],
        all_fundamentals=all_fundamentals,
        rank_fn=rank_fn,
        top_n=1,
        buffer_multiplier=1.0,
    )

    assert len(steps) == 2

    step1 = steps[0]
    assert step1.rebalance_date == date(2020, 1, 1)
    assert step1.eligible_universe_size == 2  # B excluded by the embargo
    assert step1.outcome.holdings == {"A"}  # A(10) beats C(5), B not eligible yet

    step2 = steps[1]
    assert step2.rebalance_date == date(2021, 1, 1)
    assert step2.eligible_universe_size == 3  # B now eligible
    # B(20) now outranks A(10); with buffer_multiplier=1.0 (no bulge
    # tolerance) A falls out of the top 1 and is sold, B is bought.
    assert step2.outcome.holdings == {"B"}
    assert step2.outcome.buys == {"B"}
    assert step2.outcome.sells == {"A"}


def test_run_rebalance_walk_sorts_out_of_order_rebalance_dates():
    all_fundamentals = [_record("A", date(2019, 1, 1))]
    rank_fn = _rank_by_fixed_score({"A": 1})

    steps = run_rebalance_walk(
        rebalance_dates=[date(2021, 1, 1), date(2020, 1, 1)],  # deliberately reversed
        all_fundamentals=all_fundamentals,
        rank_fn=rank_fn,
        top_n=1,
    )

    assert [s.rebalance_date for s in steps] == [date(2020, 1, 1), date(2021, 1, 1)]


def test_run_rebalance_walk_excludes_prelisting_history_from_an_early_rebalance():
    # PRELISTED's only record as of the 2016 rebalance would otherwise
    # pass the plain point-in-time embargo (its as_of_date, 2015-05-30, is
    # well before the 2016-01-01 rebalance) - exactly the 3B Blackbio-
    # style bug: real historical fundamentals, but the company wasn't
    # listed (buyable) until 2020-06-01, four years after this rebalance
    # date. listing_date_by_symbol must prevent it from ever reaching
    # rank_fn for that rebalance.
    seen_at_2016 = []

    def recording_rank_fn(eligible, rebalance_date):
        if rebalance_date == date(2016, 1, 1):
            seen_at_2016.extend(r.symbol for r in eligible)
        return _rank_by_fixed_score({"PRELISTED": 10, "NORMAL": 5})(eligible, rebalance_date)

    all_fundamentals = [
        _record("PRELISTED", date(2015, 5, 30)),  # pre-listing history
        _record("PRELISTED", date(2021, 5, 30)),  # post-listing history
        _record("NORMAL", date(2015, 5, 30)),  # genuinely listed since well before 2016
    ]

    steps = run_rebalance_walk(
        rebalance_dates=[date(2016, 1, 1), date(2022, 1, 1)],
        all_fundamentals=all_fundamentals,
        rank_fn=recording_rank_fn,
        top_n=2,
        buffer_multiplier=1.0,
        listing_date_by_symbol={"PRELISTED": date(2020, 6, 1)},
    )

    # PRELISTED must not even be visible to rank_fn at the 2016 rebalance.
    assert seen_at_2016 == ["NORMAL"]
    assert steps[0].outcome.holdings == {"NORMAL"}
    # By 2022, PRELISTED's post-listing record is both listed and past the
    # embargo, so it correctly appears (and, scoring higher, gets bought).
    assert steps[1].outcome.holdings == {"PRELISTED", "NORMAL"}


# --- Decision 0011: the double-lag regression, on real TCS data ----------
#
# The concrete, checkable version of "the embargo lag is single, not
# double". Real TCS consolidated figures (screener.in, FY Mar 2025 and
# FY Mar 2026 - the same source and two of the same fiscal years already
# used in tests/fixtures/real_companies.py). as_of_date is left to
# __post_init__'s real computation (fiscal_year_end + 60), not
# hand-overridden like _record() above - this test exercises the actual
# production embargo math end to end, not a synthetic stand-in for it.


def test_tcs_2026_06_01_rebalance_uses_fy2026_not_fy2025():
    # FY2026 fiscal year end 2026-03-31 -> as_of_date 2026-05-30, which
    # is before the 2026-06-01 rebalance date - genuinely public by then
    # under the single 60-day lag SPEC.md section 6 asks for. Under the
    # bug this regression guards against (a second, independent 60-day
    # buffer previously subtracted again in filter_point_in_time), the
    # effective cutoff would have been 2026-04-02, wrongly excluding
    # FY2026 and leaving the stale FY2025 record as "latest eligible".
    fy2025 = FundamentalsRecord(
        symbol="TCS", sector="Information Technology", broad_sector="Information Technology",
        fiscal_year_end=date(2025, 3, 31), operating_profit=67_407, depreciation_amortization=5_242,
        total_assets=158_649, other_liabilities=54_501, investments=30_964, cwip=1_546,
        borrowings=9_392, market_cap=833_607, source_url="https://example.invalid",
    )
    fy2026 = FundamentalsRecord(
        symbol="TCS", sector="Information Technology", broad_sector="Information Technology",
        fiscal_year_end=date(2026, 3, 31), operating_profit=72_398, depreciation_amortization=5_560,
        total_assets=181_167, other_liabilities=62_644, investments=33_988, cwip=2_665,
        borrowings=11_283, market_cap=833_607, source_url="https://example.invalid",
    )
    assert fy2025.as_of_date == date(2025, 5, 30)
    assert fy2026.as_of_date == date(2026, 5, 30)

    seen_at_rebalance = []

    def recording_rank_fn(eligible, rebalance_date):
        seen_at_rebalance.extend(eligible)
        return []

    run_rebalance_walk(
        rebalance_dates=[date(2026, 6, 1)],
        all_fundamentals=[fy2025, fy2026],
        rank_fn=recording_rank_fn,
        top_n=1,
    )

    fiscal_years_seen = {r.fiscal_year_end for r in seen_at_rebalance}
    assert date(2026, 3, 31) in fiscal_years_seen, (
        "FY2026 was wrongly excluded from the 2026-06-01 rebalance - the "
        "double-lag bug decision 0011 fixed has been reintroduced"
    )
    latest_seen = max(seen_at_rebalance, key=lambda r: r.fiscal_year_end)
    assert latest_seen.fiscal_year_end == date(2026, 3, 31)
    assert latest_seen.operating_profit == 72_398  # the real FY2026 figure, not FY2025's


# --- compute_turnover ---------------------------------------------------


def _outcome(holdings, buys, sells) -> RebalanceOutcome:
    weight = 1 / len(holdings) if holdings else 0.0
    return RebalanceOutcome(
        weights={s: weight for s in holdings}, buys=set(buys), sells=set(sells), holdings=set(holdings)
    )


def test_compute_turnover_fresh_basket_from_nothing_is_full_turnover():
    outcome = _outcome(holdings={"A", "B"}, buys={"A", "B"}, sells=set())
    assert compute_turnover(previous_holdings=set(), outcome=outcome) == 1.0


def test_compute_turnover_partial_change():
    # previous {A,B,C} (3) -> new {A,B,D} (3): D bought, C sold, A/B held.
    # (1 buy + 1 sell) / (3 previous + 3 new) = 2/6 = 1/3.
    outcome = _outcome(holdings={"A", "B", "D"}, buys={"D"}, sells={"C"})
    turnover = compute_turnover(previous_holdings={"A", "B", "C"}, outcome=outcome)
    assert turnover == pytest.approx(1 / 3)


def test_compute_turnover_nothing_to_nothing_is_zero():
    outcome = _outcome(holdings=set(), buys=set(), sells=set())
    assert compute_turnover(previous_holdings=set(), outcome=outcome) == 0.0


# --- compute_period_return -----------------------------------------------


def test_compute_period_return_equal_weight_average_with_no_turnover_cost():
    # A: 100->110 (+10%), B: 50->55 (+10%) - equal-weighted return = 10%.
    prices = {("A", date(2020, 1, 1)): 100, ("A", date(2020, 2, 1)): 110,
              ("B", date(2020, 1, 1)): 50, ("B", date(2020, 2, 1)): 55}
    price_lookup = lambda symbol, d: prices.get((symbol, d))

    r = compute_period_return(
        weights={"A": 0.5, "B": 0.5}, price_lookup=price_lookup,
        period_start=date(2020, 1, 1), period_end=date(2020, 2, 1),
        turnover=0.0, transaction_cost_bps=25.0,
    )
    assert r == pytest.approx(0.10)


def test_compute_period_return_applies_transaction_cost_to_turnover():
    prices = {("A", date(2020, 1, 1)): 100, ("A", date(2020, 2, 1)): 110}
    price_lookup = lambda symbol, d: prices.get((symbol, d))

    r = compute_period_return(
        weights={"A": 1.0}, price_lookup=price_lookup,
        period_start=date(2020, 1, 1), period_end=date(2020, 2, 1),
        turnover=1.0, transaction_cost_bps=25.0,
    )
    # 10% gross, minus 25bps * full turnover = 0.10 - 0.0025 = 0.0975
    assert r == pytest.approx(0.0975)


def test_compute_period_return_excludes_symbol_with_missing_price():
    prices = {("A", date(2020, 1, 1)): 100, ("A", date(2020, 2, 1)): 110}
    price_lookup = lambda symbol, d: prices.get((symbol, d))  # B has no prices at all

    r = compute_period_return(
        weights={"A": 0.5, "B": 0.5}, price_lookup=price_lookup,
        period_start=date(2020, 1, 1), period_end=date(2020, 2, 1),
        turnover=0.0,
    )
    # Only A's weighted contribution counts; B silently contributes 0.
    assert r == pytest.approx(0.5 * 0.10)


# --- Performance stats (hand-computed) ------------------------------------


def test_compute_equity_curve():
    curve = compute_equity_curve([0.20, -0.10, 0.05], starting_value=1.0)
    assert curve == pytest.approx([1.0, 1.2, 1.08, 1.134])


def test_compute_cagr_constant_return_matches_the_period_return():
    # 10%/period for 2 periods compounds to a 10% CAGR trivially.
    assert compute_cagr([0.10, 0.10], periods_per_year=1.0) == pytest.approx(0.10)


def test_compute_cagr_empty_is_zero():
    assert compute_cagr([]) == 0.0


def test_compute_annualized_volatility_hand_computed():
    # [0.20, 0.00]: mean 0.10, pstdev = sqrt(((0.10)^2+(0.10)^2)/2) = 0.10
    assert compute_annualized_volatility([0.20, 0.00], periods_per_year=1.0) == pytest.approx(0.10)


def test_compute_sharpe_ratio_hand_computed():
    # risk_free=0: excess = [0.20, 0.00], mean 0.10, vol 0.10 -> Sharpe = 1.0
    sharpe = compute_sharpe_ratio([0.20, 0.00], risk_free_rate=0.0, periods_per_year=1.0)
    assert sharpe == pytest.approx(1.0)


def test_compute_sharpe_ratio_zero_volatility_is_zero_not_a_division_error():
    assert compute_sharpe_ratio([0.05, 0.05], risk_free_rate=0.0) == 0.0


def test_compute_max_drawdown_hand_computed():
    # equity curve [1.0, 1.2, 1.08, 1.134]: peak 1.2 after the second
    # point; trough 1.08 -> (1.08-1.2)/1.2 = -0.10 is the worst drawdown.
    curve = compute_equity_curve([0.20, -0.10, 0.05])
    assert compute_max_drawdown(curve) == pytest.approx(-0.10)


def test_compute_hit_rate_hand_computed():
    strategy = [0.10, 0.05, -0.02]
    benchmark = [0.08, 0.06, -0.03]
    # wins: 0.10>0.08 True, 0.05>0.06 False, -0.02>-0.03 True -> 2/3
    assert compute_hit_rate(strategy, benchmark) == pytest.approx(2 / 3)


def test_compute_hit_rate_mismatched_lengths_raises():
    with pytest.raises(ValueError):
        compute_hit_rate([0.1], [0.1, 0.2])


def test_compute_rolling_excess_return_hand_computed():
    strategy = [0.05, 0.05, 0.05, 0.05]
    benchmark = [0.02, 0.02, 0.02, 0.02]
    # constant excess of 0.03/period; 2-period rolling sum is 0.06 throughout.
    rolling = compute_rolling_excess_return(strategy, benchmark, window=2)
    assert rolling == pytest.approx([0.06, 0.06, 0.06])


# --- run_backtest orchestration + CSV output ------------------------------


def test_run_backtest_end_to_end_synthetic():
    all_fundamentals = [_record("A", date(2019, 1, 1)), _record("B", date(2019, 1, 1))]
    rank_fn = _rank_by_fixed_score({"A": 10, "B": 5})
    rebalance_dates = [date(2020, 1, 1), date(2021, 1, 1), date(2022, 1, 1)]

    prices = {
        ("A", date(2020, 1, 1)): 100, ("A", date(2021, 1, 1)): 110, ("A", date(2022, 1, 1)): 121,
    }
    price_lookup = lambda symbol, d: prices.get((symbol, d))

    result = run_backtest(
        rebalance_dates=rebalance_dates,
        all_fundamentals=all_fundamentals,
        rank_fn=rank_fn,
        price_lookup=price_lookup,
        benchmark_returns=[0.05, 0.05],  # one per closed period (3 dates -> 2 periods)
        top_n=1,
        buffer_multiplier=1.0,
        risk_free_rate=0.0,
    )

    assert len(result.steps) == 3
    assert result.stats.periods == 2
    # A(score10) always wins the top-1 slot over B(score5); its price
    # compounds 100->110->121, a steady 10%/period, no turnover after the
    # first period (already holding A) so no transaction-cost drag there.
    assert result.period_returns[0] == pytest.approx(0.10 - 1.0 * 0.0025)  # first period: fresh basket, full turnover
    assert result.period_returns[1] == pytest.approx(0.10)  # second period: A already held, zero turnover
    assert result.stats.hit_rate == 1.0  # beat the 5% benchmark both periods
    assert result.equity_curve[0] == 1.0
    assert len(result.equity_curve) == 3  # starting value + 2 periods


def test_run_backtest_rejects_mismatched_benchmark_length():
    all_fundamentals = [_record("A", date(2019, 1, 1))]
    rank_fn = _rank_by_fixed_score({"A": 1})

    with pytest.raises(ValueError):
        run_backtest(
            rebalance_dates=[date(2020, 1, 1), date(2021, 1, 1)],
            all_fundamentals=all_fundamentals,
            rank_fn=rank_fn,
            price_lookup=lambda s, d: 100.0,
            benchmark_returns=[0.05, 0.05],  # should be exactly 1 (2 dates -> 1 period)
            top_n=1,
        )


def test_write_basket_composition_csv(tmp_path):
    steps = run_rebalance_walk(
        rebalance_dates=[date(2020, 1, 1)],
        all_fundamentals=[_record("A", date(2019, 1, 1)), _record("B", date(2019, 1, 1))],
        rank_fn=_rank_by_fixed_score({"A": 10, "B": 5}),
        top_n=2,
    )
    path = tmp_path / "basket_history.csv"
    write_basket_composition_csv(steps, path)

    lines = path.read_text(encoding="utf-8").strip().splitlines()
    assert lines[0] == "rebalance_date,symbol,weight"
    assert lines[1] == "2020-01-01,A,0.5"
    assert lines[2] == "2020-01-01,B,0.5"
