"""Price and market data acquisition (SPEC.md section 2.3).

Two sources, each independently cached:

1. yfinance (primary) - `.NS` suffix for NSE-listed names, `.BO` for
   BSE-only names (SPEC.md section 2.3). Wrapped in retry-with-backoff;
   treat failures as expected, not exceptional - yfinance wraps Yahoo's
   internal endpoints, not an official product, and has broken from
   upstream auth changes before (SPEC.md section 2.5).

2. NSE bhavcopy daily archives (fallback) - official, stable CSV format,
   good for point-in-time backtest integrity even when yfinance is down.
   One trading day at a time, keyed by ISIN (not just symbol, so a
   company can be matched even if its ticker changed).

Every raw pull is cached to disk (SPEC.md section 2.3) so repeated runs
don't re-hit either source. NSE archives are official public data (SPEC.md
section 2.5) - no rate limiting needed the way screener.in requires, but
caching still avoids needless re-fetching.

Also here: a generic point-in-time value lookup (nearest_value_lookup) and
market-cap helper (compute_point_in_time_market_cap), the mechanism
docs/decisions/0007-point-in-time-market-cap.md designs around for
historical EV/EY - built and synthetic-tested now, deliberately not yet
wired to a live shares-outstanding source (see that decision doc for why
a constant-shares assumption was rejected and what's still open).
"""

from __future__ import annotations

import io
import logging
import time
import zipfile
from dataclasses import dataclass
from datetime import date, datetime
from pathlib import Path
from typing import Callable, Literal

import pandas as pd
import requests
import yfinance as yf

logger = logging.getLogger(__name__)

DEFAULT_USER_AGENT = "MagicFormulaPIT-Research/0.1 (personal, non-commercial research tool)"

NSE_BHAVCOPY_URL_TEMPLATE = (
    "https://nsearchives.nseindia.com/content/cm/"
    "BhavCopy_NSE_CM_0_0_0_{date_str}_F_0000.csv.zip"
)

PriceSource = Literal["yfinance", "bhavcopy"]


class NotATradingDayError(Exception):
    """Raised when NSE has no bhavcopy for a given date (weekend/holiday),
    distinct from a genuine fetch failure so callers can walk back a day
    rather than retry the same date."""


def yf_symbol(nse_symbol: str | None, bse_symbol: str | None) -> str:
    """`.NS` suffix when an NSE symbol exists (deeper liquidity, SPEC.md
    section 1's own preference); `.BO` only for BSE-only names."""
    if nse_symbol:
        return f"{nse_symbol}.NS"
    if bse_symbol:
        return f"{bse_symbol}.BO"
    raise ValueError("no NSE or BSE symbol available to build a yfinance ticker")


# --- yfinance client ---------------------------------------------------


class YFinanceClient:
    """Retry-with-backoff, disk-caching wrapper around yfinance.

    `ticker_factory` defaults to yfinance.Ticker but is injectable so tests
    can supply a fake without touching the network.
    """

    def __init__(
        self,
        cache_dir: Path,
        max_retries: int = 3,
        backoff_seconds: float = 2.0,
        ticker_factory: Callable[[str], object] = yf.Ticker,
    ) -> None:
        self.cache_dir = Path(cache_dir)
        self.cache_dir.mkdir(parents=True, exist_ok=True)
        self.max_retries = max_retries
        self.backoff_seconds = backoff_seconds
        self.ticker_factory = ticker_factory

    def fetch_history(
        self,
        symbol: str,
        period: str = "1y",
        force_refresh: bool = False,
    ) -> pd.DataFrame:
        """symbol must already carry the .NS/.BO suffix (see yf_symbol).
        Returns yfinance's own OHLCV DataFrame, cached as parquet.
        """
        cache_path = self.cache_dir / f"{symbol}_{period}.parquet"
        if cache_path.exists() and not force_refresh:
            logger.debug("%s: serving price history from cache", symbol)
            return pd.read_parquet(cache_path)

        last_error: Exception | None = None
        for attempt in range(1, self.max_retries + 1):
            try:
                history = self.ticker_factory(symbol).history(period=period)
                if history is None or history.empty:
                    raise ValueError(f"{symbol}: yfinance returned no rows for period={period}")
                history.to_parquet(cache_path)
                return history
            except Exception as exc:  # noqa: BLE001 - yfinance raises all sorts
                last_error = exc
                logger.warning(
                    "%s: yfinance fetch attempt %d/%d failed: %s",
                    symbol, attempt, self.max_retries, exc,
                )
                if attempt < self.max_retries:
                    time.sleep(self.backoff_seconds * attempt)

        raise RuntimeError(
            f"{symbol}: yfinance failed after {self.max_retries} attempts"
        ) from last_error


# --- NSE bhavcopy fallback ---------------------------------------------


@dataclass
class BhavcopyRow:
    trade_date: date
    isin: str
    ticker_symbol: str
    series: str
    open: float
    high: float
    low: float
    close: float
    prev_close: float
    volume: float
    turnover: float


def parse_bhavcopy_csv(text: str) -> list[BhavcopyRow]:
    """Pure parser for one day's NSE CM bhavcopy CSV (UDiFF common
    bhavcopy format) - no network I/O. Only equity (SctySrs == 'EQ',
    FinInstrmTp == 'STK') rows are kept; the file also carries
    derivatives, SGBs, and other instrument types under the same columns.
    """
    df = pd.read_csv(io.StringIO(text))
    equity = df[(df["FinInstrmTp"] == "STK") & (df["SctySrs"] == "EQ")]

    rows = []
    for record in equity.to_dict("records"):
        rows.append(
            BhavcopyRow(
                trade_date=datetime.strptime(record["TradDt"], "%Y-%m-%d").date(),
                isin=record["ISIN"],
                ticker_symbol=record["TckrSymb"],
                series=record["SctySrs"],
                open=float(record["OpnPric"]),
                high=float(record["HghPric"]),
                low=float(record["LwPric"]),
                close=float(record["ClsPric"]),
                prev_close=float(record["PrvsClsgPric"]),
                volume=float(record["TtlTradgVol"]),
                turnover=float(record["TtlTrfVal"]),
            )
        )
    return rows


class BhavcopyClient:
    """Fetch + cache one day's NSE bhavcopy at a time."""

    def __init__(self, cache_dir: Path, timeout: float = 30.0) -> None:
        self.cache_dir = Path(cache_dir)
        self.cache_dir.mkdir(parents=True, exist_ok=True)
        self.timeout = timeout
        self.session = requests.Session()
        self.session.headers["User-Agent"] = DEFAULT_USER_AGENT

    def fetch(self, trading_date: date, force_refresh: bool = False) -> list[BhavcopyRow]:
        date_str = trading_date.strftime("%Y%m%d")
        cache_path = self.cache_dir / f"bhavcopy_{date_str}.csv"

        if cache_path.exists() and not force_refresh:
            logger.debug("%s: serving bhavcopy from cache", date_str)
            return parse_bhavcopy_csv(cache_path.read_text(encoding="utf-8"))

        url = NSE_BHAVCOPY_URL_TEMPLATE.format(date_str=date_str)
        response = self.session.get(url, timeout=self.timeout)
        if response.status_code == 404:
            raise NotATradingDayError(
                f"no bhavcopy for {trading_date.isoformat()} (weekend/holiday, or not yet published)"
            )
        response.raise_for_status()

        with zipfile.ZipFile(io.BytesIO(response.content)) as archive:
            inner_name = archive.namelist()[0]
            csv_text = archive.read(inner_name).decode("utf-8")

        cache_path.write_text(csv_text, encoding="utf-8")
        return parse_bhavcopy_csv(csv_text)

    def fetch_most_recent(self, as_of: date, max_days_back: int = 7) -> tuple[date, list[BhavcopyRow]]:
        """Walk backward from as_of until a trading day is found (handles
        weekends/holidays) - gives up after max_days_back."""
        for offset in range(max_days_back + 1):
            candidate = date.fromordinal(as_of.toordinal() - offset)
            try:
                return candidate, self.fetch(candidate)
            except NotATradingDayError:
                continue
        raise NotATradingDayError(
            f"no trading day found in the {max_days_back} days up to {as_of.isoformat()}"
        )


# --- Point-in-time market cap (design in docs/decisions/0007, 0008, 0012) -


# How far `nearest_value_lookup` may reach back before a "most recent
# known value" stops meaning "point-in-time" and starts meaning "stale
# data quietly reused". Two different tolerances because the two series
# behave differently in practice - checked directly, not assumed:
# measured the actual gap between consecutive *confirmed* (post-
# filter_persisted_values) observations across 200 real companies
# (166,647 gaps) and found get_shares_full() is far denser than expected
# whenever it has coverage at all - median gap 1 day, p90 5 days, p99
# only 16 days. Decision 0012: an earlier caller used 400 days for
# shares, not a bound in any meaningful sense for annual rebalancing (it
# would accept a share count over a year stale); a first revision
# widened this to 180 reasoning from disclosure-cycle length alone,
# without checking the real gap distribution first. Re-checked with a
# sensitivity sweep (30/60/90/180/400 days) against the real cached
# series before settling on a final value: coverage in already-good
# years (2019+) is nearly identical across the whole range (within ~1pp),
# and already-bad years (2016-2018) stay bad regardless of tolerance -
# the tolerance choice does not change which years are usable, only how
# much margin is given for a genuinely slow disclosure. 90 was chosen as
# ~5.6x the measured p99 gap - generous enough that one unusually slow
# real disclosure cycle doesn't wrongly exclude a company, nowhere near
# loose enough to accept a value from a different fiscal period.
PRICE_LOOKUP_MAX_TOLERANCE_DAYS = 14
SHARES_LOOKUP_MAX_TOLERANCE_DAYS = 90


def filter_persisted_values(
    series: dict[date, float],
    confirmation_window: int = 3,
    min_confirmations: int = 2,
    tolerance_pct: float = 2.0,
) -> dict[date, float]:
    """Drop raw observations that aren't corroborated by nearby readings -
    the noise-rejection layer decision 0008 found `get_shares_full()`
    needs before any live wiring (see that doc's root-cause investigation:
    isolated single/multi-day garbage values, unrelated to any real
    corporate action, sandwiched between otherwise-stable readings, found
    in every ticker checked regardless of cap bucket - up to 2258x in one
    case).

    Takes a plain `dict[date, float]`, the same shape `nearest_value_lookup`
    expects - which also means same-date duplicates are a non-issue here:
    `get_shares_full()` sometimes returns more than one row for the same
    calendar date (its DatetimeIndex isn't always unique), but building a
    `dict` from that series already collapses duplicates to the
    last-assigned value before it ever reaches this function, so no
    explicit dedup step is needed on this side of the interface.

    A reading is kept only if at least `min_confirmations` of its nearest
    `confirmation_window` neighbors *on either side* (raw, unfiltered -
    whichever side has more available data confirms it, so a genuine step
    change right after a real split is confirmed by forward neighbors
    alone) fall within `tolerance_pct` of it. This is a persistence check,
    not a magnitude-threshold check, deliberately: 0008 found real splits
    (e.g. KOTAKBANK's 5:1, NMDC's 3:1) produce ratios just as large as the
    garbage spikes do, so "reject big jumps" would also reject real
    corporate actions. A real change persists across many subsequent
    observations; noise does not recur at all. Small day-to-day jitter
    (SAIL's ~1% wobble, common across many tickers) stays inside
    `tolerance_pct` and is accepted rather than fussed over - only clearly
    unconfirmed outliers get dropped.

    A dropped observation is not replaced with an interpolated or
    synthetic value - it is simply excluded from the returned series, so
    a caller using `nearest_value_lookup` on the result falls back to the
    nearest earlier *confirmed* reading, exactly as if the noisy
    observation had never been recorded. See docs/decisions/0008 for why
    this was chosen over excluding the company from the rebalance
    entirely (that would introduce a selection bias correlated with how
    noisy a company's `get_shares_full()` happens to be, which 0008 found
    is unrelated to its actual investability or cap bucket).
    """
    if not series:
        return {}

    ordered = sorted(series.items())
    n = len(ordered)

    def _within_tolerance(a: float, b: float) -> bool:
        if a == 0:
            return b == 0
        return abs(a - b) / abs(a) <= tolerance_pct / 100.0

    cleaned: dict[date, float] = {}
    for i, (d, v) in enumerate(ordered):
        before = [ordered[k][1] for k in range(max(0, i - confirmation_window), i)]
        after = [ordered[k][1] for k in range(i + 1, min(n, i + 1 + confirmation_window))]
        confirmations = sum(1 for nv in before + after if _within_tolerance(v, nv))
        if confirmations >= min_confirmations:
            cleaned[d] = v

    return cleaned


def nearest_value_lookup(
    series: dict[date, float], target_date: date, max_days_tolerance: int | None = None
) -> float | None:
    """Look up the value in `series` whose date is closest to, but not
    after, target_date - i.e. the most recently known value as of that
    date. Never returns a value dated after target_date: that would leak
    future information into a point-in-time lookup, which is the entire
    reason this helper exists rather than a plain nearest-neighbor match.

    Returns None if no eligible (on-or-before) entry exists at all, or if
    the nearest eligible entry is more than max_days_tolerance days before
    target_date (when given) - a stale match far in the past likely means
    the series doesn't actually cover this period (e.g. data starts later
    than target_date), and should be treated as missing rather than used.

    Used identically for a shares-outstanding series and (once
    magicformula.data_fetch.prices grows a similar close-price series
    keyed by date) a price series - the same "most recent known value,
    never from the future" rule applies to both.
    """
    eligible = [(d, v) for d, v in series.items() if d <= target_date]
    if not eligible:
        return None
    nearest_date, nearest_value = max(eligible, key=lambda dv: dv[0])
    if max_days_tolerance is not None and (target_date - nearest_date).days > max_days_tolerance:
        return None
    return nearest_value


def compute_point_in_time_market_cap(
    price: float | None, shares_outstanding: float | None
) -> float | None:
    """market_cap = price x shares_outstanding. Returns None (not a
    misleading 0) if either input is missing, so a caller can tell "don't
    know" apart from "market cap is genuinely zero".
    """
    if price is None or shares_outstanding is None:
        return None
    return price * shares_outstanding


def fetch_point_in_time_series(
    nse_symbol: str, bse_symbol: str | None
) -> tuple[dict[date, float], dict[date, float]] | None:
    """Real price + cleaned point-in-time shares-outstanding history for
    one company, covering as much history as yfinance has (period="max"
    for price, shares from 2010 - a small-cap or recently-listed company
    that genuinely has no data that far back correctly returns None for
    early dates via nearest_value_lookup's tolerance, rather than being
    forced to look further than it has).

    Originally lived only in scripts/run_expanded_backtest_pilot.py -
    moved here so cli.py (and any future caller) goes through the same
    tested library function instead of a script re-implementing it a
    third time. Returns None if either series is entirely unusable
    (ticker resolution failure, no price history, no shares history after
    filter_persisted_values' outlier rejection - decision 0008).
    """
    try:
        symbol = yf_symbol(nse_symbol, bse_symbol)
    except ValueError:
        return None

    ticker = yf.Ticker(symbol)
    try:
        hist = ticker.history(period="max")
        if hist is None or hist.empty:
            return None
        price_series = {ts.date(): float(px) for ts, px in hist["Close"].items()}

        shares = ticker.get_shares_full(start="2010-01-01")
        if shares is None or len(shares) == 0:
            return None
        raw_shares_series = {ts.date(): float(v) for ts, v in shares.items()}
        shares_series = filter_persisted_values(raw_shares_series)
        if not shares_series:
            return None
    except Exception as exc:  # noqa: BLE001 - yfinance raises all sorts
        logger.warning("%s: price/shares fetch failed: %s", symbol, exc)
        return None

    return price_series, shares_series
