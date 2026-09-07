"""Unit tests for magicformula.data_fetch.fundamentals.

Five layers, matching the module's own structure:
  1. Parser, against a real saved HTML fixture (no network at test time).
  2. ScreenerClient's cache/retry behavior, with the network mocked out.
  3. The anomaly detector and its three-tier peer-group fallback -
     synthetic tests for the core mechanics, then real-data tests using
     Ashok Leyland and Coal India, the two cases that motivated the
     checks (see docs/decisions/0001-fundamentals-current-liability-
     split.md), each alongside real, verified-clean peers.
  4. Extraction failure handling: retry-then-log policy and the
     failed_extractions.csv output.
  5. Authoritative sector-based exclusion (decision 0004): a company whose
     real Broad Sector is Financial Services/Utilities gets excluded after
     exactly one fetch, even when magicformula.universe's pre-fetch name
     heuristic missed it - REC Limited is the real case that motivated
     this (a well-known NBFC whose registered name carries no
     financial-sounding word at all).
"""

from __future__ import annotations

import csv
from datetime import date
from pathlib import Path
from unittest.mock import MagicMock

import pytest
import requests

from magicformula.data_fetch.fundamentals import (
    FundamentalsRecord,
    ScreenerClient,
    SectorExcluded,
    apply_anomaly_filter,
    check_fundamentals_anomalies,
    classify_failure_reason,
    company_url,
    fetch_fundamentals,
    fetch_universe_fundamentals,
    is_financial_or_utility_sector,
    parse_company_html,
    parse_sector_tags,
    write_failed_extractions,
)
from magicformula.data_fetch.fundamentals import AnomalyCheckResult, FailedExtraction

FIXTURES_DIR = Path(__file__).parent / "fixtures" / "screener_html"


# --- company_url ------------------------------------------------------
#
# Regression coverage for a real bug: screener.in has no "/standalone/"
# URL segment at all (confirmed live: "/company/COLPAL/standalone/" is
# 404, "/company/COLPAL/" - no segment - is 200 with the real standalone
# data). Building the fallback URL as "/company/<SYM>/standalone/"
# silently 404s for every company that needs the fallback - caught only
# once a live pilot run showed every fallback attempt failing. See
# docs/decisions/0003 (fallback introduced) - this bug meant it never
# actually worked until fixed here.


def test_company_url_consolidated_has_explicit_segment():
    assert company_url("TCS", "consolidated") == "https://www.screener.in/company/TCS/consolidated/"


def test_company_url_standalone_has_no_statement_segment():
    assert company_url("TCS", "standalone") == "https://www.screener.in/company/TCS/"


def test_company_url_rejects_unknown_statement():
    with pytest.raises(ValueError):
        company_url("TCS", "quarterly")


# --- Parser (real HTML fixture, no network) -------------------------------
#
# Regression coverage for a real bug found in the corrected pilot
# (docs/decisions/0005): Siemens Ltd changed its fiscal year end
# (September -> March), producing an 18-month stub/transition period
# column labeled "Mar 202618m" (no space before "18m"). The year parser
# originally split on whitespace and fed "202618m" straight to int(),
# raising "invalid literal for int()". Fixed to take exactly the first 4
# digits of the year token.


def test_parse_company_html_matches_differently_labeled_stub_period_across_tables():
    # Siemens' fiscal-year-change stub period is labeled "Mar 202618m" in
    # the P&L header but plain "Mar 2026" in the balance sheet header -
    # matching by parsed date (not raw label text or column position) is
    # what lets these be recognized as the same period and combined into
    # one record, rather than either being dropped (label mismatch) or
    # miscounted (treated as two different, unmatched years).
    html = (FIXTURES_DIR / "SIEMENS.html").read_text(encoding="utf-8")
    records = parse_company_html(
        html, "SIEMENS", "https://www.screener.in/company/SIEMENS/consolidated/"
    )

    # 11 ordinary Sep-ending years (2014-2024) + 1 stub period ending Mar
    # 2026 = 12, not 11 (stub dropped) or 13 (stub double-counted).
    assert len(records) == 12

    earliest = records[0]
    assert earliest.fiscal_year_end == date(2014, 9, 30)
    assert earliest.operating_profit == 119
    assert earliest.depreciation_amortization == 229

    stub_period = records[-1]
    assert stub_period.fiscal_year_end == date(2026, 3, 31)
    assert stub_period.operating_profit == 2863
    assert stub_period.depreciation_amortization == 415
    assert stub_period.total_assets == 21213
    assert stub_period.other_liabilities == 7066


def test_parse_company_html_extracts_full_multi_year_history_for_tcs():
    # TCS's real page shows Mar 2015 through Mar 2026: 12 fiscal years -
    # this is the actual point of the rework, so check the count and both
    # ends of the range, not just the latest year (which was all the old,
    # single-record parser ever surfaced or was ever tested against).
    html = (FIXTURES_DIR / "TCS.html").read_text(encoding="utf-8")
    records = parse_company_html(html, "TCS", "https://www.screener.in/company/TCS/consolidated/")

    assert len(records) == 12
    assert [r.fiscal_year_end for r in records] == [
        date(y, 3, 31) for y in range(2015, 2027)
    ]  # oldest first

    earliest, latest = records[0], records[-1]

    assert earliest.fiscal_year_end == date(2015, 3, 31)
    assert earliest.operating_profit == 24482
    assert earliest.depreciation_amortization == 1799
    assert earliest.total_assets == 73318
    assert earliest.other_liabilities == 22325
    # as_of_date is per-record, not one shared date reused across years -
    # this is the actual point-in-time-embargo prerequisite the rework
    # exists for (SPEC.md section 6).
    assert earliest.as_of_date == date(2015, 5, 30)

    assert latest.symbol == "TCS"
    assert latest.sector == "Information Technology"
    assert latest.broad_sector == "Information Technology"
    assert latest.fiscal_year_end == date(2026, 3, 31)
    assert latest.operating_profit == 72398
    assert latest.depreciation_amortization == 5560
    assert latest.total_assets == 181167
    assert latest.other_liabilities == 62644
    assert latest.investments == 33988
    assert latest.cwip == 2665
    assert latest.borrowings == 11283
    assert latest.as_of_date == date(2026, 5, 30)

    # market_cap is screener's current figure - same value on every
    # historical record by construction (see FundamentalsRecord's
    # docstring caveat), not something to expect variation in here.
    assert all(r.market_cap == 833607 for r in records)
    assert all(r.cap_bucket is None for r in records)  # not populated by this module alone
    assert all(r.anomaly_reasons == [] for r in records)


# --- ScreenerClient (network mocked) --------------------------------------


def test_screener_client_uses_cache_without_hitting_network(tmp_path):
    cache_file = tmp_path / "TCS_consolidated.html"
    cache_file.write_text("<html>cached</html>", encoding="utf-8")

    client = ScreenerClient(cache_dir=tmp_path, rate_limit_seconds=0.0)
    client.session.get = MagicMock(side_effect=AssertionError("should not hit network"))

    html = client.fetch_html("TCS")

    assert html == "<html>cached</html>"
    client.session.get.assert_not_called()


def test_screener_client_writes_cache_on_live_fetch(tmp_path):
    client = ScreenerClient(cache_dir=tmp_path, rate_limit_seconds=0.0)
    response = MagicMock(text="<html>live</html>")
    response.raise_for_status = MagicMock()
    client.session.get = MagicMock(return_value=response)

    html = client.fetch_html("INFY")

    assert html == "<html>live</html>"
    assert (tmp_path / "INFY_consolidated.html").read_text(encoding="utf-8") == "<html>live</html>"


def test_screener_client_retries_then_succeeds(tmp_path):
    client = ScreenerClient(cache_dir=tmp_path, rate_limit_seconds=0.0, max_retries=3)
    ok_response = MagicMock(text="<html>ok</html>")
    ok_response.raise_for_status = MagicMock()
    client.session.get = MagicMock(
        side_effect=[requests.ConnectionError("boom"), requests.ConnectionError("boom"), ok_response]
    )

    html = client.fetch_html("ITC")

    assert html == "<html>ok</html>"
    assert client.session.get.call_count == 3


def test_screener_client_raises_after_exhausting_retries(tmp_path):
    client = ScreenerClient(cache_dir=tmp_path, rate_limit_seconds=0.0, max_retries=2)
    client.session.get = MagicMock(side_effect=requests.ConnectionError("boom"))

    with pytest.raises(RuntimeError):
        client.fetch_html("ITC")

    assert client.session.get.call_count == 2


# --- Anomaly detector: synthetic mechanics ---------------------------------


def _record(
    symbol: str,
    sector: str | None,
    operating_profit: float,
    depreciation: float,
    total_assets: float,
    other_liabilities: float,
    broad_sector: str | None = None,
    cap_bucket: str | None = None,
) -> FundamentalsRecord:
    return FundamentalsRecord(
        symbol=symbol,
        sector=sector,
        broad_sector=broad_sector,
        fiscal_year_end=date(2026, 3, 31),
        operating_profit=operating_profit,
        depreciation_amortization=depreciation,
        total_assets=total_assets,
        other_liabilities=other_liabilities,
        investments=0.0,
        cwip=0.0,
        borrowings=0.0,
        market_cap=1.0,
        source_url="https://example.invalid",
        cap_bucket=cap_bucket,
    )


def test_check_skips_when_no_tier_clears_min_size():
    # Only 2 records total, no broad_sector/cap_bucket given either -
    # every tier stays below min_sector_size=3, so nothing is flagged.
    records = [
        _record("A", "TINY", operating_profit=1000, depreciation=0, total_assets=100_000, other_liabilities=0),
        _record("B", "TINY", operating_profit=100, depreciation=0, total_assets=100, other_liabilities=0),
    ]
    results = check_fundamentals_anomalies(records, min_sector_size=3)
    assert all(not r.flagged for r in results)
    assert all(r.total_assets_to_ebit_peer_tier == "none" for r in results)


def test_check_flags_high_side_outlier_by_construction():
    # 4 peers with TA/EBIT around 10; one outlier at 100 (10x the others).
    records = [
        _record("NORMAL1", "SEC", operating_profit=100, depreciation=0, total_assets=1000, other_liabilities=0),
        _record("NORMAL2", "SEC", operating_profit=100, depreciation=0, total_assets=1000, other_liabilities=0),
        _record("NORMAL3", "SEC", operating_profit=100, depreciation=0, total_assets=1000, other_liabilities=0),
        _record("OUTLIER", "SEC", operating_profit=100, depreciation=0, total_assets=10_000, other_liabilities=0),
    ]
    results = {r.symbol: r for r in check_fundamentals_anomalies(records, min_sector_size=3)}
    assert results["OUTLIER"].flagged
    assert "Total Assets/EBIT" in results["OUTLIER"].reasons[0]
    assert results["OUTLIER"].total_assets_to_ebit_peer_tier == "sector"
    assert not results["NORMAL1"].flagged


def test_check_flags_low_side_outlier_too():
    records = [
        _record("NORMAL1", "SEC", operating_profit=100, depreciation=0, total_assets=1000, other_liabilities=0),
        _record("NORMAL2", "SEC", operating_profit=100, depreciation=0, total_assets=1000, other_liabilities=0),
        _record("NORMAL3", "SEC", operating_profit=100, depreciation=0, total_assets=1000, other_liabilities=0),
        _record("TINYRATIO", "SEC", operating_profit=100, depreciation=0, total_assets=10, other_liabilities=0),
    ]
    results = {r.symbol: r for r in check_fundamentals_anomalies(records, min_sector_size=3)}
    assert results["TINYRATIO"].flagged


def test_check_ignores_non_positive_ebit_for_ta_ebit_ratio():
    records = [
        _record("NORMAL1", "SEC", operating_profit=100, depreciation=0, total_assets=1000, other_liabilities=0),
        _record("NORMAL2", "SEC", operating_profit=100, depreciation=0, total_assets=1000, other_liabilities=0),
        _record("NORMAL3", "SEC", operating_profit=100, depreciation=0, total_assets=1000, other_liabilities=0),
        _record("NEGATIVE", "SEC", operating_profit=50, depreciation=100, total_assets=1000, other_liabilities=0),
    ]
    results = {r.symbol: r for r in check_fundamentals_anomalies(records, min_sector_size=3)}
    assert results["NEGATIVE"].total_assets_to_ebit is None
    assert not results["NEGATIVE"].flagged


# --- Anomaly detector: three-tier peer-group fallback ----------------------


def test_check_falls_back_to_broad_sector_when_sector_has_too_few_peers():
    # THINSECTOR has only 2 members (below min_sector_size=3) - TARGET's
    # own "Sector" tier can't be trusted. Its "Broad Sector" (WIDER) pools
    # with 3 other companies whose narrow Sector tags differ from
    # TARGET's and from each other, giving 4 members - enough to resolve
    # at tier 2. TARGET's ratio (100) is a clear outlier against that
    # wider group's ratios (all ~10).
    records = [
        _record("TARGET", "THINSECTOR", operating_profit=100, depreciation=0,
                total_assets=10_000, other_liabilities=0, broad_sector="WIDER"),
        _record("THINPEER", "THINSECTOR", operating_profit=100, depreciation=0,
                total_assets=1_000, other_liabilities=0, broad_sector="WIDER"),
        _record("OTHER1", "OTHERSECTOR1", operating_profit=100, depreciation=0,
                total_assets=1_000, other_liabilities=0, broad_sector="WIDER"),
        _record("OTHER2", "OTHERSECTOR2", operating_profit=100, depreciation=0,
                total_assets=1_000, other_liabilities=0, broad_sector="WIDER"),
    ]
    results = {r.symbol: r for r in check_fundamentals_anomalies(records, min_sector_size=3)}

    assert results["TARGET"].total_assets_to_ebit_peer_tier == "broad_sector"
    assert results["TARGET"].total_assets_to_ebit_peer_group == "WIDER"
    assert results["TARGET"].flagged
    # THINPEER also only has 2 Sector-tier peers and falls back too, but
    # its own ratio isn't an outlier against the WIDER group.
    assert results["THINPEER"].total_assets_to_ebit_peer_tier == "broad_sector"
    assert not results["THINPEER"].flagged


def test_check_falls_back_to_cap_bucket_when_sector_and_broad_sector_both_too_thin():
    # TARGET's Sector (n=2) and Broad Sector (still n=2, no wider pooling
    # available) both fall short of min_sector_size=3. Only cap_bucket
    # ("large", shared with two unrelated companies) clears the bar.
    records = [
        _record("TARGET", "THINSECTOR", operating_profit=100, depreciation=0,
                total_assets=10_000, other_liabilities=0, broad_sector="THINBROAD",
                cap_bucket="large"),
        _record("SECTORPEER", "THINSECTOR", operating_profit=100, depreciation=0,
                total_assets=1_000, other_liabilities=0, broad_sector="THINBROAD",
                cap_bucket="large"),
        _record("UNRELATED1", "SOMETHINGELSE", operating_profit=100, depreciation=0,
                total_assets=1_000, other_liabilities=0, broad_sector="ANOTHERBROAD",
                cap_bucket="large"),
        _record("UNRELATED2", "YETANOTHER", operating_profit=100, depreciation=0,
                total_assets=1_000, other_liabilities=0, broad_sector="STILLDIFFERENT",
                cap_bucket="large"),
    ]
    results = {r.symbol: r for r in check_fundamentals_anomalies(records, min_sector_size=3)}

    assert results["TARGET"].total_assets_to_ebit_peer_tier == "cap_bucket"
    assert results["TARGET"].total_assets_to_ebit_peer_group == "large"
    assert results["TARGET"].flagged


# --- apply_anomaly_filter --------------------------------------------------


def test_apply_anomaly_filter_excludes_by_default():
    records = [_record("A", "SEC", 100, 0, 1000, 0), _record("B", "SEC", 100, 0, 1000, 0)]
    results = [
        AnomalyCheckResult("A", True, ["bad"], 1.0, "SEC", "sector", 1.0, None, None, "none", None),
        AnomalyCheckResult("B", False, [], 1.0, "SEC", "sector", 1.0, None, None, "none", None),
    ]
    kept = apply_anomaly_filter(records, results, mode="exclude")
    assert [r.symbol for r in kept] == ["B"]


def test_apply_anomaly_filter_flag_only_keeps_everything_but_annotates():
    records = [_record("A", "SEC", 100, 0, 1000, 0), _record("B", "SEC", 100, 0, 1000, 0)]
    results = [
        AnomalyCheckResult("A", True, ["bad"], 1.0, "SEC", "sector", 1.0, None, None, "none", None),
        AnomalyCheckResult("B", False, [], 1.0, "SEC", "sector", 1.0, None, None, "none", None),
    ]
    kept = apply_anomaly_filter(records, results, mode="flag_only")
    assert [r.symbol for r in kept] == ["A", "B"]
    assert kept[0].anomaly_reasons == ["bad"]
    assert kept[1].anomaly_reasons == []


# --- Real-data validation: Ashok Leyland and Coal India -------------------
#
# All figures below are real, from screener.in consolidated financials,
# FY Mar 2026, the same source as tests/fixtures/real_companies.py and
# scripts/roce_bucketing_correlation_check.py - not invented for the test.


def test_ashok_leyland_flagged_by_real_sector_peers_on_total_assets_to_ebit():
    # Real screener.in "Sector" tag for all six: "Capital Goods" (n=10 in
    # the wider 64-company sample - well above min_sector_size, so this
    # resolves at tier 1, no fallback needed). Ashok Leyland's
    # consolidated financials include a captive NBFC subsidiary (Hinduja
    # Leyland Finance), inflating Total Assets against EBIT far past its
    # own genuine industrial peers - not a synthetic scenario.
    #
    # Two of the ten real Capital Goods members (Graphite India, HEG)
    # also trip this check in the full sample, for an unrelated, legitimate
    # reason (a genuine cyclical earnings trough in graphite electrodes,
    # not a data-quality problem) - excluded from this test's peer set so
    # it isolates the case this check exists for, not because they're
    # inconvenient.
    records = [
        _record("ASHOKLEY", "Capital Goods", operating_profit=2697, depreciation=1138,
                total_assets=100_704, other_liabilities=22_527),
        _record("ESCORTS", "Capital Goods", operating_profit=1496, depreciation=255,
                total_assets=15_792, other_liabilities=3_257),
        _record("RATNAMANI", "Capital Goods", operating_profit=758, depreciation=132,
                total_assets=5_385, other_liabilities=957),
        _record("GRINDWELL", "Capital Goods", operating_profit=576, depreciation=105,
                total_assets=3_436, other_liabilities=840),
        _record("CARBORUNIV", "Capital Goods", operating_profit=582, depreciation=247,
                total_assets=5_271, other_liabilities=959),
        _record("TIMKEN", "Capital Goods", operating_profit=632, depreciation=106,
                total_assets=3_699, other_liabilities=770),
    ]

    results = {r.symbol: r for r in check_fundamentals_anomalies(records)}

    assert results["ASHOKLEY"].flagged
    assert results["ASHOKLEY"].total_assets_to_ebit_peer_tier == "sector"
    assert "Total Assets/EBIT" in results["ASHOKLEY"].reasons[0]
    for clean_peer in ["ESCORTS", "RATNAMANI", "GRINDWELL", "CARBORUNIV", "TIMKEN"]:
        assert not results[clean_peer].flagged, f"{clean_peer} should not be flagged"


def test_coal_india_resolves_via_cap_bucket_fallback_without_a_hand_picked_override():
    # Coal India's real screener.in "Sector" tag is "Oil, Gas & Consumable
    # Fuels" - only 2 peers in the 64-company sample (Reliance, ONGC),
    # below min_sector_size. Its "Broad Sector" ("Energy") doesn't help
    # either - same 2 peers, no wider pooling available in-sample. This
    # is what motivated the fallback hierarchy in the first place: with
    # only Reliance and ONGC as tier-1/tier-2 peers, Reliance's own
    # diversified-conglomerate Other Liabilities ratio pulls the would-be
    # median up enough to mask Coal India's anomaly entirely (see
    # docs/decisions/0001-fundamentals-current-liability-split.md).
    #
    # Rather than hand-picking "Metals & Mining" as an override (the
    # previous approach), this test lets the hierarchy fall all the way to
    # cap_bucket ("large") - real cap-bucket peers from the same 64-company
    # sample, no manual sector reassignment.
    records = [
        _record("COALINDIA", "Oil, Gas & Consumable Fuels", operating_profit=37_172, depreciation=10_137,
                total_assets=283_956, other_liabilities=150_782,
                broad_sector="Energy", cap_bucket="large"),
        _record("RELIANCE", "Oil, Gas & Consumable Fuels", operating_profit=179_065, depreciation=57_688,
                total_assets=2_177_546, other_liabilities=870_554,
                broad_sector="Energy", cap_bucket="large"),
        # Real "large" cap_bucket peers, unrelated narrow sectors, giving
        # tier 3 (cap_bucket) enough members once tiers 1 and 2 fall short.
        _record("TCS", "Information Technology", operating_profit=72_398, depreciation=5_560,
                total_assets=181_167, other_liabilities=62_644,
                broad_sector="Information Technology", cap_bucket="large"),
        _record("INFY", "Information Technology", operating_profit=42_280, depreciation=4_902,
                total_assets=154_288, other_liabilities=52_260,
                broad_sector="Information Technology", cap_bucket="large"),
        _record("HINDALCO", "Metals & Mining", operating_profit=34_880, depreciation=8_830,
                total_assets=344_681, other_liabilities=108_933,
                broad_sector="Commodities", cap_bucket="large"),
        _record("MARUTI", "Automobile and Auto Components", operating_profit=21_530, depreciation=6_742,
                total_assets=148_880, other_liabilities=41_622,
                broad_sector="Consumer Discretionary", cap_bucket="large"),
        _record("ITC", "Fast Moving Consumer Goods", operating_profit=27_306, depreciation=1_711,
                total_assets=93_637, other_liabilities=18_731,
                broad_sector="Fast Moving Consumer Goods", cap_bucket="large"),
        _record("ASIANPAINT", "Consumer Durables", operating_profit=6_700, depreciation=1_229,
                total_assets=34_519, other_liabilities=9_218,
                broad_sector="Consumer Discretionary", cap_bucket="large"),
    ]

    results = {r.symbol: r for r in check_fundamentals_anomalies(records)}

    assert results["COALINDIA"].other_liabilities_to_capital_employed_peer_tier == "cap_bucket"
    assert results["COALINDIA"].other_liabilities_to_capital_employed_peer_group == "large"
    assert results["COALINDIA"].flagged
    assert "Other Liabilities/Capital Employed" in results["COALINDIA"].reasons[0]
    # Reliance shares Coal India's narrow (and broad) sector, but on the
    # ratio this test is about (Other Liabilities/Capital Employed) it
    # reads as unremarkable once compared against the real large-cap pool
    # - it does separately trip Total Assets/EBIT in this small 8-company
    # pool (a real, different finding: Reliance's refining+telecom+retail
    # conglomerate structure is itself asset-heavy relative to a pool with
    # two very asset-light IT names in it), which isn't what this test is
    # checking and isn't a bug.
    assert not any(
        "Other Liabilities/Capital Employed" in r for r in results["RELIANCE"].reasons
    )
    # And Coal India's Total Assets/EBIT is unremarkable here too - only
    # the liability-composition ratio is the real anomaly.
    assert not any("Total Assets/EBIT" in r for r in results["COALINDIA"].reasons)


# --- Anomaly detector: IQR calibration and small-peer-group stability -----
# (decision 0010 - the two tests above, Ashok Leyland and Coal India, are
# the ground-truth cases this whole check exists to catch. Both were
# re-verified to still flag correctly under the log-scale IQR mechanism
# before it shipped - a threshold change that produces a nicer aggregate
# flag rate but loses either of those two real cases would be worse than
# the flat-multiple check it replaces, not better.)


def test_iqr_check_has_no_power_at_four_peers_but_catches_the_same_case_at_five():
    # Checked empirically before choosing min_sector_size=5 as the new
    # default (previously 3): log-scale IQR fences computed from only 3
    # peers with ordinary, non-identical natural spread can be too wide
    # to catch even a clear, real outlier - a mathematical property of
    # quartiles at very small n, not a bug in this specific data. These
    # exact figures (peers 6, 8, 13; outlier 40) were verified by direct
    # calculation to sit right on that edge: undetected with 3 peers,
    # caught once a 4th clean peer is added. This is the reason
    # min_sector_size was raised, encoded as a reproducible test rather
    # than left as a claim in a decision doc.
    def make(symbol, total_assets):
        return _record(symbol, "SEC", operating_profit=100, depreciation=0,
                        total_assets=total_assets, other_liabilities=0)

    thin_pool = [
        make("TARGET", 4000),  # ratio 40 - the real outlier either way
        make("PEER1", 600),    # ratio 6
        make("PEER2", 800),    # ratio 8
        make("PEER3", 1300),   # ratio 13
    ]
    results = {r.symbol: r for r in check_fundamentals_anomalies(thin_pool, min_sector_size=4)}
    assert results["TARGET"].total_assets_to_ebit_peer_tier == "sector"
    assert not results["TARGET"].flagged, (
        "with only 3 peers, IQR fences are too wide to catch this outlier - "
        "this is the instability min_sector_size=5 exists to avoid, not a "
        "regression if it starts failing (it would mean min_sector_size=4 "
        "became newly safe, which would need its own re-verification)"
    )

    wider_pool = thin_pool + [make("PEER4", 1000)]  # ratio 10, still a clean peer
    results = {r.symbol: r for r in check_fundamentals_anomalies(wider_pool, min_sector_size=5)}
    assert results["TARGET"].total_assets_to_ebit_peer_tier == "sector"
    assert results["TARGET"].flagged, (
        "the same outlier, against the same kind of peers, is caught once "
        "there are enough of them (n=5) for IQR fences to be trustworthy"
    )
    for clean_peer in ["PEER1", "PEER2", "PEER3", "PEER4"]:
        assert not results[clean_peer].flagged


def test_iqr_check_below_min_sector_size_falls_through_to_none_not_a_false_clear():
    # A peer group of exactly 4 (one below the default min_sector_size=5)
    # with no broader tier available at all must resolve to "none", not
    # silently compute IQR fences on too few points and report a
    # (falsely reassuring) "not flagged" - the two states are different:
    # "checked, looks fine" vs "couldn't check". Total_assets_to_ebit
    # (the ratio itself) is still populated - it's the *judgment* that's
    # withheld, not the underlying number.
    records = [
        _record("A", "SEC", operating_profit=100, depreciation=0, total_assets=1000, other_liabilities=0),
        _record("B", "SEC", operating_profit=100, depreciation=0, total_assets=1000, other_liabilities=0),
        _record("C", "SEC", operating_profit=100, depreciation=0, total_assets=1000, other_liabilities=0),
        _record("D", "SEC", operating_profit=100, depreciation=0, total_assets=100_000, other_liabilities=0),
    ]
    results = {r.symbol: r for r in check_fundamentals_anomalies(records)}  # default min_sector_size=5
    assert results["D"].total_assets_to_ebit_peer_tier == "none"
    assert results["D"].total_assets_to_ebit == 1000.0  # ratio computed, just not judged
    assert not results["D"].flagged


def test_check_does_not_crash_on_real_zero_other_liabilities_companies():
    # Real screener.in consolidated figures: EFCIL and TPHQ both have
    # Other Liabilities == 0 exactly (Total Liabilities = Equity +
    # Borrowings, no residual "other" bucket - an unusual but real small-
    # company balance sheet, not a data error). capital_employed is still
    # positive for both, so they passed the existing "capital_employed >
    # 0" pool-membership guard, but Other Liabilities/Capital Employed ==
    # 0 exactly - and math.log(0) crashed the pool's IQR computation with
    # ValueError before this fix, the first time a small-cap-inclusive
    # real run happened to include both of them alongside enough real
    # peers to reach min_sector_size. Five ordinary "SEC" peers with
    # positive ratios fill out the pool so it's large enough to resolve.
    records = [
        _record("EFCIL", "SEC", operating_profit=10, depreciation=0, total_assets=2, other_liabilities=0),
        _record("TPHQ", "SEC", operating_profit=10, depreciation=0, total_assets=4, other_liabilities=0),
        _record("PEER1", "SEC", operating_profit=100, depreciation=0, total_assets=1000, other_liabilities=200),
        _record("PEER2", "SEC", operating_profit=100, depreciation=0, total_assets=1000, other_liabilities=250),
        _record("PEER3", "SEC", operating_profit=100, depreciation=0, total_assets=1000, other_liabilities=300),
        _record("PEER4", "SEC", operating_profit=100, depreciation=0, total_assets=1000, other_liabilities=350),
        _record("PEER5", "SEC", operating_profit=100, depreciation=0, total_assets=1000, other_liabilities=400),
    ]
    results = {r.symbol: r for r in check_fundamentals_anomalies(records)}  # must not raise ValueError

    # EFCIL/TPHQ's own ratio (0.0) isn't judged against the pool - value
    # <= 0 is never flaggable by construction (_deviates' own guard) -
    # but the important thing is the *other* peers' bounds still compute.
    assert results["EFCIL"].other_liabilities_to_capital_employed == 0.0
    assert not results["EFCIL"].flagged
    assert results["PEER1"].other_liabilities_to_capital_employed_peer_tier == "sector"


# --- Extraction failure handling -------------------------------------------


def test_classify_failure_reason_no_page_anywhere():
    reason = "SIEMENS: failed to fetch standalone financials after 3 attempts"
    assert classify_failure_reason(reason) == "no_page_anywhere"


def test_classify_failure_reason_page_template_mismatch():
    reason = "HDFCBANK: could not locate annual P&L / balance sheet tables"
    assert classify_failure_reason(reason) == "page_template_mismatch"
    reason2 = "SOMECO: missing required financial statement rows"
    assert classify_failure_reason(reason2) == "page_template_mismatch"


def test_classify_failure_reason_falls_back_to_genuine_parse_error():
    # The real bug this category exists to catch: Siemens' fiscal-year-
    # change stub period ("Mar 202618m") crashing int() before the fix.
    reason = "invalid literal for int() with base 10: '202618m'"
    assert classify_failure_reason(reason) == "genuine_parse_error"


def test_write_failed_extractions_produces_expected_csv(tmp_path):
    failures = [
        FailedExtraction(symbol="COLPAL", statement="consolidated", attempts=3, reason="missing rows"),
        FailedExtraction(symbol="PAGEIND", statement="consolidated", attempts=3, reason="empty response"),
    ]
    path = tmp_path / "failed_extractions.csv"
    write_failed_extractions(failures, path)

    with path.open(newline="", encoding="utf-8") as f:
        rows = list(csv.reader(f))
    assert rows[0] == ["symbol", "statement", "attempts", "reason"]
    assert rows[1] == ["COLPAL", "consolidated", "3", "missing rows"]
    assert rows[2] == ["PAGEIND", "consolidated", "3", "empty response"]


def test_fetch_universe_fundamentals_retries_before_succeeding(tmp_path, monkeypatch):
    client = ScreenerClient(cache_dir=tmp_path / "cache", rate_limit_seconds=0.0)

    call_log = []

    def fake_fetch_fundamentals(symbol, client_arg, statement="consolidated", force_refresh=False):
        call_log.append((symbol, force_refresh))
        if symbol == "FLAKY" and len(call_log) < 2:
            raise ValueError("transient parse failure")
        return [FundamentalsRecord(
            symbol=symbol, sector="SEC", broad_sector="BROAD",
            fiscal_year_end=date(2026, 3, 31), operating_profit=100,
            depreciation_amortization=0, total_assets=1000, other_liabilities=0,
            investments=0, cwip=0, borrowings=0, market_cap=1.0,
            source_url="https://example.invalid",
        )]

    monkeypatch.setattr(
        "magicformula.data_fetch.fundamentals.fetch_fundamentals", fake_fetch_fundamentals
    )

    result = fetch_universe_fundamentals(["FLAKY"], client, extraction_max_retries=2)

    assert len(result.records) == 1
    assert result.failed_extractions == []
    # Second attempt (the successful one) must force a fresh fetch, not
    # reuse whatever the first, failed attempt might have cached.
    assert call_log == [("FLAKY", False), ("FLAKY", True)]


def test_fetch_universe_fundamentals_logs_and_reports_permanent_failures(tmp_path, monkeypatch):
    # Fails identically under both consolidated and standalone (e.g. the
    # bank-template case: no "Operating Profit" row exists under either
    # statement type) - the fallback can't rescue this, and both statement
    # types' attempts count toward the reported total.
    client = ScreenerClient(cache_dir=tmp_path / "cache", rate_limit_seconds=0.0)

    def always_fails(symbol, client_arg, statement="consolidated", force_refresh=False):
        raise ValueError("could not locate annual P&L / balance sheet tables")

    monkeypatch.setattr(
        "magicformula.data_fetch.fundamentals.fetch_fundamentals", always_fails
    )

    failed_path = tmp_path / "failed_extractions.csv"
    result = fetch_universe_fundamentals(
        ["HDFCBANK", "ICICIBANK"], client, extraction_max_retries=1, failed_extractions_path=failed_path
    )

    assert result.records == []
    assert result.total_symbols == 2
    assert {f.symbol for f in result.failed_extractions} == {"HDFCBANK", "ICICIBANK"}
    # 1 + extraction_max_retries per statement type, both statement types tried
    assert all(f.attempts == 4 for f in result.failed_extractions)
    assert all(f.statement == "consolidated+standalone" for f in result.failed_extractions)
    assert failed_path.exists()
    with failed_path.open(newline="", encoding="utf-8") as f:
        rows = list(csv.reader(f))
    assert len(rows) == 3  # header + 2 failures


def test_fetch_universe_fundamentals_falls_back_to_standalone_when_consolidated_is_unusable(
    tmp_path, monkeypatch
):
    # Real case this models: Colgate/Garden Reach Shipbuilders/etc. return
    # a 200 with an empty or stale-dated consolidated table - not a
    # network error, so plain retries on the same statement never help,
    # but standalone has the real numbers.
    client = ScreenerClient(cache_dir=tmp_path / "cache", rate_limit_seconds=0.0)
    calls = []

    def fake_fetch_fundamentals(symbol, client_arg, statement="consolidated", force_refresh=False):
        calls.append(statement)
        if statement == "consolidated":
            raise ValueError(f"{symbol}: could not locate annual P&L / balance sheet tables")
        return [FundamentalsRecord(
            symbol=symbol, sector="SEC", broad_sector="BROAD",
            fiscal_year_end=date(2026, 3, 31), operating_profit=100,
            depreciation_amortization=0, total_assets=1000, other_liabilities=0,
            investments=0, cwip=0, borrowings=0, market_cap=1.0,
            source_url="https://example.invalid", statement=statement,
        )]

    monkeypatch.setattr(
        "magicformula.data_fetch.fundamentals.fetch_fundamentals", fake_fetch_fundamentals
    )

    result = fetch_universe_fundamentals(["COLPAL"], client, extraction_max_retries=1)

    assert len(result.records) == 1
    assert result.records[0].statement == "standalone"
    assert result.failed_extractions == []
    # All consolidated attempts exhausted (1 + extraction_max_retries=1 -> 2)
    # before standalone was tried even once.
    assert calls == ["consolidated", "consolidated", "standalone"]


# --- Authoritative sector-based exclusion (decision 0004) ------------------
#
# REC Limited: a real, well-known NBFC. Its registered name ("REC
# Limited", formerly Rural Electrification Corporation) carries no
# financial-sounding word at all, so magicformula.universe's pre-fetch
# name heuristic misses it - this exclusion is the backstop.


def test_is_financial_or_utility_sector():
    assert is_financial_or_utility_sector("Financial Services") is True
    assert is_financial_or_utility_sector("Utilities") is True
    assert is_financial_or_utility_sector("Information Technology") is False
    assert is_financial_or_utility_sector(None) is False


def test_parse_sector_tags_real_rec_limited_is_financial_services():
    html = (FIXTURES_DIR / "RECLTD.html").read_text(encoding="utf-8")
    sector, broad_sector = parse_sector_tags(html)
    assert broad_sector == "Financial Services"
    assert is_financial_or_utility_sector(broad_sector)


def test_parse_sector_tags_real_tata_power_is_utilities():
    html = (FIXTURES_DIR / "TATAPOWER.html").read_text(encoding="utf-8")
    sector, broad_sector = parse_sector_tags(html)
    assert broad_sector == "Utilities"
    assert sector == "Power"
    assert is_financial_or_utility_sector(broad_sector)


def test_fetch_fundamentals_raises_sector_excluded_for_real_rec_limited(tmp_path):
    # Pre-seed the cache with the real fixture so no network call happens.
    cache_dir = tmp_path / "cache"
    cache_dir.mkdir()
    html = (FIXTURES_DIR / "RECLTD.html").read_text(encoding="utf-8")
    (cache_dir / "RECLTD_consolidated.html").write_text(html, encoding="utf-8")
    client = ScreenerClient(cache_dir=cache_dir)

    with pytest.raises(SectorExcluded) as exc_info:
        fetch_fundamentals("RECLTD", client)

    assert exc_info.value.symbol == "RECLTD"
    assert exc_info.value.broad_sector == "Financial Services"


def test_fetch_universe_fundamentals_routes_sector_excluded_after_exactly_one_fetch(
    tmp_path, monkeypatch
):
    client = ScreenerClient(cache_dir=tmp_path / "cache", rate_limit_seconds=0.0)
    calls = []

    def fake_fetch_fundamentals(symbol, client_arg, statement="consolidated", force_refresh=False):
        calls.append((symbol, statement))
        if symbol == "RECLTD":
            raise SectorExcluded("RECLTD", sector="Financial Services", broad_sector="Financial Services")
        return [FundamentalsRecord(
            symbol=symbol, sector="SEC", broad_sector="BROAD",
            fiscal_year_end=date(2026, 3, 31), operating_profit=100,
            depreciation_amortization=0, total_assets=1000, other_liabilities=0,
            investments=0, cwip=0, borrowings=0, market_cap=1.0,
            source_url="https://example.invalid",
        )]

    monkeypatch.setattr(
        "magicformula.data_fetch.fundamentals.fetch_fundamentals", fake_fetch_fundamentals
    )

    result = fetch_universe_fundamentals(["RECLTD", "TCS"], client, extraction_max_retries=2)

    assert [r.symbol for r in result.records] == ["TCS"]
    assert result.failed_extractions == []  # not a failure - a correct exclusion
    assert len(result.sector_excluded) == 1
    assert result.sector_excluded[0].symbol == "RECLTD"
    assert result.sector_excluded[0].broad_sector == "Financial Services"
    # Exactly one call for RECLTD: no retry (extraction_max_retries=2 would
    # otherwise allow 3), and no consolidated->standalone fallback attempt.
    assert calls.count(("RECLTD", "consolidated")) == 1
    assert not any(s == "RECLTD" and st == "standalone" for s, st in calls)


# --- Multi-year records feeding the anomaly check (decision 0006) --------


def test_fetch_universe_fundamentals_flags_company_by_latest_year_only(tmp_path, monkeypatch):
    # OUTLIER has two fiscal years: an older one that looks perfectly
    # normal (TA/EBIT ~10, same as its peers) and a latest one that's a
    # wild outlier (TA/EBIT ~100). If the anomaly check used OUTLIER's
    # *older* year (or averaged across years, or double-counted both as
    # separate "peers"), this would either miss the anomaly or corrupt the
    # sector median for everyone else. It must use only the latest year.
    client = ScreenerClient(cache_dir=tmp_path / "cache", rate_limit_seconds=0.0)

    def make_records(symbol, years_and_ebit_ratios):
        records = []
        for year, total_assets in years_and_ebit_ratios:
            records.append(
                FundamentalsRecord(
                    symbol=symbol, sector="SEC", broad_sector="BROAD",
                    fiscal_year_end=date(year, 3, 31), operating_profit=100,
                    depreciation_amortization=0, total_assets=total_assets, other_liabilities=0,
                    investments=0, cwip=0, borrowings=0, market_cap=1.0,
                    source_url="https://example.invalid",
                )
            )
        return records

    fixtures = {
        "NORMAL1": make_records("NORMAL1", [(2026, 1000)]),
        "NORMAL2": make_records("NORMAL2", [(2026, 1000)]),
        "NORMAL3": make_records("NORMAL3", [(2026, 1000)]),
        # A 4th clean peer - decision 0010 raised min_sector_size's default
        # to 5 (log-scale IQR has no real power below that, see
        # _resolve_peer_group's docstring), so the "SEC" pool needs 5
        # members (4 clean + OUTLIER) to resolve at the sector tier at all.
        "NORMAL4": make_records("NORMAL4", [(2026, 1000)]),
        "OUTLIER": make_records("OUTLIER", [(2024, 1000), (2026, 10_000)]),
    }

    def fake_fetch_fundamentals(symbol, client_arg, statement="consolidated", force_refresh=False):
        return fixtures[symbol]

    monkeypatch.setattr(
        "magicformula.data_fetch.fundamentals.fetch_fundamentals", fake_fetch_fundamentals
    )

    result = fetch_universe_fundamentals(
        ["NORMAL1", "NORMAL2", "NORMAL3", "NORMAL4", "OUTLIER"], client, extraction_max_retries=0,
    )

    # OUTLIER's *entire* history is excluded (both fiscal years), not just
    # its flagged latest year - a structural distortion found in the
    # current year is assumed to have applied to its prior years too.
    remaining_symbols = {r.symbol for r in result.records}
    assert remaining_symbols == {"NORMAL1", "NORMAL2", "NORMAL3", "NORMAL4"}
    assert sum(1 for r in result.records if r.symbol == "NORMAL1") == 1


def test_fetch_universe_fundamentals_cap_bucket_fallback_fires_end_to_end(monkeypatch, tmp_path):
    # Decision 0009: fetch_fundamentals() never sets cap_bucket (it comes
    # from universe.py, a separate concern - see the existing assertion at
    # the top of this file that every freshly-fetched record has
    # cap_bucket=None). Before this fix, callers set record.cap_bucket
    # only *after* fetch_universe_fundamentals() had already returned,
    # so the anomaly check's internal cap_bucket peer-tier fallback never
    # had real cap_bucket data available and could never fire - silently
    # unreachable, not merely unused, regardless of whether sector/
    # broad_sector coverage ever fell short. This test constructs exactly
    # that scenario (both narrower tiers too thin) and confirms passing
    # cap_bucket_by_symbol into fetch_universe_fundamentals is what makes
    # the fallback actually resolve, through the real integration path -
    # not just at the check_fundamentals_anomalies() unit level already
    # covered by test_check_falls_back_to_cap_bucket_when_sector_and_
    # broad_sector_both_too_thin.
    client = ScreenerClient(cache_dir=tmp_path / "cache", rate_limit_seconds=0.0)

    def make_one(symbol, sector, broad_sector, total_assets):
        return FundamentalsRecord(
            symbol=symbol, sector=sector, broad_sector=broad_sector,
            fiscal_year_end=date(2026, 3, 31), operating_profit=100,
            depreciation_amortization=0, total_assets=total_assets, other_liabilities=0,
            investments=0, cwip=0, borrowings=0, market_cap=1.0,
            source_url="https://example.invalid",
            # Deliberately NOT setting cap_bucket here - fetch_fundamentals()
            # never does, in reality; it must come from cap_bucket_by_symbol.
        )

    fixtures = {
        # TARGET's own sector (n=2) and broad_sector (n=2) both fall short
        # of the default min_sector_size=5 (decision 0010) - only
        # cap_bucket ("large", shared with three companies in unrelated
        # sectors, for a pool of 5 with TARGET itself) clears the bar.
        "TARGET": make_one("TARGET", "THINSECTOR", "THINBROAD", total_assets=10_000),
        "SECTORPEER": make_one("SECTORPEER", "THINSECTOR", "THINBROAD", total_assets=1_000),
        "UNRELATED1": make_one("UNRELATED1", "SOMETHINGELSE", "ANOTHERBROAD", total_assets=1_000),
        "UNRELATED2": make_one("UNRELATED2", "YETANOTHER", "STILLDIFFERENT", total_assets=1_000),
        "UNRELATED3": make_one("UNRELATED3", "ATHIRDONE", "ATHIRDBROAD", total_assets=1_000),
    }

    def fake_fetch_fundamentals(symbol, client_arg, statement="consolidated", force_refresh=False):
        return [fixtures[symbol]]

    monkeypatch.setattr(
        "magicformula.data_fetch.fundamentals.fetch_fundamentals", fake_fetch_fundamentals
    )

    cap_bucket_by_symbol = {
        "TARGET": "large", "SECTORPEER": "large",
        "UNRELATED1": "large", "UNRELATED2": "large", "UNRELATED3": "large",
    }

    result = fetch_universe_fundamentals(
        list(fixtures),
        client,
        extraction_max_retries=0,
        anomaly_mode="flag_only",
        cap_bucket_by_symbol=cap_bucket_by_symbol,
    )

    target = next(r for r in result.records if r.symbol == "TARGET")
    assert target.cap_bucket == "large"  # populated before the check ran, not after
    assert target.anomaly_reasons  # flagged
    assert "cap_bucket" in target.anomaly_reasons[0]
    assert "(large)" in target.anomaly_reasons[0]
