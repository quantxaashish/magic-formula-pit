"""Unit tests for magicformula.formulas (SPEC.md sections 3 and 12).

Three layers, in order:
  1. Pure formula functions against hand-computed numbers.
  2. compute_metrics()'s edge-case handling (non-positive EBIT/EV, strict-mode
     fallback), built from synthetic inputs so the correct answer is known
     by construction.
  3. Real-company sanity checks against fixtures/real_companies.py, hand
     read off screener.in for five large-caps (see that file's docstring
     for the documented approximation and tolerance).
"""

from __future__ import annotations

import logging

import pytest

from magicformula.formulas import (
    EXCLUDE_NON_POSITIVE_CAPITAL_EMPLOYED,
    EXCLUDE_NON_POSITIVE_EBIT,
    EXCLUDE_NON_POSITIVE_EV,
    FinancialInputs,
    capital_employed_standard,
    compute_metrics,
    earnings_yield,
    ebit,
    enterprise_value,
    net_fixed_assets,
    net_working_capital,
    roc_strict,
    roce_standard,
)
from .fixtures.real_companies import REAL_COMPANY_FIXTURES


# --- 1. Pure formula functions -------------------------------------------


def test_ebit():
    assert ebit(operating_profit=1000, depreciation_amortization=200) == 800


def test_capital_employed_standard():
    assert capital_employed_standard(total_assets=5000, current_liabilities=1500) == 3500


def test_roce_standard():
    assert roce_standard(ebit_value=800, capital_employed=3500) == pytest.approx(800 / 3500)


def test_net_working_capital():
    assert net_working_capital(current_assets=2000, current_liabilities=1500) == 500


def test_net_fixed_assets():
    assert net_fixed_assets(net_ppe=1800, capital_work_in_progress=200) == 2000


def test_roc_strict():
    assert roc_strict(ebit_value=800, nwc=500, nfa=2000) == pytest.approx(800 / 2500)


def test_enterprise_value_with_other_liquid_investments():
    ev = enterprise_value(
        market_cap=10_000,
        total_debt=1_000,
        cash_and_equivalents=300,
        other_liquid_investments=200,
    )
    assert ev == 10_500


def test_enterprise_value_defaults_other_liquid_investments_to_zero():
    ev = enterprise_value(market_cap=10_000, total_debt=1_000, cash_and_equivalents=300)
    assert ev == 10_700


def test_earnings_yield():
    assert earnings_yield(ebit_value=800, ev=10_500) == pytest.approx(800 / 10_500)


# --- 2. compute_metrics edge cases (synthetic, exact by construction) ----


def _base_inputs(**overrides) -> FinancialInputs:
    defaults = dict(
        symbol="TEST",
        operating_profit=1000,
        depreciation_amortization=200,
        total_assets=5000,
        current_liabilities=1500,
        market_cap=10_000,
        total_debt=1_000,
        cash_and_equivalents=300,
        other_liquid_investments=200,
    )
    defaults.update(overrides)
    return FinancialInputs(**defaults)


def test_compute_metrics_standard_mode():
    result = compute_metrics(_base_inputs(), roce_mode="standard")
    assert result.excluded is False
    assert result.roce_mode_used == "standard"
    assert result.ebit == 800
    assert result.capital_employed == 3500
    assert result.roce == pytest.approx(800 / 3500)
    assert result.ev == 10_500
    assert result.ey == pytest.approx(800 / 10_500)


def test_compute_metrics_strict_greenblatt_mode():
    inputs = _base_inputs(current_assets=2000, net_ppe=1800, capital_work_in_progress=200)
    result = compute_metrics(inputs, roce_mode="strict_greenblatt")
    assert result.excluded is False
    assert result.roce_mode_used == "strict_greenblatt"
    assert result.capital_employed == 2500  # NWC 500 + NFA 2000
    assert result.roce == pytest.approx(800 / 2500)


@pytest.mark.parametrize("operating_profit,depreciation", [(200, 200), (100, 300)])
def test_compute_metrics_excludes_non_positive_ebit(operating_profit, depreciation):
    inputs = _base_inputs(
        operating_profit=operating_profit, depreciation_amortization=depreciation
    )
    result = compute_metrics(inputs)
    assert result.excluded is True
    assert result.exclusion_reason == EXCLUDE_NON_POSITIVE_EBIT
    assert result.ey != result.ey  # NaN


def test_compute_metrics_excludes_non_positive_ev():
    # Positive EBIT, but cash swamps market cap + debt.
    inputs = _base_inputs(market_cap=1_000, total_debt=0, cash_and_equivalents=1_500)
    result = compute_metrics(inputs)
    assert result.excluded is True
    assert result.exclusion_reason == EXCLUDE_NON_POSITIVE_EV
    assert result.ebit == 800  # still populated
    assert result.roce == pytest.approx(800 / 3500)  # still populated
    assert result.ey != result.ey  # NaN


def test_compute_metrics_excludes_capital_employed_exactly_zero():
    # total_assets == current_liabilities: standard-mode capital employed
    # is exactly 0. Before this exclusion existed, this crashed a real
    # backtest run with ZeroDivisionError inside roce_standard - this
    # confirms it's now excluded, not division-by-zero.
    inputs = _base_inputs(total_assets=1500, current_liabilities=1500)
    result = compute_metrics(inputs)
    assert result.excluded is True
    assert result.exclusion_reason == EXCLUDE_NON_POSITIVE_CAPITAL_EMPLOYED
    assert result.capital_employed == 0
    assert result.roce != result.roce  # NaN, not a ZeroDivisionError


def test_compute_metrics_excludes_capital_employed_negative():
    inputs = _base_inputs(total_assets=1000, current_liabilities=1500)
    result = compute_metrics(inputs)
    assert result.excluded is True
    assert result.exclusion_reason == EXCLUDE_NON_POSITIVE_CAPITAL_EMPLOYED
    assert result.capital_employed == -500


def test_essentia_real_zero_capital_employed_excluded_not_crashed():
    # Real screener.in consolidated figures, FY Mar 2022: Total Assets
    # and Other Liabilities are both exactly 17 Cr - the actual case that
    # crashed a live small-cap-inclusive backtest run with
    # ZeroDivisionError before this exclusion was added.
    inputs = FinancialInputs(
        symbol="ESSENTIA", operating_profit=1.0, depreciation_amortization=0.0,
        total_assets=17.0, current_liabilities=17.0, market_cap=195.0,
        total_debt=29.0, cash_and_equivalents=0.0, other_liquid_investments=0.0,
    )
    result = compute_metrics(inputs)
    assert result.excluded is True
    assert result.exclusion_reason == EXCLUDE_NON_POSITIVE_CAPITAL_EMPLOYED


def test_gtl_real_deeply_negative_capital_employed_excluded():
    # Real screener.in consolidated figures, FY Mar 2021: Other
    # Liabilities (7,434 Cr) massively exceed Total Assets (197 Cr) - a
    # genuine reflection of severe financial distress (GTL Infrastructure
    # was in insolvency proceedings around this period), not a data
    # error. Capital employed is -7,237 Cr; there is no meaningful ROCE
    # to compute here, the same way a negative EBIT or EV isn't ranked.
    inputs = FinancialInputs(
        symbol="GTL", operating_profit=58.0, depreciation_amortization=5.0,
        total_assets=197.0, current_liabilities=7434.0, market_cap=113.0,
        total_debt=194.0, cash_and_equivalents=0.0, other_liquid_investments=51.0,
    )
    result = compute_metrics(inputs)
    assert result.excluded is True
    assert result.exclusion_reason == EXCLUDE_NON_POSITIVE_CAPITAL_EMPLOYED
    assert result.capital_employed == pytest.approx(-7237.0)


def test_compute_metrics_strict_mode_falls_back_when_fields_missing(caplog):
    inputs = _base_inputs()  # current_assets/net_ppe/cwip left as None
    with caplog.at_level(logging.WARNING):
        result = compute_metrics(inputs, roce_mode="strict_greenblatt")
    assert result.roce_mode_used == "standard"
    assert result.roce == pytest.approx(800 / 3500)
    assert any("falling back to standard mode" in record.message for record in caplog.records)


# --- 3. Real-company sanity checks (SPEC.md section 12) ------------------


@pytest.mark.parametrize(
    "fixture", REAL_COMPANY_FIXTURES, ids=lambda f: f["inputs"].symbol
)
def test_real_company_ebit_matches_reported_figures(fixture):
    inputs = fixture["inputs"]
    result = compute_metrics(inputs)
    expected_ebit = inputs.operating_profit - inputs.depreciation_amortization
    assert result.ebit == expected_ebit
    assert result.excluded is False


@pytest.mark.parametrize(
    "fixture", REAL_COMPANY_FIXTURES, ids=lambda f: f["inputs"].symbol
)
def test_real_company_roce_within_documented_tolerance_of_published(fixture):
    result = compute_metrics(fixture["inputs"])
    computed_roce_pct = result.roce * 100
    diff = abs(computed_roce_pct - fixture["published_roce_pct"])
    assert diff <= fixture["roce_tolerance_pct"], (
        f"{fixture['inputs'].symbol}: computed ROCE {computed_roce_pct:.2f}% vs "
        f"published {fixture['published_roce_pct']}% (source: {fixture['source']})"
    )


@pytest.mark.parametrize(
    "fixture", REAL_COMPANY_FIXTURES, ids=lambda f: f["inputs"].symbol
)
def test_real_company_earnings_yield_is_in_sane_range(fixture):
    result = compute_metrics(fixture["inputs"])
    assert 0 < result.ey < 0.30


def test_real_company_earnings_yield_ordering_matches_relative_valuation():
    # TCS/INFY/ITC trade at moderate P/E (~15-17x); Asian Paints/Nestle trade
    # at premium P/E (~50-73x). A market-based yield should reflect that.
    by_symbol = {
        f["inputs"].symbol: compute_metrics(f["inputs"]).ey for f in REAL_COMPANY_FIXTURES
    }
    moderate_pe_names = ["TCS", "INFY", "ITC"]
    premium_pe_names = ["ASIANPAINT", "NESTLEIND"]
    assert min(by_symbol[s] for s in moderate_pe_names) > max(
        by_symbol[s] for s in premium_pe_names
    )
