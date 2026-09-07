"""magicformula CLI - the documented way to run this pipeline end to end
(SPEC.md section 11), replacing the ad hoc scripts/*.py pilots for actual
use. Every parameter that used to be hardcoded (and sometimes
inconsistently duplicated) across those scripts - basket size, buffer
multiplier, price/shares tolerance, transaction cost, rebalance cadence,
anomaly mode - comes from config/settings.yaml via magicformula.config,
not re-hardcoded here.

Six commands: build-universe, fetch-data, rank, basket, backtest,
dashboard. The first five are real and wired to the already-tested
library modules; dashboard is a documented stub - see its own docstring
for why.

State lives under data/processed/: universe.csv, fundamentals.parquet,
the current holdings file (for the turnover buffer to persist across
`basket` runs), and backtest output. Network-fetch caches (screener.in
HTML, yfinance price/shares series) live under data/raw/ and are reused
automatically - fetch-data does not re-hit either data source from
scratch for an already-cached company. This avoids the network cost, not
the wall-clock cost: parsing ~1,900 cached HTML statements and rebuilding
the anomaly-check peer pools still took ~38 minutes end to end in a real
run against an almost-fully-warm cache (1,774 of 1,932 companies already
cached, 158 new ones fetched live from yfinance) - budget accordingly,
this is not a "fast" command even when nothing new is fetched over the
network.
"""

from __future__ import annotations

import csv
import json
import logging
import pickle
from datetime import date
from pathlib import Path

import pandas as pd
import typer

from magicformula.config import Settings, load_settings
from magicformula.data_fetch.fundamentals import (
    FundamentalsRecord,
    ScreenerClient,
    _latest_record_per_symbol,
    check_fundamentals_anomalies,
    fetch_universe_fundamentals,
)
from magicformula.data_fetch.prices import (
    fetch_point_in_time_series,
    nearest_value_lookup,
)
from magicformula.formulas import FinancialInputs, compute_metrics
from magicformula.ranker import RankedStock, rank_universe
from magicformula.portfolio import rebalance
from magicformula.backtest import (
    filter_point_in_time,
    run_backtest,
    write_basket_composition_csv,
)
from magicformula.universe import build_universe as build_universe_pipeline

app = typer.Typer(help="Magic Formula India - NSE/BSE quantitative value screener.")
logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
logger = logging.getLogger("magicformula.cli")

PROCESSED_DIR = Path("data/processed")
UNIVERSE_PATH = PROCESSED_DIR / "universe.csv"
FUNDAMENTALS_PATH = PROCESSED_DIR / "fundamentals.parquet"
SERIES_CACHE_PATH = Path("data/raw/point_in_time_series_cache.pkl")
SCREENER_CACHE_DIR = Path("data/raw/screener_html_cache")
HOLDINGS_STATE_PATH = PROCESSED_DIR / "current_holdings.json"


def _settings(config_path: Path) -> Settings:
    return load_settings(config_path)


def _load_universe_df() -> pd.DataFrame:
    if not UNIVERSE_PATH.exists():
        raise typer.BadParameter(
            f"{UNIVERSE_PATH} not found - run `magicformula build-universe` first."
        )
    return pd.read_csv(UNIVERSE_PATH, dtype={"nse_symbol": str, "bse_symbol": str})


def _load_fundamentals() -> list[FundamentalsRecord]:
    if not FUNDAMENTALS_PATH.exists():
        raise typer.BadParameter(
            f"{FUNDAMENTALS_PATH} not found - run `magicformula fetch-data` first."
        )
    df = pd.read_parquet(FUNDAMENTALS_PATH)
    fields = [
        "symbol", "sector", "broad_sector", "fiscal_year_end", "operating_profit",
        "depreciation_amortization", "total_assets", "other_liabilities", "investments",
        "cwip", "borrowings", "market_cap", "source_url", "statement", "cap_bucket",
    ]
    return [FundamentalsRecord(**row._asdict()) for row in df[fields].itertuples(index=False)]


def _load_series_cache() -> dict[str, tuple[dict, dict]]:
    if not SERIES_CACHE_PATH.exists():
        return {}
    with SERIES_CACHE_PATH.open("rb") as f:
        return pickle.load(f)


def _build_rank_fn(settings: Settings, price_shares_by_symbol: dict, cap_bucket_by_symbol: dict):
    """Anomaly status is computed fresh, per rebalance date, against that
    date's own point-in-time-eligible latest-year records - decision
    0013's fix. Never reuse a globally-latest-year anomaly verdict across
    dates; see docs/decisions/0013 for what goes wrong if you do.
    """
    def rank_fn(eligible: list[FundamentalsRecord], rebalance_date: date) -> list[RankedStock]:
        latest = _latest_record_per_symbol(eligible)
        anomaly_results = check_fundamentals_anomalies(
            latest, iqr_k=settings.fundamentals.anomaly_iqr_k,
            min_sector_size=settings.fundamentals.anomaly_min_sector_size,
        )
        flagged = {r.symbol for r in anomaly_results if r.flagged}
        candidates = latest if settings.fundamentals.anomaly_mode == "flag_only" else [
            r for r in latest if r.symbol not in flagged
        ]

        metrics = []
        for r in candidates:
            series = price_shares_by_symbol.get(r.symbol)
            if series is None:
                continue
            price_series, shares_series = series
            price = nearest_value_lookup(
                price_series, rebalance_date,
                max_days_tolerance=settings.prices.price_lookup_max_tolerance_days,
            )
            if price is None:
                continue
            shares = nearest_value_lookup(
                shares_series, rebalance_date,
                max_days_tolerance=settings.prices.shares_lookup_max_tolerance_days,
            )
            if shares is None:
                continue
            market_cap = (price * shares) / 1e7  # raw rupees -> Rs. Crore
            inputs = FinancialInputs(
                symbol=r.symbol, operating_profit=r.operating_profit,
                depreciation_amortization=r.depreciation_amortization,
                total_assets=r.total_assets, current_liabilities=r.other_liabilities,
                market_cap=market_cap, total_debt=r.borrowings,
                cash_and_equivalents=0, other_liquid_investments=r.investments,
            )
            metrics.append(compute_metrics(inputs, roce_mode=settings.ranking.roce_mode))
        return rank_universe(metrics, mode="within_bucket", cap_bucket_by_symbol=cap_bucket_by_symbol)

    return rank_fn


@app.command("build-universe")
def build_universe_cmd(
    config: Path = typer.Option(Path("config/settings.yaml"), help="Path to settings.yaml"),
) -> None:
    """Fetch NSE+BSE+AMFI, merge, tag cap buckets, apply exclusions. Writes
    data/processed/universe.csv."""
    settings = _settings(config)
    as_of = settings.universe.as_of or date.today()
    entries = build_universe_pipeline(settings.universe.amfi_xlsx_url, as_of=as_of)

    PROCESSED_DIR.mkdir(parents=True, exist_ok=True)
    with UNIVERSE_PATH.open("w", newline="", encoding="utf-8") as f:
        writer = csv.writer(f)
        writer.writerow([
            "isin", "name", "nse_symbol", "bse_symbol", "cap_bucket",
            "date_of_listing", "excluded", "exclusion_reason",
        ])
        for e in entries:
            writer.writerow([
                e.isin, e.name, e.nse_symbol, e.bse_symbol, e.cap_bucket,
                e.date_of_listing.isoformat() if e.date_of_listing else "",
                e.excluded, e.exclusion_reason or "",
            ])

    by_bucket: dict[str, int] = {}
    eligible = [e for e in entries if not e.excluded and e.cap_bucket and e.nse_symbol]
    for e in eligible:
        by_bucket[e.cap_bucket] = by_bucket.get(e.cap_bucket, 0) + 1
    typer.echo(f"Universe: {len(entries)} total entities, {len(eligible)} eligible, by bucket: {by_bucket}")
    typer.echo(f"Written to {UNIVERSE_PATH}")


@app.command("fetch-data")
def fetch_data_cmd(
    config: Path = typer.Option(Path("config/settings.yaml"), help="Path to settings.yaml"),
) -> None:
    """Fetch fundamentals (screener.in) and point-in-time price/shares
    series (yfinance) for every eligible universe entry. Both are cached
    to disk (data/raw/screener_html_cache, data/raw/point_in_time_series_cache.pkl),
    which avoids re-hitting either source for an already-cached company -
    it does not make this command fast in absolute terms. Budget ~30-45
    minutes even on a warm cache (real measurement: 38 minutes for 1,932
    eligible companies, 1,774 already cached); a fully cold run (no
    caches at all) is 1.5-2.5+ hours, per full_universe_fundamentals_fetch.py's
    own prior measurement."""
    settings = _settings(config)
    universe_df = _load_universe_df()
    eligible = universe_df[
        (~universe_df["excluded"]) & universe_df["cap_bucket"].isin(["large", "mid", "small"])
        & universe_df["nse_symbol"].notna()
    ]
    symbols = sorted(eligible["nse_symbol"].tolist())
    cap_bucket_by_symbol = dict(zip(eligible["nse_symbol"], eligible["cap_bucket"]))
    typer.echo(f"Fetching fundamentals for {len(symbols)} eligible companies...")

    client = ScreenerClient(cache_dir=SCREENER_CACHE_DIR)
    result = fetch_universe_fundamentals(
        symbols, client,
        iqr_k=settings.fundamentals.anomaly_iqr_k,
        min_sector_size=settings.fundamentals.anomaly_min_sector_size,
        anomaly_mode=settings.fundamentals.anomaly_mode,
        cap_bucket_by_symbol=cap_bucket_by_symbol,
    )

    PROCESSED_DIR.mkdir(parents=True, exist_ok=True)
    df = pd.DataFrame([r.__dict__ for r in result.records])
    df.to_parquet(FUNDAMENTALS_PATH, index=False)
    typer.echo(f"Fundamentals: {len(result.records)} records for "
               f"{len({r.symbol for r in result.records})} companies written to {FUNDAMENTALS_PATH}")

    entry_by_symbol = {
        row.nse_symbol: row for row in eligible.itertuples()
    }
    cache = _load_series_cache()
    missing = [s for s in symbols if s not in cache]
    typer.echo(f"Price/shares series: {len(symbols) - len(missing)} of {len(symbols)} "
               f"already cached, fetching {len(missing)} missing...")
    for i, symbol in enumerate(missing):
        entry = entry_by_symbol.get(symbol)
        bse_symbol = getattr(entry, "bse_symbol", None) if entry else None
        result_series = fetch_point_in_time_series(symbol, bse_symbol if isinstance(bse_symbol, str) else None)
        if result_series is not None:
            cache[symbol] = result_series
        if (i + 1) % 100 == 0:
            typer.echo(f"  {i + 1}/{len(missing)} fetched")

    SERIES_CACHE_PATH.parent.mkdir(parents=True, exist_ok=True)
    with SERIES_CACHE_PATH.open("wb") as f:
        pickle.dump(cache, f)
    typer.echo(f"Price/shares series: {len(cache)} companies cached to {SERIES_CACHE_PATH}")


@app.command("rank")
def rank_cmd(
    config: Path = typer.Option(Path("config/settings.yaml"), help="Path to settings.yaml"),
    as_of: str = typer.Option(None, help="YYYY-MM-DD; defaults to today"),
    top: int = typer.Option(10, help="How many per bucket to print"),
) -> None:
    """Compute today's (or --as-of's) Magic Formula ranking - point-in-time
    embargo applied, anomaly status computed fresh, no basket/turnover
    logic (see `basket` for that)."""
    settings = _settings(config)
    rebalance_date = date.fromisoformat(as_of) if as_of else date.today()

    universe_df = _load_universe_df()
    fundamentals = _load_fundamentals()
    cap_bucket_by_symbol = dict(zip(universe_df["nse_symbol"], universe_df["cap_bucket"]))
    price_shares_by_symbol = _load_series_cache()

    eligible = filter_point_in_time(fundamentals, rebalance_date)
    rank_fn = _build_rank_fn(settings, price_shares_by_symbol, cap_bucket_by_symbol)
    ranked = rank_fn(eligible, rebalance_date)

    typer.echo(f"Ranking as of {rebalance_date} ({len(ranked)} ranked stocks):")
    by_bucket: dict[str, list[RankedStock]] = {}
    for r in ranked:
        by_bucket.setdefault(r.cap_bucket or "unknown", []).append(r)
    for bucket, stocks in sorted(by_bucket.items()):
        stocks_sorted = sorted(stocks, key=lambda s: s.position)
        typer.echo(f"\n[{bucket}] top {min(top, len(stocks_sorted))} of {len(stocks_sorted)}:")
        for s in stocks_sorted[:top]:
            typer.echo(f"  {s.position:>3}. {s.symbol:<15} ROCE={s.roce:.2%}  EY={s.ey:.2%}  combined_rank={s.combined_rank}")

    PROCESSED_DIR.mkdir(parents=True, exist_ok=True)
    ranking_path = PROCESSED_DIR / "current_ranking.csv"
    with ranking_path.open("w", newline="", encoding="utf-8") as f:
        writer = csv.writer(f)
        writer.writerow(["as_of", "position", "symbol", "cap_bucket", "roce", "ey", "rank_roce", "rank_ey", "combined_rank"])
        for r in sorted(ranked, key=lambda s: (s.cap_bucket or "", s.position)):
            writer.writerow([rebalance_date.isoformat(), r.position, r.symbol, r.cap_bucket,
                              r.roce, r.ey, r.rank_roce, r.rank_ey, r.combined_rank])
    typer.echo(f"\nFull ranking written to {ranking_path}")


@app.command("basket")
def basket_cmd(
    config: Path = typer.Option(Path("config/settings.yaml"), help="Path to settings.yaml"),
    as_of: str = typer.Option(None, help="YYYY-MM-DD; defaults to today"),
) -> None:
    """Construct the actual basket to hold now: ranks, then applies the
    turnover buffer and sector cap against whatever basket was last
    written by this command (data/processed/current_holdings.json) - a
    fresh basket if this is the first run. Writes
    data/processed/current_basket.csv and updates the holdings state."""
    settings = _settings(config)
    rebalance_date = date.fromisoformat(as_of) if as_of else date.today()

    universe_df = _load_universe_df()
    fundamentals = _load_fundamentals()
    cap_bucket_by_symbol = dict(zip(universe_df["nse_symbol"], universe_df["cap_bucket"]))
    sector_by_symbol = {r.symbol: r.sector for r in fundamentals if r.sector}
    price_shares_by_symbol = _load_series_cache()

    eligible = filter_point_in_time(fundamentals, rebalance_date)
    rank_fn = _build_rank_fn(settings, price_shares_by_symbol, cap_bucket_by_symbol)
    ranked = rank_fn(eligible, rebalance_date)

    previous_holdings: set[str] = set()
    if HOLDINGS_STATE_PATH.exists():
        with HOLDINGS_STATE_PATH.open("r", encoding="utf-8") as f:
            previous_holdings = set(json.load(f)["holdings"])

    outcome = rebalance(
        ranked, previous_holdings=previous_holdings,
        top_n=settings.portfolio.top_n, buffer_multiplier=settings.portfolio.buffer_multiplier,
        sector_by_symbol=sector_by_symbol, max_sector_fraction=settings.portfolio.max_sector_fraction,
    )

    ranked_lookup = {r.symbol: r for r in ranked}
    PROCESSED_DIR.mkdir(parents=True, exist_ok=True)
    basket_path = PROCESSED_DIR / "current_basket.csv"
    with basket_path.open("w", newline="", encoding="utf-8") as f:
        writer = csv.writer(f)
        writer.writerow(["symbol", "weight", "cap_bucket", "roce", "ey", "combined_rank"])
        for symbol, weight in sorted(outcome.weights.items()):
            r = ranked_lookup.get(symbol)
            writer.writerow([symbol, weight, cap_bucket_by_symbol.get(symbol),
                              r.roce if r else "", r.ey if r else "", r.combined_rank if r else ""])

    with HOLDINGS_STATE_PATH.open("w", encoding="utf-8") as f:
        json.dump({"as_of": rebalance_date.isoformat(), "holdings": sorted(outcome.holdings)}, f, indent=2)

    typer.echo(f"Basket as of {rebalance_date}: {len(outcome.holdings)} holdings "
               f"({len(outcome.buys)} buys, {len(outcome.sells)} sells vs. previous state)")
    typer.echo(f"Written to {basket_path}, holdings state updated in {HOLDINGS_STATE_PATH}")


@app.command("backtest")
def backtest_cmd(
    config: Path = typer.Option(Path("config/settings.yaml"), help="Path to settings.yaml"),
    benchmark_returns_json: Path = typer.Option(
        None, help="Optional JSON list of per-period benchmark returns "
                    "(len = number of rebalance dates - 1). Without it, "
                    "only strategy-side stats (CAGR, vol, drawdown, turnover) are reported."
    ),
) -> None:
    """Run the full historical rebalance walk and section 7 performance
    stats. Rebalance dates, basket size, buffer, tolerances, transaction
    cost, and risk-free rate all come from config/settings.yaml."""
    settings = _settings(config)
    end_year = settings.backtest.end_year or date.today().year
    rebalance_dates = [
        date(y, settings.backtest.rebalance_month, settings.backtest.rebalance_day)
        for y in range(settings.backtest.start_year, end_year + 1)
    ]

    universe_df = _load_universe_df()
    fundamentals = _load_fundamentals()
    cap_bucket_by_symbol = dict(zip(universe_df["nse_symbol"], universe_df["cap_bucket"]))
    sector_by_symbol = {r.symbol: r.sector for r in fundamentals if r.sector}
    listing_date_by_symbol = {
        row.nse_symbol: date.fromisoformat(row.date_of_listing)
        for row in universe_df.itertuples()
        if isinstance(row.date_of_listing, str) and row.date_of_listing
    }
    price_shares_by_symbol = _load_series_cache()

    def price_lookup(symbol: str, d: date) -> float | None:
        series = price_shares_by_symbol.get(symbol)
        if series is None:
            return None
        price_series, _ = series
        return nearest_value_lookup(price_series, d, max_days_tolerance=settings.prices.price_lookup_max_tolerance_days)

    rank_fn = _build_rank_fn(settings, price_shares_by_symbol, cap_bucket_by_symbol)

    if benchmark_returns_json is not None:
        with benchmark_returns_json.open() as f:
            benchmark_returns = json.load(f)
    else:
        benchmark_returns = [0.0] * (len(rebalance_dates) - 1)
        typer.echo("No --benchmark-returns-json given - hit rate and excess-return stats "
                   "will be reported against a flat 0% benchmark, not a real index. "
                   "See docs/decisions/0012 for how the real Nifty 500 TRI series was sourced.")

    result = run_backtest(
        rebalance_dates, fundamentals, rank_fn, price_lookup, benchmark_returns,
        top_n=settings.portfolio.top_n, buffer_multiplier=settings.portfolio.buffer_multiplier,
        sector_by_symbol=sector_by_symbol, max_sector_fraction=settings.portfolio.max_sector_fraction,
        listing_date_by_symbol=listing_date_by_symbol,
        transaction_cost_bps=settings.backtest.transaction_cost_bps,
        risk_free_rate=settings.backtest.risk_free_rate,
        periods_per_year=settings.backtest.periods_per_year,
    )

    # Named cli_backtest_* deliberately, not backtest_stats.json/
    # backtest_baskets.csv - this repo already has real, manually-produced
    # research snapshots at those exact names (decision 0012/0013's
    # Nifty-500-TRI-benchmarked results), and this command overwrote them
    # by accident twice during development before this rename. A fresh
    # clone won't have that collision, but the distinct name costs
    # nothing and removes the risk permanently either way.
    PROCESSED_DIR.mkdir(parents=True, exist_ok=True)
    write_basket_composition_csv(result.steps, PROCESSED_DIR / "cli_backtest_baskets.csv")
    with (PROCESSED_DIR / "cli_backtest_stats.json").open("w") as f:
        json.dump({
            "period_returns": result.period_returns, "benchmark_returns": result.benchmark_returns,
            "equity_curve": result.equity_curve, "benchmark_equity_curve": result.benchmark_equity_curve,
            "rebalance_dates": [d.isoformat() for d in rebalance_dates],
            "stats": result.stats.__dict__,
        }, f, indent=2)

    s = result.stats
    typer.echo(f"Backtest {rebalance_dates[0]} -> {rebalance_dates[-1]}: CAGR={s.cagr:+.2%}  "
               f"vol={s.annualized_volatility:.2%}  Sharpe={s.sharpe_ratio:.3f}  "
               f"max_drawdown={s.max_drawdown:.2%}  turnover={s.mean_turnover:.2%}")
    typer.echo(f"Written to {PROCESSED_DIR}/cli_backtest_baskets.csv and cli_backtest_stats.json")


@app.command("dashboard")
def dashboard_cmd(
    port: int = typer.Option(8501, help="Local port to serve the dashboard on"),
) -> None:
    """Launch the Streamlit dashboard: browse the current basket, the
    full rank table, and backtest results (SPEC.md section 9). None of
    these three views need quality_overlay.py's Phase 2 output - they
    only read files `rank`, `basket`, and `backtest` already produce.
    quality_overlay.py, once built, adds its own columns/filters to these
    same views rather than being a prerequisite for them.
    """
    import subprocess
    import sys

    app_path = Path(__file__).parent / "dashboard_app.py"
    typer.echo(f"Launching dashboard on http://localhost:{port} (Ctrl+C to stop)...")
    subprocess.run([
        sys.executable, "-m", "streamlit", "run", str(app_path),
        "--server.port", str(port), "--server.headless", "true",
    ])


if __name__ == "__main__":
    app()
