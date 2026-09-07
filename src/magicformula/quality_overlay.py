"""Phase 2 quality overlay (SPEC.md section 8): Piotroski F-score, an
adapted Altman Z-score, a Sloan accrual-quality flag, a promoter-pledge
threshold, and a price-momentum tilt - the direct answer to "more refined
stocks" in the original ask, layered on top of the plain two-factor
(ROCE/EY) ranking without changing it.

Every signal here is independently toggleable via QualityOverlayConfig,
and the plain two-factor baseline (magicformula.ranker.rank_universe
alone, nothing in this module called) remains runnable completely
unmodified - apply_quality_overlay with every toggle off returns the
input ranking's order and membership unchanged (see
test_apply_quality_overlay_with_everything_disabled_is_a_pure_passthrough).

Two signals are *adapted*, not textbook, because of a real, checked data
constraint: screener.in's free tier (the only fundamentals source this
project uses) does not split current vs. non-current assets/liabilities,
so there is no real Working Capital figure available.
  - F-score: 7 of the 9 standard Piotroski tests (drops "delta Current
    Ratio" and "delta Gross Margin" - both need line items this data
    source doesn't expose; see compute_f_score's docstring for exactly
    which 7 and why).
  - Z-score: the original 1968 Altman formula's 4 of 5 terms that don't
    need Working Capital (drops the 1.2 x WC/TA term entirely, not a
    zero-filled proxy for it - see compute_z_score's docstring). Zone
    cutoffs are relabeled as approximate for the same reason: they were
    calibrated for the full 5-term formula.

Accrual flag, promoter-pledge flag, and momentum use real, unadapted
definitions - the data behind them (net profit, CFO, total assets,
promoter-pledge disclosure, price history) is available without any
proxy.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date
from typing import Literal

from magicformula.data_fetch.prices import nearest_value_lookup
from magicformula.ranker import RankedStock

ZScoreZone = Literal["distress", "grey", "safe"]


@dataclass
class QualityInputs:
    """One company's quality-overlay inputs for one fiscal year, plus
    the prior year's figures needed for the F-score's delta tests. All
    of the prior_* fields are optional - a company's earliest available
    fiscal year (or one where the prior year's optional fields failed to
    parse) legitimately has no prior year to compare against, and every
    delta-based test degrades to "cannot evaluate" (None), not a silent
    wrong answer, when that happens.
    """

    symbol: str
    net_profit: float | None
    cash_from_operations: float | None
    total_assets: float
    borrowings: float
    other_liabilities: float  # current-liability proxy, decision 0001
    equity_capital: float | None
    reserves: float | None
    sales: float | None
    ebit: float
    market_cap: float
    promoter_pledge_percentage: float | None

    prior_net_profit: float | None = None
    prior_total_assets: float | None = None
    prior_borrowings: float | None = None
    prior_equity_capital: float | None = None
    prior_sales: float | None = None


# --- Piotroski F-score (adapted, 7 of 9 tests) -----------------------------


@dataclass
class FScoreResult:
    score: int
    max_score: int  # <= 7; lower than 7 only when some tests couldn't be evaluated
    components: dict[str, bool | None]  # None = insufficient data to evaluate this test


def compute_f_score(inputs: QualityInputs) -> FScoreResult:
    """7 of the 9 standard Piotroski (2000) tests - profitability (4),
    leverage/financing (2, of the standard 3), operating efficiency (1,
    of the standard 2).

    Dropped, both because screener.in's free tier has no current-assets/
    current-liabilities split to compute them correctly:
      - "Delta Current Ratio" (needs Current Assets, not just the
        Other-Liabilities current-liability proxy this project already
        uses elsewhere - decision 0001 - a *ratio* needs both sides to be
        real, not one real side and one proxy).
      - "Delta Gross Margin" (needs Cost of Goods Sold; screener.in shows
        only a lump "Expenses" figure, not a COGS/opex split).

    The 7 kept, each independently None (not evaluable) when its inputs
    are missing rather than silently skipped or counted as a fail:
      1. ROA > 0 (net_profit / total_assets)
      2. CFO > 0
      3. CFO > net_profit (earnings-quality / accrual test)
      4. Delta ROA > 0 (this year's ROA better than last year's)
      5. Delta Leverage improved (borrowings/total_assets decreased)
      6. No new shares issued (equity_capital did not increase)
      7. Delta Asset Turnover improved (sales/total_assets increased)

    `max_score` is the count of tests actually evaluated (out of 7), so
    `score/max_score` stays a fair fraction when prior-year data is
    missing - a company with only 4 evaluable tests and 3 passes scores
    3/4, not penalized to 3/7 for data it never had.
    """
    components: dict[str, bool | None] = {}

    roa = inputs.net_profit / inputs.total_assets if inputs.net_profit is not None and inputs.total_assets else None
    components["roa_positive"] = roa > 0 if roa is not None else None

    cfo = inputs.cash_from_operations
    components["cfo_positive"] = cfo > 0 if cfo is not None else None

    components["cfo_exceeds_net_profit"] = (
        cfo > inputs.net_profit if cfo is not None and inputs.net_profit is not None else None
    )

    prior_roa = (
        inputs.prior_net_profit / inputs.prior_total_assets
        if inputs.prior_net_profit is not None and inputs.prior_total_assets else None
    )
    components["roa_improved"] = roa > prior_roa if roa is not None and prior_roa is not None else None

    leverage = inputs.borrowings / inputs.total_assets if inputs.total_assets else None
    prior_leverage = (
        inputs.prior_borrowings / inputs.prior_total_assets
        if inputs.prior_borrowings is not None and inputs.prior_total_assets else None
    )
    components["leverage_improved"] = (
        leverage < prior_leverage if leverage is not None and prior_leverage is not None else None
    )

    components["no_new_shares_issued"] = (
        inputs.equity_capital <= inputs.prior_equity_capital
        if inputs.equity_capital is not None and inputs.prior_equity_capital is not None else None
    )

    turnover = inputs.sales / inputs.total_assets if inputs.sales is not None and inputs.total_assets else None
    prior_turnover = (
        inputs.prior_sales / inputs.prior_total_assets
        if inputs.prior_sales is not None and inputs.prior_total_assets else None
    )
    components["turnover_improved"] = (
        turnover > prior_turnover if turnover is not None and prior_turnover is not None else None
    )

    evaluated = {k: v for k, v in components.items() if v is not None}
    return FScoreResult(
        score=sum(1 for v in evaluated.values() if v),
        max_score=len(evaluated),
        components=components,
    )


# --- Altman Z-score (adapted, 4 of 5 terms) --------------------------------


@dataclass
class ZScoreResult:
    score: float | None  # None if total_assets or total_liabilities is 0 (undefined)
    zone: ZScoreZone | None


def compute_z_score(inputs: QualityInputs) -> ZScoreResult:
    """Original 1968 Altman Z-score, 4 of its 5 terms - the 1.2 x
    (Working Capital / Total Assets) term is dropped entirely (not
    zero-filled), because this project has no real Working Capital
    figure (see module docstring). Retained Earnings is approximated by
    Reserves (screener.in's own label, close enough for most companies
    absent large revaluation/other-reserve components - not exact).

    Z = 1.4*(Reserves/TA) + 3.3*(EBIT/TA) + 0.6*(MarketCap/TotalLiabilities) + 1.0*(Sales/TA)

    Zone cutoffs (distress < 1.8, grey 1.8-3.0, safe > 3.0) are this
    project's own approximate thresholds, not a reproduction of Altman's
    original 1.81/2.99 cutoffs - those were calibrated for the full
    5-term formula, and dropping a term shifts the real distribution.
    Treat the zone as a directional read, not a precise distress
    probability.

    Returns score=None (not a divide-by-zero) when total_assets or
    total_liabilities is 0 - matches this project's convention elsewhere
    (formulas.py excludes non-positive denominators rather than crashing
    or returning misleading infinities).
    """
    total_liabilities = inputs.borrowings + inputs.other_liabilities
    if not inputs.total_assets or not total_liabilities or inputs.reserves is None or inputs.sales is None:
        return ZScoreResult(score=None, zone=None)

    z = (
        1.4 * (inputs.reserves / inputs.total_assets)
        + 3.3 * (inputs.ebit / inputs.total_assets)
        + 0.6 * (inputs.market_cap / total_liabilities)
        + 1.0 * (inputs.sales / inputs.total_assets)
    )
    if z < 1.8:
        zone: ZScoreZone = "distress"
    elif z < 3.0:
        zone = "grey"
    else:
        zone = "safe"
    return ZScoreResult(score=z, zone=zone)


# --- Accrual flag (Sloan ratio) --------------------------------------------


def compute_accrual_flag(inputs: QualityInputs, threshold: float = 0.10) -> bool | None:
    """Sloan (1996) accrual ratio: (Net Profit - CFO) / Total Assets.
    Flagged when accruals exceed `threshold` (default 10% of assets) -
    a large gap between reported profit and actual cash generated is a
    real, well-documented earnings-quality red flag, independent of and
    complementary to the F-score's binary CFO > Net Profit test (this is
    a magnitude threshold, not just a sign check).

    Returns None (not False) when net_profit or cash_from_operations is
    missing - "not flagged" and "couldn't check" must stay distinguishable
    to a caller deciding whether to trust a clean result.
    """
    if inputs.net_profit is None or inputs.cash_from_operations is None or not inputs.total_assets:
        return None
    accrual_ratio = (inputs.net_profit - inputs.cash_from_operations) / inputs.total_assets
    return accrual_ratio > threshold


# --- Promoter-pledge threshold ---------------------------------------------


def compute_promoter_pledge_flag(inputs: QualityInputs, threshold: float = 20.0) -> bool:
    """Flagged when disclosed promoter pledge exceeds `threshold` percent
    (default 20%). Unlike the other flags, this deliberately returns a
    plain bool, not Optional: promoter_pledge_percentage is None when
    screener.in's "Insights" bullet isn't present at all, which happens
    for the large majority of companies with no material pledge - keeping
    a three-way None/True/False here would flag "no evidence of a
    problem" as unusably ambiguous when it's actually the common, benign
    case. This is documented as "no red flag disclosed", not "confirmed
    zero pledge" (see FundamentalsRecord.promoter_pledge_percentage).
    """
    if inputs.promoter_pledge_percentage is None:
        return False
    return inputs.promoter_pledge_percentage > threshold


# --- Momentum ---------------------------------------------------------------


def compute_momentum(
    price_series: dict[date, float],
    as_of: date,
    lookback_days: int = 180,
    max_days_tolerance: int = 14,
) -> float | None:
    """Trailing price return over `lookback_days` as of `as_of` -
    (price_now / price_then) - 1. Reuses nearest_value_lookup for both
    endpoints, so this never uses a future price (the same point-in-time
    discipline as every other price lookup in this project) and returns
    None rather than a stale-value return when either endpoint is more
    than `max_days_tolerance` away from its target date.
    """
    price_now = nearest_value_lookup(price_series, as_of, max_days_tolerance=max_days_tolerance)
    then = date.fromordinal(as_of.toordinal() - lookback_days)
    price_then = nearest_value_lookup(price_series, then, max_days_tolerance=max_days_tolerance)
    if price_now is None or price_then is None or price_then <= 0:
        return None
    return price_now / price_then - 1


# --- Overlay orchestration --------------------------------------------------


@dataclass
class QualityOverlayConfig:
    """Every signal defaults to disabled - apply_quality_overlay with the
    default config is a pure passthrough, so the plain two-factor
    baseline stays runnable unmodified (SPEC.md section 8's explicit
    requirement, checked by
    test_apply_quality_overlay_with_everything_disabled_is_a_pure_passthrough,
    not just assumed from the code shape).
    """

    enable_f_score_filter: bool = False
    min_f_score_fraction: float = 0.0  # score/max_score must be >= this to survive

    enable_z_score_filter: bool = False
    exclude_z_score_distress: bool = False

    enable_accrual_filter: bool = False
    accrual_threshold: float = 0.10

    enable_promoter_pledge_filter: bool = False
    promoter_pledge_threshold: float = 20.0

    enable_momentum_tilt: bool = False
    momentum_lookback_days: int = 180
    momentum_weight: float = 0.0  # 0 = compute and report only, don't reorder


@dataclass
class QualityAdjustedStock:
    ranked: RankedStock
    f_score: FScoreResult | None
    z_score: ZScoreResult | None
    accrual_flagged: bool | None
    promoter_pledge_flagged: bool
    momentum: float | None
    excluded_by_overlay: bool
    exclusion_reason: str | None = None


def apply_quality_overlay(
    ranked: list[RankedStock],
    quality_inputs_by_symbol: dict[str, QualityInputs],
    config: QualityOverlayConfig,
    price_shares_by_symbol: dict[str, tuple[dict[date, float], dict]] | None = None,
    as_of: date | None = None,
) -> list[QualityAdjustedStock]:
    """Computes every enabled signal for each already-ranked stock and
    applies the enabled filters, in this fixed order: F-score, Z-score,
    accrual, promoter-pledge, then momentum re-sort (momentum never drops
    a stock, only reorders - it's a tilt, not a filter, per the original
    ask). A stock missing quality_inputs_by_symbol entirely gets every
    signal as None/not-flagged and is never excluded by a disabled or
    data-starved filter - an overlay should never be stricter than "can't
    tell" when it has nothing to check.

    With every config flag at its default (all False/0.0), this returns
    every input stock, unexcluded, in its original order - a structural
    passthrough, not a coincidence of the specific thresholds chosen.
    """
    results: list[QualityAdjustedStock] = []
    for r in ranked:
        inputs = quality_inputs_by_symbol.get(r.symbol)
        f_score = compute_f_score(inputs) if inputs else None
        z_score = compute_z_score(inputs) if inputs else None
        accrual_flagged = compute_accrual_flag(inputs, config.accrual_threshold) if inputs else None
        pledge_flagged = compute_promoter_pledge_flag(inputs, config.promoter_pledge_threshold) if inputs else False

        momentum = None
        if config.enable_momentum_tilt and price_shares_by_symbol and as_of:
            series = price_shares_by_symbol.get(r.symbol)
            if series is not None:
                momentum = compute_momentum(series[0], as_of, config.momentum_lookback_days)

        excluded = False
        reason = None
        if config.enable_f_score_filter and f_score is not None and f_score.max_score > 0:
            if f_score.score / f_score.max_score < config.min_f_score_fraction:
                excluded, reason = True, f"F-score {f_score.score}/{f_score.max_score} below threshold"
        if not excluded and config.enable_z_score_filter and config.exclude_z_score_distress:
            if z_score is not None and z_score.zone == "distress":
                excluded, reason = True, f"Z-score {z_score.score:.2f} in distress zone"
        if not excluded and config.enable_accrual_filter and accrual_flagged:
            excluded, reason = True, "accrual ratio above threshold"
        if not excluded and config.enable_promoter_pledge_filter and pledge_flagged:
            excluded, reason = True, "promoter pledge above threshold"

        results.append(QualityAdjustedStock(
            ranked=r, f_score=f_score, z_score=z_score, accrual_flagged=accrual_flagged,
            promoter_pledge_flagged=pledge_flagged, momentum=momentum,
            excluded_by_overlay=excluded, exclusion_reason=reason,
        ))

    if config.enable_momentum_tilt and config.momentum_weight > 0:
        results.sort(
            key=lambda qa: qa.ranked.combined_rank - config.momentum_weight * (qa.momentum or 0.0)
        )

    return results
