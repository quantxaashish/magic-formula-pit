"""Unit tests for magicformula.universe (SPEC.md section 1).

Fixtures under tests/fixtures/universe/ are small real slices of NSE's
EQUITY_L.csv, BSE's ListofScripData API response, and AMFI's own
categorization Excel file (fetched live, same as the rest of this
project's fixtures) - not synthetic data, so the parsers are exercised
against the real formats they'll actually see.
"""

from __future__ import annotations

import json
from datetime import date
from pathlib import Path

from magicformula.universe import (
    BseListing,
    NseListing,
    UniverseEntry,
    exclude_insufficient_listing_history,
    exclude_likely_financials_and_utilities,
    exclude_non_standard_instruments,
    merge_universe,
    parse_amfi_categorization,
    parse_bse_equity_json,
    parse_nse_equity_csv,
    tag_cap_buckets,
)

FIXTURES_DIR = Path(__file__).parent / "fixtures" / "universe"


# --- Parsers, against real fixtures ----------------------------------------


def test_parse_nse_equity_csv_extracts_known_rows():
    text = (FIXTURES_DIR / "nse_equity_sample.csv").read_text(encoding="utf-8")
    listings = parse_nse_equity_csv(text)

    by_symbol = {l.symbol: l for l in listings}
    assert by_symbol["TCS"].isin == "INE467B01029"
    assert by_symbol["TCS"].date_of_listing == date(2004, 8, 25)
    assert by_symbol["TCS"].series == "EQ"
    # A company listed only on NSE in this sample (not present in the BSE fixture).
    assert "AAKASH" in by_symbol
    # A genuinely recently-listed company, for the listing-history exclusion test.
    assert by_symbol["3BBLACKBIO"].date_of_listing == date(2026, 4, 20)


def test_parse_bse_equity_json_extracts_known_rows():
    data = json.loads((FIXTURES_DIR / "bse_equity_sample.json").read_text(encoding="utf-8"))
    listings = parse_bse_equity_json(data)

    by_isin = {l.isin: l for l in listings}
    assert by_isin["INE467B01029"].symbol == "TCS"
    assert by_isin["INE467B01029"].scrip_code == "532540"
    # A company listed only on BSE in this sample.
    assert any(l.name == "Andhra Petrochemicals Ltd" for l in listings)


def test_parse_amfi_categorization_matches_known_ranks():
    xlsx_bytes = (FIXTURES_DIR / "amfi_categorization_31dec2025.xlsx").read_bytes()
    categorization = parse_amfi_categorization(xlsx_bytes)

    # Reliance Industries is AMFI's #1 by average market cap in this edition.
    assert categorization["INE002A01018"] == (1, "large")
    # TCS and Infosys are both large-cap, single-digit ranks.
    rank, bucket = categorization["INE467B01029"]
    assert bucket == "large" and rank <= 100
    rank, bucket = categorization["INE009A01021"]
    assert bucket == "large" and rank <= 100


# --- Merge (ISIN-keyed, prefer NSE) -----------------------------------------


def test_merge_universe_prefers_nse_for_dual_listed_company():
    nse = [
        NseListing(
            symbol="TCS", name="Tata Consultancy Services Limited", series="EQ",
            date_of_listing=date(2004, 8, 25), isin="INE467B01029", face_value=1.0,
        )
    ]
    bse = [
        BseListing(scrip_code="532540", symbol="TCS", name="Tata Consultancy Services Ltd", isin="INE467B01029", group="A")
    ]

    entries = merge_universe(nse, bse)

    assert len(entries) == 1
    entry = entries[0]
    assert entry.primary_exchange == "NSE"
    assert entry.primary_symbol == "TCS"
    assert entry.nse_symbol == "TCS"
    assert entry.bse_symbol == "TCS"
    assert entry.bse_scrip_code == "532540"
    assert entry.date_of_listing == date(2004, 8, 25)


def test_merge_universe_keeps_nse_only_and_bse_only_entities():
    nse = [
        NseListing(symbol="NSEONLY", name="NSE Only Co", series="EQ",
                   date_of_listing=date(2020, 1, 1), isin="ISIN_NSE_ONLY", face_value=1.0)
    ]
    bse = [
        BseListing(scrip_code="999999", symbol="BSEONLY", name="BSE Only Co", isin="ISIN_BSE_ONLY", group="X")
    ]

    entries = {e.isin: e for e in merge_universe(nse, bse)}

    assert entries["ISIN_NSE_ONLY"].primary_exchange == "NSE"
    assert entries["ISIN_NSE_ONLY"].bse_symbol is None
    assert entries["ISIN_BSE_ONLY"].primary_exchange == "BSE"
    assert entries["ISIN_BSE_ONLY"].nse_symbol is None
    assert entries["ISIN_BSE_ONLY"].date_of_listing is None


def test_merge_universe_real_fixtures_end_to_end():
    nse = parse_nse_equity_csv((FIXTURES_DIR / "nse_equity_sample.csv").read_text(encoding="utf-8"))
    bse = parse_bse_equity_json(json.loads((FIXTURES_DIR / "bse_equity_sample.json").read_text(encoding="utf-8")))

    entries = {e.isin: e for e in merge_universe(nse, bse)}

    # 10 dual-listed + 1 NSE-only (AAKASH) + 1 NSE-only (3BBLACKBIO, no BSE
    # counterpart in this small sample) + 1 BSE-only (Andhra Petrochemicals)
    assert len(entries) == 13
    tcs = entries["INE467B01029"]
    assert tcs.primary_exchange == "NSE" and tcs.primary_symbol == "TCS" and tcs.bse_symbol == "TCS"


# --- AMFI cap-bucket tagging -------------------------------------------------


def test_tag_cap_buckets_sets_bucket_and_rank():
    entries = [
        UniverseEntry(
            isin="INE002A01018", name="Reliance Industries Limited",
            primary_exchange="NSE", primary_symbol="RELIANCE",
            nse_symbol="RELIANCE", bse_symbol="RELIANCE", bse_scrip_code="500325",
            date_of_listing=date(1995, 11, 29),
        ),
        UniverseEntry(
            isin="NOT_IN_AMFI", name="Some Untracked Micro-Cap",
            primary_exchange="NSE", primary_symbol="MICRO",
            nse_symbol="MICRO", bse_symbol=None, bse_scrip_code=None,
            date_of_listing=date(2020, 1, 1),
        ),
    ]
    amfi_by_isin = {"INE002A01018": (1, "large")}

    tag_cap_buckets(entries, amfi_by_isin)

    assert entries[0].cap_bucket == "large" and entries[0].cap_rank == 1
    assert entries[1].cap_bucket is None and entries[1].cap_rank is None


# --- Basic exclusions --------------------------------------------------------


def test_exclude_non_standard_instruments_flags_reit_and_invit_by_name():
    entries = [
        UniverseEntry(isin="A", name="Embassy Office Parks REIT", primary_exchange="NSE",
                      primary_symbol="EMBASSY", nse_symbol="EMBASSY", bse_symbol=None,
                      bse_scrip_code=None, date_of_listing=date(2019, 1, 1)),
        UniverseEntry(isin="B", name="India Grid Trust InvIT", primary_exchange="NSE",
                      primary_symbol="INDIGRID", nse_symbol="INDIGRID", bse_symbol=None,
                      bse_scrip_code=None, date_of_listing=date(2019, 1, 1)),
        UniverseEntry(isin="C", name="Tata Consultancy Services Limited", primary_exchange="NSE",
                      primary_symbol="TCS", nse_symbol="TCS", bse_symbol=None,
                      bse_scrip_code=None, date_of_listing=date(2004, 8, 25)),
    ]

    exclude_non_standard_instruments(entries)

    assert entries[0].excluded and "REIT/InvIT" in entries[0].exclusion_reason
    assert entries[1].excluded
    assert not entries[2].excluded


def test_exclude_non_standard_instruments_does_not_override_existing_exclusion():
    entry = UniverseEntry(
        isin="A", name="Some REIT Fund", primary_exchange="NSE", primary_symbol="X",
        nse_symbol="X", bse_symbol=None, bse_scrip_code=None, date_of_listing=date(2019, 1, 1),
        excluded=True, exclusion_reason="already excluded for another reason",
    )
    exclude_non_standard_instruments([entry])
    assert entry.exclusion_reason == "already excluded for another reason"


def test_exclude_non_standard_instruments_does_not_false_positive_on_substring_matches():
    # Real false positives found in a full-universe audit
    # (docs/decisions/0002): a dropped "-RE" marker matched "-Regular" fund
    # names and unrelated "-RE"-suffixed companies with zero real REIT/
    # InvIT catches to show for it, and plain substring "TRUST" matching
    # flagged "Trustwave Securities" (a securities firm, not a trust).
    entries = [
        UniverseEntry(isin="A", name="Viceroy Hotels Ltd-RE", primary_exchange="NSE",
                      primary_symbol="VICEROY", nse_symbol="VICEROY", bse_symbol=None,
                      bse_scrip_code=None, date_of_listing=date(2010, 1, 1)),
        UniverseEntry(isin="B", name="Infinity Hybrid Long-Short Fund-Regular-Growth",
                      primary_exchange="NSE", primary_symbol="INFHYB", nse_symbol="INFHYB",
                      bse_symbol=None, bse_scrip_code=None, date_of_listing=date(2010, 1, 1)),
        UniverseEntry(isin="C", name="Trustwave Securities Ltd", primary_exchange="NSE",
                      primary_symbol="TRUSTWAVE", nse_symbol="TRUSTWAVE", bse_symbol=None,
                      bse_scrip_code=None, date_of_listing=date(2010, 1, 1)),
    ]
    exclude_non_standard_instruments(entries)
    assert not any(e.excluded for e in entries)


def test_exclude_non_standard_instruments_still_catches_whole_word_trust_reit_invit():
    entries = [
        UniverseEntry(isin="A", name="Master Trust Limited", primary_exchange="NSE",
                      primary_symbol="MASTERTRUST", nse_symbol="MASTERTRUST", bse_symbol=None,
                      bse_scrip_code=None, date_of_listing=date(2010, 1, 1)),
        UniverseEntry(isin="B", name="Some REIT Fund", primary_exchange="NSE", primary_symbol="X",
                      nse_symbol="X", bse_symbol=None, bse_scrip_code=None,
                      date_of_listing=date(2019, 1, 1)),
        UniverseEntry(isin="C", name="Example InvIT Trust", primary_exchange="NSE",
                      primary_symbol="Y", nse_symbol="Y", bse_symbol=None, bse_scrip_code=None,
                      date_of_listing=date(2019, 1, 1)),
    ]
    exclude_non_standard_instruments(entries)
    assert all(e.excluded for e in entries)


def test_exclude_insufficient_listing_history_uses_real_recent_listing():
    nse = parse_nse_equity_csv((FIXTURES_DIR / "nse_equity_sample.csv").read_text(encoding="utf-8"))
    entries = merge_universe(nse, [])
    by_symbol = {e.primary_symbol: e for e in entries}

    exclude_insufficient_listing_history(entries, as_of=date(2026, 9, 6), min_days=365)

    # Listed 2026-04-20 - well under a year old as of 2026-09-06.
    assert by_symbol["3BBLACKBIO"].excluded
    assert "365" in by_symbol["3BBLACKBIO"].exclusion_reason
    # Listed 2004 - long-established, not excluded.
    assert not by_symbol["TCS"].excluded


def test_exclude_insufficient_listing_history_leaves_unknown_listing_date_alone():
    entry = UniverseEntry(
        isin="A", name="BSE Only Co", primary_exchange="BSE", primary_symbol="X",
        nse_symbol=None, bse_symbol="X", bse_scrip_code="1", date_of_listing=None,
    )
    exclude_insufficient_listing_history([entry], as_of=date(2026, 9, 6))
    assert not entry.excluded


# --- Financials/utilities pre-filter (must run before fundamentals.py) ----
#
# This is what the pilot run should have excluded *before* ever calling
# screener.in - banks/NBFCs/insurers use a completely different page
# template there (no "Operating Profit" row), so every one of these
# hitting fundamentals.py wastes a full retry cycle on a company that
# section 5 excludes from ranking regardless.


def _entry(name: str) -> UniverseEntry:
    return UniverseEntry(
        isin=name, name=name, primary_exchange="NSE", primary_symbol=name,
        nse_symbol=name, bse_symbol=None, bse_scrip_code=None,
        date_of_listing=date(2010, 1, 1),
    )


def test_exclude_likely_financials_flags_real_bank_and_nbfc_names():
    entries = [
        _entry("HDFC Bank Limited"),
        _entry("Bajaj Finance Limited"),
        _entry("ICICI Lombard General Insurance Company Limited"),
        _entry("Cholamandalam Investment and Finance Company Limited"),
    ]
    exclude_likely_financials_and_utilities(entries)
    assert all(e.excluded for e in entries)
    assert all("Financial Services" in e.exclusion_reason for e in entries)


def test_exclude_likely_utilities_flags_real_power_names():
    entries = [_entry("Power Grid Corporation of India Limited"), _entry("Tata Power Company Limited")]
    exclude_likely_financials_and_utilities(entries)
    assert all(e.excluded for e in entries)
    assert all("Utilities" in e.exclusion_reason for e in entries)


def test_exclude_likely_financials_and_utilities_leaves_ordinary_companies_alone():
    entries = [_entry("Tata Consultancy Services Limited"), _entry("Asian Paints Limited")]
    exclude_likely_financials_and_utilities(entries)
    assert not any(e.excluded for e in entries)


def test_exclude_likely_financials_and_utilities_does_not_override_existing_exclusion():
    entry = _entry("HDFC Bank Limited")
    entry.excluded = True
    entry.exclusion_reason = "already excluded for another reason"
    exclude_likely_financials_and_utilities([entry])
    assert entry.exclusion_reason == "already excluded for another reason"
