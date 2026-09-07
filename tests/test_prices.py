"""Unit tests for magicformula.data_fetch.prices.

Three layers:
  1. yf_symbol()'s NSE-preferred/BSE-fallback logic.
  2. YFinanceClient - network replaced with an injectable fake ticker
     factory (no live calls in the test suite).
  3. Bhavcopy parser (real saved fixture, one trading day, 4 companies)
     and BhavcopyClient (network mocked).
"""

from __future__ import annotations

from datetime import date
from pathlib import Path
from unittest.mock import MagicMock

import pandas as pd
import pytest
import requests

from magicformula.data_fetch.prices import (
    PRICE_LOOKUP_MAX_TOLERANCE_DAYS,
    SHARES_LOOKUP_MAX_TOLERANCE_DAYS,
    BhavcopyClient,
    NotATradingDayError,
    YFinanceClient,
    compute_point_in_time_market_cap,
    filter_persisted_values,
    nearest_value_lookup,
    parse_bhavcopy_csv,
    yf_symbol,
)

FIXTURES_DIR = Path(__file__).parent / "fixtures"


# --- yf_symbol ---------------------------------------------------------


def test_yf_symbol_prefers_nse():
    assert yf_symbol("TCS", "TCS") == "TCS.NS"


def test_yf_symbol_falls_back_to_bse_only():
    assert yf_symbol(None, "SOMEBSECODE") == "SOMEBSECODE.BO"


def test_yf_symbol_raises_when_neither_available():
    with pytest.raises(ValueError):
        yf_symbol(None, None)


# --- YFinanceClient (network faked) -----------------------------------


class _FakeTicker:
    def __init__(self, df: pd.DataFrame | None = None, raise_on_history: Exception | None = None):
        self._df = df
        self._raise = raise_on_history

    def history(self, period: str) -> pd.DataFrame:
        if self._raise is not None:
            raise self._raise
        return self._df


def _sample_history_df() -> pd.DataFrame:
    return pd.DataFrame(
        {"Close": [100.0, 101.0]}, index=pd.to_datetime(["2026-09-01", "2026-09-02"])
    )


def test_yfinance_client_caches_and_returns_history(tmp_path):
    df = _sample_history_df()
    client = YFinanceClient(cache_dir=tmp_path, ticker_factory=lambda symbol: _FakeTicker(df))

    result = client.fetch_history("TCS.NS", period="1y")

    assert list(result["Close"]) == [100.0, 101.0]
    assert (tmp_path / "TCS.NS_1y.parquet").exists()


def test_yfinance_client_uses_cache_without_calling_factory_again(tmp_path):
    df = _sample_history_df()
    call_count = {"n": 0}

    def factory(symbol):
        call_count["n"] += 1
        return _FakeTicker(df)

    client = YFinanceClient(cache_dir=tmp_path, ticker_factory=factory)
    client.fetch_history("TCS.NS", period="1y")
    client.fetch_history("TCS.NS", period="1y")

    assert call_count["n"] == 1


def test_yfinance_client_retries_then_succeeds(tmp_path):
    attempts = {"n": 0}

    def factory(symbol):
        attempts["n"] += 1
        if attempts["n"] < 2:
            return _FakeTicker(raise_on_history=ConnectionError("boom"))
        return _FakeTicker(_sample_history_df())

    client = YFinanceClient(cache_dir=tmp_path, max_retries=3, backoff_seconds=0.0, ticker_factory=factory)
    result = client.fetch_history("TCS.NS")

    assert attempts["n"] == 2
    assert not result.empty


def test_yfinance_client_raises_after_exhausting_retries(tmp_path):
    def factory(symbol):
        return _FakeTicker(raise_on_history=ConnectionError("boom"))

    client = YFinanceClient(cache_dir=tmp_path, max_retries=2, backoff_seconds=0.0, ticker_factory=factory)

    with pytest.raises(RuntimeError):
        client.fetch_history("TCS.NS")


def test_yfinance_client_treats_empty_history_as_failure(tmp_path):
    def factory(symbol):
        return _FakeTicker(pd.DataFrame())

    client = YFinanceClient(cache_dir=tmp_path, max_retries=1, backoff_seconds=0.0, ticker_factory=factory)

    with pytest.raises(RuntimeError):
        client.fetch_history("DELISTEDCO.NS")


# --- Bhavcopy parser (real fixture) -------------------------------------


def test_parse_bhavcopy_csv_extracts_known_rows():
    text = (FIXTURES_DIR / "bhavcopy_sample_20260904.csv").read_text(encoding="utf-8")
    rows = parse_bhavcopy_csv(text)

    by_symbol = {r.ticker_symbol: r for r in rows}
    assert len(rows) == 4
    assert by_symbol["TCS"].isin == "INE467B01029"
    assert by_symbol["TCS"].trade_date == date(2026, 9, 4)
    assert by_symbol["TCS"].close == 2304.00
    assert by_symbol["TCS"].prev_close == 2320.10
    assert by_symbol["RELIANCE"].isin == "INE002A01018"


def test_parse_bhavcopy_csv_filters_out_non_equity_rows():
    text = (FIXTURES_DIR / "bhavcopy_sample_20260904.csv").read_text(encoding="utf-8")
    # Append a non-EQ series row (e.g. a debt instrument) using the same
    # column layout - should be dropped, not misparsed.
    header, *data_lines = text.strip().splitlines()
    fake_debt_row = data_lines[0].replace(",TCS,EQ,", ",TCSPP,BE,")
    augmented = "\n".join([header, *data_lines, fake_debt_row])

    rows = parse_bhavcopy_csv(augmented)
    assert all(r.series == "EQ" for r in rows)
    assert not any(r.ticker_symbol == "TCSPP" for r in rows)


# --- BhavcopyClient (network mocked) ------------------------------------


def test_bhavcopy_client_uses_cache_without_hitting_network(tmp_path):
    text = (FIXTURES_DIR / "bhavcopy_sample_20260904.csv").read_text(encoding="utf-8")
    cache_file = tmp_path / "bhavcopy_20260904.csv"
    cache_file.write_text(text, encoding="utf-8")

    client = BhavcopyClient(cache_dir=tmp_path)
    client.session.get = MagicMock(side_effect=AssertionError("should not hit network"))

    rows = client.fetch(date(2026, 9, 4))
    assert len(rows) == 4
    client.session.get.assert_not_called()


def test_bhavcopy_client_raises_not_a_trading_day_on_404(tmp_path):
    client = BhavcopyClient(cache_dir=tmp_path)
    response = MagicMock(status_code=404)
    client.session.get = MagicMock(return_value=response)

    with pytest.raises(NotATradingDayError):
        client.fetch(date(2026, 9, 6))  # a Sunday


def _zip_bytes(inner_name: str, text: str) -> bytes:
    import io
    import zipfile

    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w") as zf:
        zf.writestr(inner_name, text)
    return buffer.getvalue()


def test_bhavcopy_client_fetch_most_recent_walks_back_over_non_trading_days(tmp_path):
    client = BhavcopyClient(cache_dir=tmp_path)
    sample_text = (FIXTURES_DIR / "bhavcopy_sample_20260904.csv").read_text(encoding="utf-8")
    sample_zip = _zip_bytes("BhavCopy_NSE_CM_0_0_0_20260904_F_0000.csv", sample_text)

    def fake_get(url, timeout):
        if "20260904" in url:
            return MagicMock(status_code=200, content=sample_zip)
        return MagicMock(status_code=404)

    client.session.get = fake_get

    found_date, rows = client.fetch_most_recent(as_of=date(2026, 9, 6), max_days_back=7)

    assert found_date == date(2026, 9, 4)
    assert len(rows) == 4


# --- Point-in-time lookup (decision 0007) ---------------------------------


def test_nearest_value_lookup_finds_most_recent_on_or_before_target():
    series = {date(2020, 1, 1): 100.0, date(2020, 6, 1): 150.0, date(2021, 1, 1): 200.0}
    assert nearest_value_lookup(series, date(2020, 8, 1)) == 150.0


def test_nearest_value_lookup_exact_match():
    series = {date(2020, 1, 1): 100.0, date(2020, 6, 1): 150.0}
    assert nearest_value_lookup(series, date(2020, 6, 1)) == 150.0


def test_nearest_value_lookup_never_uses_a_future_value():
    # Only a later entry exists relative to target_date - must return
    # None, not leak the future value backward.
    series = {date(2021, 1, 1): 200.0}
    assert nearest_value_lookup(series, date(2020, 1, 1)) is None


def test_nearest_value_lookup_respects_tolerance():
    series = {date(2015, 1, 1): 100.0}
    # Nearest eligible point is >5 years before target - too stale to trust.
    assert nearest_value_lookup(series, date(2020, 1, 1), max_days_tolerance=365) is None
    # Same series, no tolerance specified - returns the stale match anyway.
    assert nearest_value_lookup(series, date(2020, 1, 1)) == 100.0


def test_nearest_value_lookup_empty_series():
    assert nearest_value_lookup({}, date(2020, 1, 1)) is None


# --- Decision 0012: the shares-lookup tolerance is a real cap, not 400 ---
#
# nearest_value_lookup's own tolerance mechanism was already correct and
# already tested above (never_uses_a_future_value covers "target predates
# all data"; respects_tolerance covers "nearest point is too stale"). The
# actual gap was a caller (the backtest pilot scripts) passing
# max_days_tolerance=400 for shares-outstanding lookups - not a
# meaningful bound for annual rebalancing, since it would silently accept
# a share count over a year stale. A first revision widened this to a
# named 180-day constant reasoning from disclosure-cycle length alone;
# re-checked against the real measured gap distribution (200 companies,
# 166,647 gaps: median 1 day, p99 16 days) and tightened to 90 - still
# ~5.6x the measured p99, comfortably not "the raw cycle length itself"
# the way 180 was reasoned, but not left looser than the evidence
# supports either. These tests pin the real, named constant, so a future
# change back to something too loose fails a test instead of quietly
# reintroducing the stale-value risk.


def test_shares_lookup_tolerance_constant_is_not_the_old_400_day_value():
    assert SHARES_LOOKUP_MAX_TOLERANCE_DAYS == 90
    assert SHARES_LOOKUP_MAX_TOLERANCE_DAYS < 400


def test_nearest_value_lookup_with_shares_tolerance_rejects_a_stale_share_count():
    # A share count last confirmed 200 days before the rebalance date -
    # comfortably past a real disclosure gap, and past the 90-day bound -
    # must not be silently treated as this rebalance's point-in-time
    # value.
    series = {date(2020, 1, 1): 1_000_000.0}
    target = date.fromordinal(date(2020, 1, 1).toordinal() + 200)
    assert nearest_value_lookup(series, target, max_days_tolerance=SHARES_LOOKUP_MAX_TOLERANCE_DAYS) is None


def test_nearest_value_lookup_with_shares_tolerance_accepts_a_normal_disclosure_gap():
    # A share count confirmed 60 days before the rebalance date - well
    # within the measured normal gap distribution (p99 16 days) plus
    # real slack for one unusually slow disclosure - should still be
    # usable, not needlessly excluded.
    series = {date(2020, 1, 1): 1_000_000.0}
    target = date.fromordinal(date(2020, 1, 1).toordinal() + 60)
    assert nearest_value_lookup(series, target, max_days_tolerance=SHARES_LOOKUP_MAX_TOLERANCE_DAYS) == 1_000_000.0


# --- compute_point_in_time_market_cap -------------------------------------


def test_compute_point_in_time_market_cap():
    assert compute_point_in_time_market_cap(price=100.0, shares_outstanding=1_000.0) == 100_000.0


def test_compute_point_in_time_market_cap_missing_price_is_none_not_zero():
    assert compute_point_in_time_market_cap(price=None, shares_outstanding=1_000.0) is None


def test_compute_point_in_time_market_cap_missing_shares_is_none_not_zero():
    assert compute_point_in_time_market_cap(price=100.0, shares_outstanding=None) is None


# --- filter_persisted_values (decision 0008) ------------------------------


def _daily_series(start: date, values: list[float]) -> dict[date, float]:
    """One entry per consecutive day, in the shape get_shares_full() returns."""
    return {date.fromordinal(start.toordinal() + i): v for i, v in enumerate(values)}


def test_filter_persisted_values_drops_isolated_single_day_spike():
    # AAKASH's actual pattern: a stable post-split baseline (101,250,000)
    # with one day's reading wildly off (the real series' 111,989,000,
    # which produced 0007's inflated "18x" figure), reverting immediately.
    baseline = 101_250_000.0
    values = [baseline] * 10
    values[5] = 111_989_000.0  # single-day spike, no real event behind it
    series = _daily_series(date(2022, 10, 28), values)

    cleaned = filter_persisted_values(series)

    spike_date = date.fromordinal(date(2022, 10, 28).toordinal() + 5)
    assert spike_date not in cleaned
    assert len(cleaned) == 9
    assert all(v == baseline for v in cleaned.values())


def test_filter_persisted_values_drops_two_day_spike_that_does_not_persist():
    # A harder case than a single isolated day: two consecutive days carry
    # a similar wrong value (so they "confirm" each other once), then the
    # series reverts - mirrors THOMASCOTT's real two-entry spike before
    # same-date dedup collapses it. Mutual self-confirmation between the
    # two spike days must not be enough to pass min_confirmations=2 - a
    # transient blip has to actually persist, not just repeat twice.
    baseline = 11_295_200.0
    values = [baseline] * 6 + [3_826_259_968.0, 3_827_820_032.0] + [baseline] * 6
    series = _daily_series(date(2024, 9, 11), values)

    cleaned = filter_persisted_values(series)

    assert date(2024, 9, 17) not in cleaned
    assert date(2024, 9, 18) not in cleaned
    assert all(v == baseline for v in cleaned.values())


def test_filter_persisted_values_keeps_genuine_sustained_split():
    # KOTAKBANK's real pattern: a stable pre-split baseline, then a real
    # 5:1 split that persists for every subsequent observation. The
    # transition itself is just as large a jump as a noise spike - the
    # filter must keep it because, unlike noise, it never reverts.
    pre = 1_988_249_984.0
    post = 9_941_249_920.0  # ~5x, the new sustained level
    values = [pre] * 5 + [post] * 5
    series = _daily_series(date(2026, 1, 10), values)

    cleaned = filter_persisted_values(series)

    assert len(cleaned) == len(series)
    assert cleaned[date(2026, 1, 15)] == post  # the split-day reading itself


def test_filter_persisted_values_keeps_small_jitter_within_tolerance():
    # SAIL's real pattern: ~1% day-to-day wobble around a stable value,
    # with no corporate action behind it at all. Below tolerance_pct
    # (default 2%), so none of it should be treated as an outlier.
    import random

    random.seed(0)
    baseline = 4_130_530_048.0
    values = [baseline * (1 + random.uniform(-0.01, 0.01)) for _ in range(10)]
    series = _daily_series(date(2024, 1, 1), values)

    cleaned = filter_persisted_values(series)

    assert len(cleaned) == len(series)


def test_filter_persisted_values_dict_input_has_no_duplicate_dates_to_resolve():
    # get_shares_full() can return two rows for the same calendar date
    # (its DatetimeIndex isn't always unique); building a dict[date,
    # float] from that series - the input shape this function requires -
    # already collapses such duplicates to the later value via ordinary
    # dict construction, before filter_persisted_values ever sees it.
    d = date(2024, 9, 17)
    raw_rows = [(d, 3_826_259_968.0), (d, 3_827_820_032.0)]
    series = dict(raw_rows)  # later entry for the same key wins
    assert series[d] == 3_827_820_032.0


def test_filter_persisted_values_empty_series():
    assert filter_persisted_values({}) == {}


def test_filter_persisted_values_composes_with_nearest_value_lookup():
    # The intended usage: a noisy raw series is cleaned once, then the
    # existing nearest_value_lookup runs on the cleaned result. A lookup
    # dated on the spike day must silently fall back to the last
    # confirmed value, not the discarded spike, and not a synthesized
    # interpolation - proving the "discard, don't interpolate" design.
    baseline = 101_250_000.0
    values = [baseline] * 10
    spike_date = date(2022, 11, 2)
    values[5] = 111_989_000.0
    series = _daily_series(date(2022, 10, 28), values)
    assert list(series.keys())[5] == spike_date

    cleaned = filter_persisted_values(series)

    assert nearest_value_lookup(cleaned, spike_date) == baseline
