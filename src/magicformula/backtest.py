"""Point-in-time backtest engine (SPEC.md section 7).

All of this is synthetic-testable without a real historical data pull -
same pattern as ranker.py and portfolio.py - and deliberately not yet
wired to one: real historical fundamentals require the fundamentals
pipeline validated at full-universe scale first (docs/decisions/0003,
0004, 0005), and real historical prices require magicformula.data_fetch.
prices wired to actual point-in-time price history, neither of which this
module touches. Every price/benchmark-return input below is a caller-
supplied function or list, so tests can supply trivial synthetic data and
a later real run only needs to supply real data through the same
interface, not a redesign.

Pieces, in dependency order:

1. Point-in-time embargo (filter_point_in_time): at any rebalance date,
   only fundamental data whose as_of_date has already arrived is usable
   (SPEC.md section 6) - this is what stops the backtest from using
   information that wasn't actually public yet. **The public-availability
   lag lives in exactly one place: `FundamentalsRecord.as_of_date`
   (fiscal_year_end + `ANNUAL_FILING_LAG_DAYS`, magicformula.data_fetch.
   fundamentals) - not here.** filter_point_in_time does a plain
   `as_of_date <= rebalance_date` comparison, no further lag subtracted.
   Decision 0011 found an earlier version of this function subtracted a
   *second* independent lag from rebalance_date before comparing,
   compounding with the one already baked into as_of_date - safe
   direction (more conservative, not a look-ahead risk) but silently
   aged every rebalance's data by roughly an extra fiscal year. If a
   wider embargo is ever genuinely needed, widen
   `ANNUAL_FILING_LAG_DAYS` at the source - do not add a second
   subtraction here, that's exactly how this bug happened the first
   time.

2. Rebalance-walk simulation (run_rebalance_walk): iterate a sequence of
   rebalance dates, apply the point-in-time embargo, rank the eligible
   universe, and call magicformula.portfolio.rebalance() at each date -
   carrying holdings forward from one step to the next so the turnover
   buffer (SPEC.md section 6) operates across time the way it would in a
   real backtest, not just within a single rebalance.

3. Period returns (compute_period_return, compute_turnover): the
   equal-weighted basket return between two consecutive rebalance dates,
   net of a transaction cost assumption applied to the turned-over
   fraction (SPEC.md section 7 default 25 bps round trip).

4. Performance stats (compute_cagr, compute_annualized_volatility,
   compute_sharpe_ratio, compute_max_drawdown, compute_hit_rate) and the
   two chart-data series SPEC.md section 7 asks for (compute_equity_curve,
   compute_rolling_excess_return) - all pure functions over a list of
   period returns, not tied to how those returns were produced.

5. run_backtest(): orchestrates 1-4 into one call, and
   write_basket_composition_csv() for the "full history of basket
   composition per rebalance date as a CSV" SPEC.md section 7 asks for.

Not built here: actual chart rendering (matplotlib/plotly - SPEC.md
section 9 assigns that to the dashboard/reporting layer, not the backtest
engine itself) and the Phase 2 quality overlay (quality_overlay.py,
explicitly held off per the user until this engine runs against real data
at least once).
"""

from __future__ import annotations

import csv
import statistics
from dataclasses import dataclass
from datetime import date
from pathlib import Path
from typing import Callable

from magicformula.data_fetch.fundamentals import FundamentalsRecord
from magicformula.portfolio import RebalanceOutcome, rebalance
from magicformula.ranker import RankedStock

RankFn = Callable[[list[FundamentalsRecord], date], list[RankedStock]]
PriceLookup = Callable[[str, date], float | None]


def filter_point_in_time(
    records: list[FundamentalsRecord],
    rebalance_date: date,
) -> list[FundamentalsRecord]:
    """Only fundamentals whose as_of_date has already arrived by
    rebalance_date are usable at that rebalance (SPEC.md section 6). A
    record whose as_of_date lands exactly on rebalance_date is included
    ("at least" public, not "more than").

    No lag is subtracted here - the public-availability buffer is
    `as_of_date` itself (`fiscal_year_end + ANNUAL_FILING_LAG_DAYS`,
    computed once in `FundamentalsRecord.__post_init__`), not a second,
    independent buffer applied again at this layer. Decision 0011: this
    function used to also subtract a `fundamental_lag_days` (default 60)
    from `rebalance_date` before comparing, which combined with the lag
    already inside `as_of_date` to embargo roughly twice the intended
    ~60 days - real effect, checked against real TCS data, was that a
    "2026-06-01" rebalance used FY2025 fundamentals instead of the
    FY2026 data that was genuinely public by then. Fixed by removing the
    second buffer entirely rather than tuning it down - one mechanism,
    one place, matching SPEC.md section 6's intent of a single embargo,
    not two independent ones stacked for the same purpose.
    """
    return [r for r in records if r.as_of_date <= rebalance_date]


def clip_to_listing_date(
    records: list[FundamentalsRecord], listing_date: date | None
) -> list[FundamentalsRecord]:
    """Drop any fiscal year that ended before the company's actual NSE
    listing date - real, audited financials that existed before the
    company was tradeable at all.

    This is a different gate from filter_point_in_time, and both are
    needed: a company can show fundamentals history predating its own
    listing (decision 0006's 3B Blackbio case - listed on NSE only in
    April 2026, but screener.in shows financials back to 2015, because
    the underlying entity's audited history existed before that
    particular listing). Without this clip, filter_point_in_time alone
    would happily pass that FY2015 record's as_of_date (~mid-2015) for a
    rebalance in, say, 2016 - the embargo only checks "was this
    information public yet", not "could this stock have been bought yet".
    Applying this clip first means the earliest surviving record's own
    as_of_date is already at-or-after the listing date, so the ordinary
    point-in-time embargo composes correctly on top of it without a
    separate rebalance-date-vs-listing-date check.

    listing_date=None (unknown - e.g. a BSE-only entity with no NSE
    listing date available, a gap magicformula.universe already documents
    for that case) leaves records unchanged: there's nothing to clip
    against, which is not the same as "clipping is unnecessary".
    """
    if listing_date is None:
        return records
    return [r for r in records if r.fiscal_year_end >= listing_date]


def clip_all_to_listing_dates(
    records: list[FundamentalsRecord], listing_date_by_symbol: dict[str, date]
) -> list[FundamentalsRecord]:
    """clip_to_listing_date applied per-symbol across a mixed,
    multi-company records list (the shape UniverseFetchResult.records and
    run_rebalance_walk's all_fundamentals both use). A symbol missing from
    listing_date_by_symbol is left unclipped, same as passing
    listing_date=None for it individually.
    """
    return [
        r for r in records
        if r.symbol not in listing_date_by_symbol
        or r.fiscal_year_end >= listing_date_by_symbol[r.symbol]
    ]


@dataclass
class RebalanceStep:
    rebalance_date: date
    eligible_universe_size: int
    outcome: RebalanceOutcome


def run_rebalance_walk(
    rebalance_dates: list[date],
    all_fundamentals: list[FundamentalsRecord],
    rank_fn: RankFn,
    top_n: int,
    buffer_multiplier: float = 1.5,
    sector_by_symbol: dict[str, str] | None = None,
    max_sector_fraction: float = 0.30,
    listing_date_by_symbol: dict[str, date] | None = None,
) -> list[RebalanceStep]:
    """Walk forward through rebalance_dates, applying the point-in-time
    embargo before ranking at each date and carrying holdings forward so
    the turnover buffer operates across the whole sequence, not just
    within one rebalance.

    No `fundamental_lag_days` parameter here (decision 0011 removed it) -
    the embargo's lag lives solely in `FundamentalsRecord.as_of_date`;
    see filter_point_in_time's docstring before adding one back.

    `all_fundamentals` is the full pool across every period covered by
    rebalance_dates - each record's own as_of_date determines which
    rebalance(s) it's eligible for, not which one it's passed in for.

    `listing_date_by_symbol`, if given, is applied once up front via
    clip_all_to_listing_dates - before *any* rebalance's point-in-time
    embargo runs, not as an afterthought. Without it, a company's
    fundamentals history predating its own listing (decision 0006) could
    pass the embargo for an early rebalance date and get ranked as if it
    had been a buyable stock at the time, which it wasn't. Pass this
    whenever it's available (from magicformula.universe's per-entity
    date_of_listing) - a real backtest should not skip it.

    `rank_fn` takes the point-in-time-eligible fundamentals plus the
    rebalance date and returns ranked stocks - injected so this can be
    tested with a trivial synthetic ranker (a real caller would compute
    ROCE/EY via magicformula.formulas and rank via magicformula.ranker
    from the eligible records).
    """
    if listing_date_by_symbol:
        all_fundamentals = clip_all_to_listing_dates(all_fundamentals, listing_date_by_symbol)

    steps: list[RebalanceStep] = []
    holdings: set[str] = set()

    for rebalance_date in sorted(rebalance_dates):
        eligible = filter_point_in_time(all_fundamentals, rebalance_date)
        ranked = rank_fn(eligible, rebalance_date)
        outcome = rebalance(
            ranked,
            previous_holdings=holdings,
            top_n=top_n,
            buffer_multiplier=buffer_multiplier,
            sector_by_symbol=sector_by_symbol,
            max_sector_fraction=max_sector_fraction,
        )
        steps.append(
            RebalanceStep(
                rebalance_date=rebalance_date,
                eligible_universe_size=len(eligible),
                outcome=outcome,
            )
        )
        holdings = outcome.holdings

    return steps


# --- Period returns -------------------------------------------------------


def compute_turnover(previous_holdings: set[str], outcome: RebalanceOutcome) -> float:
    """One-way turnover as a fraction: names changed / average basket size
    across the two periods. 0.0 when both previous and new holdings are
    empty (nothing to turn over). A fresh basket built from nothing (empty
    previous_holdings) reads as 100% turnover, matching intuition - every
    name in it is new.
    """
    denominator = len(previous_holdings) + len(outcome.holdings)
    if denominator == 0:
        return 0.0
    return (len(outcome.buys) + len(outcome.sells)) / denominator


def compute_period_return(
    weights: dict[str, float],
    price_lookup: PriceLookup,
    period_start: date,
    period_end: date,
    turnover: float,
    transaction_cost_bps: float = 25.0,
) -> float:
    """Equal-weighted basket return between two dates, net of a
    round-trip transaction cost (SPEC.md section 7 default 25 bps)
    applied to the turned-over fraction of the basket.

    A symbol missing a price at either end of the period is excluded from
    this period's return entirely (its weight simply doesn't contribute) -
    callers should log that upstream if it happens often; this function
    doesn't have enough context to say whether it's expected (e.g. a
    delisting) or a data gap.
    """
    gross_return = 0.0
    for symbol, weight in weights.items():
        start_price = price_lookup(symbol, period_start)
        end_price = price_lookup(symbol, period_end)
        if start_price is None or end_price is None or start_price <= 0:
            continue
        gross_return += weight * (end_price / start_price - 1)

    return gross_return - turnover * (transaction_cost_bps / 10_000)


# --- Performance stats and chart-data series ------------------------------


def compute_equity_curve(period_returns: list[float], starting_value: float = 1.0) -> list[float]:
    """Cumulative NAV series, one more point than there are returns (the
    starting value comes first) - SPEC.md section 7's equity curve chart."""
    curve = [starting_value]
    for r in period_returns:
        curve.append(curve[-1] * (1 + r))
    return curve


def compute_cagr(period_returns: list[float], periods_per_year: float = 1.0) -> float:
    if not period_returns:
        return 0.0
    cumulative = 1.0
    for r in period_returns:
        cumulative *= 1 + r
    n_years = len(period_returns) / periods_per_year
    if n_years <= 0:
        return 0.0
    return cumulative ** (1 / n_years) - 1


def compute_annualized_volatility(period_returns: list[float], periods_per_year: float = 1.0) -> float:
    if len(period_returns) < 2:
        return 0.0
    return statistics.pstdev(period_returns) * (periods_per_year ** 0.5)


def compute_sharpe_ratio(
    period_returns: list[float], risk_free_rate: float, periods_per_year: float = 1.0
) -> float:
    """risk_free_rate is the ANNUALIZED rate - SPEC.md section 7 asks for
    a stated proxy (e.g. the 91-day T-bill yield); pass whichever one is
    actually used, since this function has no default of its own to fall
    back on silently.
    """
    if len(period_returns) < 2:
        return 0.0
    period_risk_free = risk_free_rate / periods_per_year
    excess_returns = [r - period_risk_free for r in period_returns]
    volatility = statistics.pstdev(excess_returns)
    if volatility == 0:
        return 0.0
    return (statistics.mean(excess_returns) * periods_per_year) / (volatility * (periods_per_year ** 0.5))


def compute_max_drawdown(equity_curve: list[float]) -> float:
    """Most negative peak-to-trough drop, as a fraction (e.g. -0.35 for a
    35% drawdown). 0.0 for an empty or single-point curve."""
    if len(equity_curve) < 2:
        return 0.0
    peak = equity_curve[0]
    max_drawdown = 0.0
    for value in equity_curve:
        peak = max(peak, value)
        drawdown = (value - peak) / peak
        max_drawdown = min(max_drawdown, drawdown)
    return max_drawdown


def compute_hit_rate(strategy_returns: list[float], benchmark_returns: list[float]) -> float:
    """Fraction of periods the strategy beat the benchmark (SPEC.md
    section 7). Both lists must be the same length (one entry per
    rebalance period, aligned)."""
    if len(strategy_returns) != len(benchmark_returns):
        raise ValueError("strategy_returns and benchmark_returns must be the same length")
    if not strategy_returns:
        return 0.0
    wins = sum(1 for s, b in zip(strategy_returns, benchmark_returns) if s > b)
    return wins / len(strategy_returns)


def compute_rolling_excess_return(
    strategy_returns: list[float], benchmark_returns: list[float], window: int
) -> list[float]:
    """Rolling `window`-period cumulative excess return (strategy minus
    benchmark, summed over the trailing window) - SPEC.md section 7's
    "rolling 1-year excess-return chart vs benchmark" (window = however
    many rebalance periods make up a year at the chosen cadence).
    """
    if len(strategy_returns) != len(benchmark_returns):
        raise ValueError("strategy_returns and benchmark_returns must be the same length")
    excess = [s - b for s, b in zip(strategy_returns, benchmark_returns)]
    return [sum(excess[i + 1 - window : i + 1]) for i in range(window - 1, len(excess))]


# --- Orchestration and output artifacts ------------------------------------


@dataclass
class PerformanceStats:
    cagr: float
    annualized_volatility: float
    sharpe_ratio: float
    risk_free_rate_used: float
    max_drawdown: float
    hit_rate: float
    mean_turnover: float
    periods: int


@dataclass
class BacktestResult:
    steps: list[RebalanceStep]
    period_returns: list[float]
    benchmark_returns: list[float]
    equity_curve: list[float]
    benchmark_equity_curve: list[float]
    stats: PerformanceStats


def run_backtest(
    rebalance_dates: list[date],
    all_fundamentals: list[FundamentalsRecord],
    rank_fn: RankFn,
    price_lookup: PriceLookup,
    benchmark_returns: list[float],
    top_n: int,
    buffer_multiplier: float = 1.5,
    sector_by_symbol: dict[str, str] | None = None,
    max_sector_fraction: float = 0.30,
    transaction_cost_bps: float = 25.0,
    risk_free_rate: float = 0.065,
    periods_per_year: float = 1.0,
    listing_date_by_symbol: dict[str, date] | None = None,
) -> BacktestResult:
    """Run the full rebalance walk and compute performance stats.

    `benchmark_returns` must have exactly one entry per *closed* period -
    i.e. len(rebalance_dates) - 1, since the last rebalance date has no
    following date to close its holding period out. Pre-computed rather
    than looked up here: this module doesn't know how to fetch Nifty 500
    TRI or a universe-equal-weight return, only how to compare against
    one once it's supplied (SPEC.md section 7 names both as benchmarks -
    call this twice, once per benchmark, for the full comparison).

    `risk_free_rate` must be an annualized rate the caller actually
    chose (SPEC.md section 7 asks for a stated proxy, e.g. the 91-day
    T-bill yield) - the default here is a placeholder, not a
    recommendation, and `PerformanceStats.risk_free_rate_used` always
    records whatever was actually passed so a report never has to guess.

    No `fundamental_lag_days` parameter here (decision 0011) - see
    filter_point_in_time's docstring for where that lag actually lives.

    `listing_date_by_symbol` - see run_rebalance_walk's docstring; passed
    straight through.
    """
    steps = run_rebalance_walk(
        rebalance_dates, all_fundamentals, rank_fn, top_n,
        buffer_multiplier, sector_by_symbol, max_sector_fraction,
        listing_date_by_symbol,
    )

    if len(benchmark_returns) != max(len(steps) - 1, 0):
        raise ValueError(
            f"benchmark_returns must have {max(len(steps) - 1, 0)} entries "
            f"(one per closed period), got {len(benchmark_returns)}"
        )

    period_returns: list[float] = []
    turnovers: list[float] = []
    previous_holdings: set[str] = set()
    for i, step in enumerate(steps[:-1]):
        turnover = compute_turnover(previous_holdings, step.outcome)
        turnovers.append(turnover)
        period_returns.append(
            compute_period_return(
                step.outcome.weights, price_lookup, step.rebalance_date,
                steps[i + 1].rebalance_date, turnover, transaction_cost_bps,
            )
        )
        previous_holdings = step.outcome.holdings

    equity_curve = compute_equity_curve(period_returns)
    benchmark_equity_curve = compute_equity_curve(benchmark_returns)

    stats = PerformanceStats(
        cagr=compute_cagr(period_returns, periods_per_year),
        annualized_volatility=compute_annualized_volatility(period_returns, periods_per_year),
        sharpe_ratio=compute_sharpe_ratio(period_returns, risk_free_rate, periods_per_year),
        risk_free_rate_used=risk_free_rate,
        max_drawdown=compute_max_drawdown(equity_curve),
        hit_rate=compute_hit_rate(period_returns, benchmark_returns),
        mean_turnover=statistics.mean(turnovers) if turnovers else 0.0,
        periods=len(period_returns),
    )

    return BacktestResult(
        steps=steps,
        period_returns=period_returns,
        benchmark_returns=benchmark_returns,
        equity_curve=equity_curve,
        benchmark_equity_curve=benchmark_equity_curve,
        stats=stats,
    )


def write_basket_composition_csv(steps: list[RebalanceStep], path: Path) -> None:
    """One row per (rebalance_date, symbol, weight) across the whole
    walk - SPEC.md section 7's "full history of basket composition per
    rebalance date as a CSV"."""
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", newline="", encoding="utf-8") as f:
        writer = csv.writer(f)
        writer.writerow(["rebalance_date", "symbol", "weight"])
        for step in steps:
            for symbol, weight in sorted(step.outcome.weights.items()):
                writer.writerow([step.rebalance_date.isoformat(), symbol, weight])
