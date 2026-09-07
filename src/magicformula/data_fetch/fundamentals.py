"""Fundamental data acquisition from screener.in (SPEC.md section 2.4).

Three layers:

1. Fetch + parse: a rate-limited, disk-caching, retrying HTTP client
   (ScreenerClient) plus a pure HTML parser (parse_company_html) that
   needs no network access, so it can be unit tested against saved HTML
   fixtures. Together they produce a FundamentalsRecord *per fiscal year*
   screener.in shows for a company (not just the latest one - see decision
   0006), matched between the P&L and balance-sheet tables by parsed
   fiscal_year_end date rather than raw column position, since the same
   period can carry different labels across the two tables.

2. Anomaly detection: built into this module, not ranker.py or
   portfolio.py, because a distorted fundamentals record is a data-quality
   property of the input, not a ranking decision. Two checks, generalized
   from - not hardcoded to - the two real cases that motivated them (see
   docs/decisions/0001-fundamentals-current-liability-split.md):
     - Total Assets / EBIT far from the peer-group median: catches a
       consolidated financial-services/NBFC subsidiary (or any other
       asset-heavy, EBIT-light distortion) inflating the balance sheet
       relative to reported EBIT. Found via Ashok Leyland (consolidates
       Hinduja Leyland Finance), generalized to any peer-group outlier.
     - Other Liabilities / Capital Employed far from the peer-group
       median: catches atypical provisioning (or an unusually clean
       balance sheet) distorting the Current Liabilities proxy this
       project uses in standard-mode Capital Employed. Found via Coal
       India (heavy mine reclamation / employee benefit provisioning),
       generalized the same way.
   Both checks are two-sided (a ratio far below the median is flagged
   too). The peer group itself is chosen via a fallback hierarchy - see
   check_fundamentals_anomalies()'s docstring - because at full-universe
   scale, especially in mid/small cap, screener.in's narrow "Sector" tag
   frequently won't have enough peers on its own (Coal India is the case
   that surfaced this: its own "Sector" has only 2 other peers in a
   64-company sample, one of them a conglomerate skewed enough to mask
   the anomaly entirely - see decision 0001's follow-up).

3. Extraction failure handling: fetch_universe_fundamentals retries each
   company up to `extraction_max_retries` times (forcing a fresh fetch,
   not the cache, on retry), and companies that still fail get logged to
   a failed_extractions.csv with the reason rather than silently vanishing
   from the universe - that failure list is itself a data-quality signal
   (a cluster of failures might mean a page template screener.in uses for
   certain company types isn't handled yet).

4. Authoritative sector-based exclusion (decision 0004): magicformula.
   universe's name-heuristic pre-filter is a best-effort cost-saver, not a
   guarantee - "REC Limited" (a well-known NBFC) carries no
   financial-sounding word in its registered name and slips past it.
   fetch_fundamentals() reads screener.in's own real "Broad Sector" tag
   immediately after the first successful HTML fetch, *before* attempting
   the full P&L/balance-sheet parse, and raises SectorExcluded if it's
   "Financial Services" or "Utilities" - checked early because many
   bank-style financials (deposit banks, core lending NBFCs like REC
   Limited itself) never produce a parseable record at all (their P&L
   reports Interest Earned/Expended, not Operating Profit), so a check
   that waited for a successful full parse would never catch them. This
   costs exactly one fetch (no retry, no consolidated/standalone fallback
   - the sector doesn't change between attempts) and routes the company to
   UniverseFetchResult.sector_excluded, not failed_extractions - it is a
   correct, informed exclusion, not a failure.

Compliance (SPEC.md section 2.5): screener.in has no public API or
scraping license. This client is for personal, non-commercial use only -
keep the rate limit and cache in place, don't redistribute the scraped
dataset, and don't raise the request rate to work around the limiter.
"""

from __future__ import annotations

import calendar
import csv
import logging
import math
import re
import time
from dataclasses import dataclass, field
from datetime import date, timedelta
from pathlib import Path
from statistics import median
from typing import Literal

import requests
from bs4 import BeautifulSoup

from magicformula.formulas import capital_employed_standard, ebit

logger = logging.getLogger(__name__)

DEFAULT_USER_AGENT = (
    "MagicFormulaIndia-Research/0.1 (personal, non-commercial research tool; "
    "see SPEC.md section 2.5 for compliance notes)"
)
DEFAULT_RATE_LIMIT_SECONDS = 2.5


def company_url(symbol: str, statement: str) -> str:
    """screener.in only has an explicit "consolidated" URL segment -
    "standalone" is the bare company URL with no statement segment at all,
    not literally "/standalone/" (confirmed live: "/company/COLPAL/" is
    200 with real data, "/company/COLPAL/standalone/" is 404). Getting
    this wrong silently breaks the consolidated<->standalone fallback
    (decision 0003) for every company that needs it - every fallback
    attempt 404s instead of reaching the real standalone page - which is
    exactly what happened until this was caught against a live pilot run.
    """
    if statement == "consolidated":
        return f"https://www.screener.in/company/{symbol}/consolidated/"
    if statement == "standalone":
        return f"https://www.screener.in/company/{symbol}/"
    raise ValueError(f"unknown statement type: {statement!r}")


# SEBI LODR Regulation 33 gives listed companies up to 60 days after fiscal
# year end to file annual results. screener.in's free view doesn't expose
# the actual filing/announcement date, so as_of_date uses this as a
# conservative (i.e. late-side) proxy: real filings are typically earlier,
# so this is a safe worst-case for the point-in-time discipline in section
# 6, not a precise value. Replace with a real announcement-date scrape
# (NSE/BSE corporate announcements) before this matters for a live
# backtest that leans on exact timing.
ANNUAL_FILING_LAG_DAYS = 60

MONTH_ABBREVIATIONS = {
    "Jan": 1, "Feb": 2, "Mar": 3, "Apr": 4, "May": 5, "Jun": 6,
    "Jul": 7, "Aug": 8, "Sep": 9, "Oct": 10, "Nov": 11, "Dec": 12,
}

PeerTier = Literal["sector", "broad_sector", "cap_bucket", "none"]


@dataclass
class FundamentalsRecord:
    """One fiscal year of one company's fundamentals. A single company
    fetch (parse_company_html) produces *many* of these - every fiscal
    year screener.in shows, not just the latest - each with its own
    fiscal_year_end and therefore its own as_of_date, which is what makes
    the point-in-time embargo (SPEC.md section 6, magicformula.backtest.
    filter_point_in_time) meaningful across a company's history rather
    than reusable single-snapshot data pretending to be a time series.
    """

    symbol: str
    sector: str | None  # screener.in's own narrow "Sector" tag (GICS-like)
    broad_sector: str | None  # screener.in's own coarser "Broad Sector" tag
    fiscal_year_end: date
    operating_profit: float
    depreciation_amortization: float
    total_assets: float
    other_liabilities: float
    investments: float
    cwip: float
    borrowings: float
    market_cap: float  # screener.in's CURRENT market cap - identical across
    # every fiscal year of a given fetch, since screener doesn't show
    # historical market cap in this table structure. NOT valid for
    # point-in-time EV/EY on anything but the latest fiscal year; a real
    # backtest must recompute this from a point-in-time share price
    # (magicformula.data_fetch.prices) before using a historical record.
    source_url: str
    statement: str = "consolidated"  # which statement type actually supplied this
    # record - may be the fallback type, not the one originally requested;
    # see _fetch_with_retry's docstring.
    cap_bucket: str | None = None  # "large"/"mid"/"small"; filled in from universe.py's
    # AMFI categorization once merged - not produced by this module alone.
    anomaly_reasons: list[str] = field(default_factory=list)
    # Added for quality_overlay.py (Phase 2 - F-score, Z-score, accrual
    # flag, promoter-pledge threshold). Per-fiscal-year like the rest of
    # this record, EXCEPT promoter_pledge_percentage, which - like
    # market_cap - is screener.in's *current* snapshot repeated on every
    # returned record, not a real historical time series (screener.in's
    # free-tier page doesn't show pledge history by fiscal year, only a
    # present-day "Insights" bullet). None means "no pledge disclosed at
    # this threshold", not "confirmed zero" - screener.in only surfaces
    # this bullet when pledging is material enough to flag.
    sales: float | None = None
    net_profit: float | None = None
    cash_from_operations: float | None = None
    equity_capital: float | None = None
    reserves: float | None = None
    promoter_pledge_percentage: float | None = None
    as_of_date: date = field(init=False)

    def __post_init__(self) -> None:
        self.as_of_date = self.fiscal_year_end + timedelta(days=ANNUAL_FILING_LAG_DAYS)


# --- Fetch layer -------------------------------------------------------


class ScreenerClient:
    """Thin, rate-limited, caching HTTP client for screener.in.

    No public API exists (SPEC.md section 2.5) - keep requests infrequent
    and cache aggressively rather than re-fetching a company's financials
    that won't have changed within the same quarter.
    """

    def __init__(
        self,
        cache_dir: Path,
        rate_limit_seconds: float = DEFAULT_RATE_LIMIT_SECONDS,
        max_retries: int = 3,
        timeout: float = 15.0,
        user_agent: str = DEFAULT_USER_AGENT,
    ) -> None:
        self.cache_dir = Path(cache_dir)
        self.cache_dir.mkdir(parents=True, exist_ok=True)
        self.rate_limit_seconds = rate_limit_seconds
        self.max_retries = max_retries
        self.timeout = timeout
        self.session = requests.Session()
        self.session.headers["User-Agent"] = user_agent
        self._last_request_at = 0.0

    def _wait_for_rate_limit(self) -> None:
        elapsed = time.monotonic() - self._last_request_at
        remaining = self.rate_limit_seconds - elapsed
        if remaining > 0:
            time.sleep(remaining)

    def fetch_html(
        self, symbol: str, statement: str = "consolidated", force_refresh: bool = False
    ) -> str:
        cache_path = self.cache_dir / f"{symbol}_{statement}.html"
        if cache_path.exists() and not force_refresh:
            logger.debug("%s: serving %s from cache", symbol, statement)
            return cache_path.read_text(encoding="utf-8")

        url = company_url(symbol, statement)
        last_error: Exception | None = None
        for attempt in range(1, self.max_retries + 1):
            self._wait_for_rate_limit()
            self._last_request_at = time.monotonic()
            try:
                response = self.session.get(url, timeout=self.timeout)
                response.raise_for_status()
                cache_path.write_text(response.text, encoding="utf-8")
                return response.text
            except requests.RequestException as exc:
                last_error = exc
                logger.warning(
                    "%s: fetch attempt %d/%d failed: %s", symbol, attempt, self.max_retries, exc
                )
                if attempt < self.max_retries:
                    time.sleep(min(2**attempt, 30))

        raise RuntimeError(
            f"{symbol}: failed to fetch {statement} financials after "
            f"{self.max_retries} attempts"
        ) from last_error


# --- Parse layer (pure, no network I/O) ---------------------------------


def _row(table, label: str) -> list[str] | None:
    for row in table.find_all("tr"):
        cells = row.find_all(["td", "th"])
        if not cells:
            continue
        first = cells[0].get_text(strip=True).rstrip("+").strip()
        if first == label:
            return [c.get_text(strip=True) for c in cells[1:]]
    return None


def _header(table) -> list[str]:
    first_row = table.find("tr")
    if not first_row:
        return []
    return [c.get_text(strip=True) for c in first_row.find_all(["td", "th"])]


def _to_number(text: str) -> float:
    return float(text.replace(",", "").replace("₹", "").replace("Cr.", "").strip())


def _to_number_or_none(text: str) -> float | None:
    """Same as _to_number, but returns None instead of raising on an
    unparseable cell (e.g. screener.in's "-" placeholder for a year a
    line item didn't apply). Used only for the optional quality-overlay
    fields (sales, net_profit, equity_capital, cash_from_operations) -
    unlike the required fields, a bad value in one of these shouldn't
    drop an otherwise-good fiscal-year record that the rest of the
    pipeline already depends on.
    """
    try:
        return _to_number(text)
    except ValueError:
        return None


def _promoter_pledge_percentage(soup: BeautifulSoup) -> float | None:
    """screener.in surfaces a material promoter pledge as a plain-text
    "Insights" bullet - "Promoters have pledged X% of their holding." -
    not a numeric table row, and only when pledging is significant enough
    to flag (confirmed live: Ashok Leyland shows this bullet at 40.1%
    pledged; TCS/Siemens/Tata Power/RECLTD show no such bullet at all).
    Returns None when the bullet isn't present - that means "no pledge
    disclosed at this threshold", not "confirmed zero pledge". This is a
    *current* snapshot, like market_cap - screener.in doesn't show pledge
    history by fiscal year on this page.
    """
    match = re.search(r"pledged\s+([\d.]+)%", soup.get_text(), re.IGNORECASE)
    return float(match.group(1)) if match else None


def _parse_fiscal_year_end(label: str) -> date:
    """Parse a screener.in annual column header like 'Mar 2026' into the
    fiscal year end date (last day of that month).

    A company that changed its fiscal year end reports one stub/
    transition period longer or shorter than 12 months; screener.in labels
    that column like "Mar 202618m" (an 18-month period ending March 2026,
    with no space before the "18m" suffix) - found via Siemens Ltd
    (September -> March fiscal year change), see
    docs/decisions/0005-corrected-pilot-results.md. The year token needs
    that trailing "<N>m" stripped before parsing; the stub period's actual
    length isn't otherwise used here, just its end date.
    """
    month_str, year_str = label.split()
    month = MONTH_ABBREVIATIONS[month_str]
    # Exactly 4 digits for the year itself - a stub-period suffix like
    # "18m" is also all-digits-then-letter, so a plain \d+ greedily
    # swallows it too ("202618" instead of "2026") and produces an
    # out-of-range year instead of a clean parse error.
    year_match = re.match(r"\d{4}", year_str)
    if not year_match:
        raise ValueError(f"could not parse a year out of column header {label!r}")
    year = int(year_match.group())
    last_day = calendar.monthrange(year, month)[1]
    return date(year, month, last_day)


def _safe_parse_fiscal_year_end(label: str) -> date | None:
    try:
        return _parse_fiscal_year_end(label)
    except (ValueError, KeyError):
        return None


def _fiscal_year_index_map(header: list[str]) -> dict[date, int]:
    """Map fiscal_year_end -> position in a row's *value* list (i.e. after
    the row-label cell has already been stripped, matching what _row()
    returns) for every header cell that parses as a fiscal year. Skips the
    leading blank label-column header and any unparseable cell ("TTM"),
    so the resulting indices align directly with _row()'s output.
    """
    mapping: dict[date, int] = {}
    for i, cell in enumerate(header[1:]):
        fiscal_year_end = _safe_parse_fiscal_year_end(cell)
        if fiscal_year_end is not None:
            mapping[fiscal_year_end] = i
    return mapping


def _sector_tag(soup: BeautifulSoup, title: str) -> str | None:
    peer_heading = soup.find("h2", string="Peer comparison")
    if not peer_heading:
        return None
    sub = peer_heading.find_next("p", class_="sub")
    if not sub:
        return None
    link = next((a for a in sub.find_all("a") if a.get("title") == title), None)
    return link.get_text(strip=True) if link else None


# Real screener.in "Broad Sector" tag values, confirmed live for HDFC
# Bank/REC Limited/LIC ("Financial Services") and Tata Power/NTPC Green
# ("Utilities") - see docs/decisions/0004-sector-based-exclusion.md.
FINANCIAL_UTILITY_BROAD_SECTORS = {"Financial Services", "Utilities"}


def parse_sector_tags(html: str) -> tuple[str | None, str | None]:
    """Extract just the (Sector, Broad Sector) tags - no network I/O.

    Deliberately separate from parse_company_html: the Peer Comparison
    section renders for every company regardless of its P&L template, so
    this always succeeds even for bank-style financials whose full
    financial-statement parse never will (they report Interest Earned/
    Expended, not Operating Profit). That's what lets a company be
    correctly excluded after exactly one fetch instead of exhausting the
    full retry+consolidated/standalone-fallback budget first.
    """
    soup = BeautifulSoup(html, "lxml")
    return _sector_tag(soup, "Sector"), _sector_tag(soup, "Broad Sector")


def is_financial_or_utility_sector(broad_sector: str | None) -> bool:
    return broad_sector in FINANCIAL_UTILITY_BROAD_SECTORS


class SectorExcluded(Exception):
    """Raised by fetch_fundamentals when a company's own real Broad Sector
    tag says Financial Services/Utilities - a correct, informed exclusion
    (SPEC.md section 5), not a parse failure. Not retried and not eligible
    for the consolidated/standalone fallback: the sector doesn't change
    between attempts or statement types.
    """

    def __init__(self, symbol: str, sector: str | None, broad_sector: str | None):
        self.symbol = symbol
        self.sector = sector
        self.broad_sector = broad_sector
        super().__init__(
            f"{symbol}: real sector ({broad_sector}/{sector}) is Financial Services/Utilities"
        )


def parse_company_html(
    html: str, symbol: str, source_url: str, statement: str = "consolidated"
) -> list[FundamentalsRecord]:
    """Extract every available fiscal-year column from a screener.in
    company page - not just the latest - as one FundamentalsRecord per
    fiscal year, oldest first. Pure function, no network I/O, so it's
    testable against saved HTML fixtures.

    How far back the history goes varies by company, checked directly
    against real pages rather than assumed: a long-listed, fiscal-year-
    stable company shows ~12 years (TCS, Tata Power); a company that
    changed its fiscal year end can show a much *shorter* window even if
    long-listed (Nestle India: only 4 columns, because screener.in only
    displays periods consistent with its current Dec->Mar fiscal year
    convention, not its full multi-decade listed history); a company can
    also show years predating its own NSE listing (its audited financials
    existed before that particular listing, even if untradeable then -
    magicformula.universe's listing-history exclusion is a separate,
    later concern from what this parser reports).

    A fiscal year is matched between the P&L and balance-sheet tables by
    its *parsed* fiscal_year_end date, not raw column position or label
    text: the same period can carry a different label in each table
    (Siemens' fiscal-year-change stub period is "Mar 202618m" in the P&L
    header but plain "Mar 2026" in the balance sheet header - seen live,
    not hypothesized). A year present in only one table is skipped and
    logged, not guessed at from the other.

    market_cap is screener.in's *current* figure, the same value on every
    returned record regardless of that record's fiscal year - screener
    doesn't show historical market cap in this table structure. It is
    NOT valid for point-in-time EV/EY on a historical record; recomputing
    it from a point-in-time share price (magicformula.data_fetch.prices)
    is required before this per-year data is used for anything but the
    latest fiscal year.
    """
    soup = BeautifulSoup(html, "lxml")

    pl_table = None
    bs_table = None
    cf_table = None
    for table in soup.find_all("table"):
        if _row(table, "Operating Profit") and "TTM" in _header(table):
            pl_table = table
        if _row(table, "Total Assets"):
            bs_table = table
        if _row(table, "Cash from Operating Activity"):
            cf_table = table

    if pl_table is None or bs_table is None:
        raise ValueError(f"{symbol}: could not locate annual P&L / balance sheet tables")

    op_row = _row(pl_table, "Operating Profit")
    dep_row = _row(pl_table, "Depreciation")
    ta_row = _row(bs_table, "Total Assets")
    ol_row = _row(bs_table, "Other Liabilities")
    if not (op_row and dep_row and ta_row and ol_row):
        raise ValueError(f"{symbol}: missing required financial statement rows")

    investments_row = _row(bs_table, "Investments")
    cwip_row = _row(bs_table, "CWIP")
    borrowings_row = _row(bs_table, "Borrowings")
    # Added for quality_overlay.py (decision 0014) - all optional (None if
    # the row or table is absent), unlike the required rows above, since
    # F-score/Z-score/accrual-flag callers already need to handle missing
    # quality data gracefully (a company lacking these was never fetchable
    # for this purpose before this parser change existed).
    sales_row = _row(pl_table, "Sales")
    net_profit_row = _row(pl_table, "Net Profit")
    equity_capital_row = _row(bs_table, "Equity Capital")
    reserves_row = _row(bs_table, "Reserves")
    cfo_row = _row(cf_table, "Cash from Operating Activity") if cf_table is not None else None
    cf_years = _fiscal_year_index_map(_header(cf_table)) if cf_table is not None else {}
    promoter_pledge_percentage = _promoter_pledge_percentage(soup)

    pl_years = _fiscal_year_index_map(_header(pl_table))
    bs_years = _fiscal_year_index_map(_header(bs_table))
    shared_years = sorted(set(pl_years) & set(bs_years))
    if not shared_years:
        raise ValueError(
            f"{symbol}: no fiscal year is present in both the P&L and balance sheet tables"
        )

    pl_only = set(pl_years) - set(bs_years)
    bs_only = set(bs_years) - set(pl_years)
    if pl_only:
        logger.warning(
            "%s: %d fiscal year(s) in P&L but not balance sheet, skipped: %s",
            symbol, len(pl_only), sorted(pl_only),
        )
    if bs_only:
        logger.warning(
            "%s: %d fiscal year(s) in balance sheet but not P&L, skipped: %s",
            symbol, len(bs_only), sorted(bs_only),
        )

    ratios: dict[str, str] = {}
    top_ratios = soup.find(id="top-ratios")
    if top_ratios:
        for li in top_ratios.find_all("li"):
            spans = li.find_all("span")
            if len(spans) >= 2:
                ratios[spans[0].get_text(strip=True)] = spans[1].get_text(strip=True)
    market_cap = _to_number(ratios["Market Cap"]) if ratios.get("Market Cap") else 0.0

    sector = _sector_tag(soup, "Sector")
    broad_sector = _sector_tag(soup, "Broad Sector")

    records: list[FundamentalsRecord] = []
    for fiscal_year_end in shared_years:
        pl_i, bs_i = pl_years[fiscal_year_end], bs_years[fiscal_year_end]
        try:
            records.append(
                FundamentalsRecord(
                    symbol=symbol,
                    sector=sector,
                    broad_sector=broad_sector,
                    fiscal_year_end=fiscal_year_end,
                    operating_profit=_to_number(op_row[pl_i]),
                    depreciation_amortization=_to_number(dep_row[pl_i]),
                    total_assets=_to_number(ta_row[bs_i]),
                    other_liabilities=_to_number(ol_row[bs_i]),
                    investments=_to_number(investments_row[bs_i])
                    if investments_row and bs_i < len(investments_row) else 0.0,
                    cwip=_to_number(cwip_row[bs_i])
                    if cwip_row and bs_i < len(cwip_row) else 0.0,
                    borrowings=_to_number(borrowings_row[bs_i])
                    if borrowings_row and bs_i < len(borrowings_row) else 0.0,
                    market_cap=market_cap,
                    source_url=source_url,
                    statement=statement,
                    sales=_to_number_or_none(sales_row[pl_i])
                    if sales_row and pl_i < len(sales_row) else None,
                    net_profit=_to_number_or_none(net_profit_row[pl_i])
                    if net_profit_row and pl_i < len(net_profit_row) else None,
                    equity_capital=_to_number_or_none(equity_capital_row[bs_i])
                    if equity_capital_row and bs_i < len(equity_capital_row) else None,
                    reserves=_to_number_or_none(reserves_row[bs_i])
                    if reserves_row and bs_i < len(reserves_row) else None,
                    cash_from_operations=(
                        _to_number_or_none(cfo_row[cf_years[fiscal_year_end]])
                        if cfo_row and fiscal_year_end in cf_years
                        and cf_years[fiscal_year_end] < len(cfo_row) else None
                    ),
                    promoter_pledge_percentage=promoter_pledge_percentage,
                )
            )
        except (IndexError, ValueError) as exc:
            logger.warning(
                "%s: skipping fiscal year %s, could not parse a value: %s",
                symbol, fiscal_year_end, exc,
            )

    if not records:
        raise ValueError(f"{symbol}: no fiscal year produced a parseable record")

    return records


def fetch_fundamentals(
    symbol: str,
    client: ScreenerClient,
    statement: str = "consolidated",
    force_refresh: bool = False,
) -> list[FundamentalsRecord]:
    """Returns every available fiscal year for this company (see
    parse_company_html), oldest first - not just the latest."""
    html = client.fetch_html(symbol, statement=statement, force_refresh=force_refresh)

    sector, broad_sector = parse_sector_tags(html)
    if is_financial_or_utility_sector(broad_sector):
        raise SectorExcluded(symbol, sector, broad_sector)

    source_url = company_url(symbol, statement)
    return parse_company_html(html, symbol, source_url=source_url, statement=statement)


# --- Anomaly detection ---------------------------------------------------


@dataclass
class AnomalyCheckResult:
    symbol: str
    flagged: bool
    reasons: list[str]
    total_assets_to_ebit: float | None
    total_assets_to_ebit_peer_group: str | None
    total_assets_to_ebit_peer_tier: PeerTier
    total_assets_to_ebit_median: float | None
    other_liabilities_to_capital_employed: float | None
    other_liabilities_to_capital_employed_peer_group: str | None
    other_liabilities_to_capital_employed_peer_tier: PeerTier
    other_liabilities_to_capital_employed_median: float | None


def _log_iqr_bounds(values: list[float], k: float = 1.5) -> tuple[float, float] | None:
    """Tukey's fences (k x IQR, default k=1.5) computed on the *log* of
    each value - decision 0010's calibration fix. Total Assets/EBIT and
    Other Liabilities/Capital Employed are both positive, multiplicative
    ratios whose denominator (EBIT, Capital Employed) can be small-but-
    positive for an entirely normal company, which mechanically produces
    a severely right-skewed distribution (measured skew 12-16 across the
    full universe) - not a data-quality signal, just the shape of a
    ratio like this. A flat "median x constant" threshold computed on the
    raw ratio is calibrated for something close to symmetric and ends up
    flagging ~51% of the universe on distribution shape alone. Taking the
    log first turns that heavy right skew into something close to
    symmetric, so ordinary quartile-based fences aren't themselves
    thrown off by it.

    Returns (lower, upper) in LOG SPACE, deliberately not exponentiated
    back - _deviates compares log(value) against these directly rather
    than round-tripping through exp(), because that round trip isn't
    exact: in a near-degenerate pool (several peers sharing the same
    ratio almost exactly, IQR close to 0), exp(log(x)) can land a hair on
    either side of x due to ordinary floating-point error, which was
    enough to spuriously flag a peer against its own, identical value.
    Comparing in log space avoids that entirely. Callers that want a
    human-readable range for a message should exponentiate the returned
    bounds themselves, for display only, not for the flagging decision.
    Returns None if fewer than 2 (strictly positive - non-positive values
    are dropped first, defensively: math.log() raises ValueError at 0 and
    is undefined for negative inputs, and callers are expected to have
    already excluded non-positive ratios from the pool they pass in, but
    this doesn't assume that's always true rather than crash if it isn't)
    values remain.
    """
    positive_values = [v for v in values if v > 0]
    if len(positive_values) < 2:
        return None
    logs = sorted(math.log(v) for v in positive_values)
    n = len(logs)

    def _percentile(p: float) -> float:
        idx = p * (n - 1)
        lo_i = int(idx)
        hi_i = min(lo_i + 1, n - 1)
        frac = idx - lo_i
        return logs[lo_i] * (1 - frac) + logs[hi_i] * frac

    q1, q3 = _percentile(0.25), _percentile(0.75)
    iqr = q3 - q1
    return q1 - k * iqr, q3 + k * iqr


def _deviates(value: float | None, pool: dict[str, float] | None, iqr_k: float) -> bool:
    """value is anomalous if its log falls outside its peer pool's
    log-scale Tukey fences - see _log_iqr_bounds for why log-scale, and
    why the comparison happens in log space rather than exponentiating
    the fences back first. `pool` is the resolved peer group's {symbol:
    ratio} dict (the record's own ratio is included in its own pool, by
    construction of the caller) - bounds are computed fresh per call
    rather than reusing a precomputed median, since IQR needs the full
    distribution, not just its center.
    """
    if value is None or value <= 0 or not pool:
        return False
    bounds = _log_iqr_bounds(list(pool.values()), k=iqr_k)
    if bounds is None:
        return False
    lower, upper = bounds
    log_value = math.log(value)
    return log_value < lower or log_value > upper


def _resolve_peer_group(
    record: FundamentalsRecord,
    ratio_by_symbol: dict[str | None, dict[str, float]],
    min_group_size: int,
) -> tuple[str | None, PeerTier, dict[str, float] | None]:
    """Walk the fallback hierarchy for one record and one ratio: narrow
    screener.in "Sector" tag first, then the coarser "Broad Sector" tag,
    then cap_bucket (large/mid/small) as the widest, last-resort grouping.
    Returns (peer_group_label, tier_used, pool) - pool is None (tier
    "none") if not even cap_bucket clears min_group_size.

    `min_group_size` is not just "enough peers for a median to mean
    something" - decision 0010 found that below about 5 peers, log-scale
    IQR fences have essentially no power to catch even a clear, real
    outlier (checked empirically: a 5x-magnitude outlier against 2 other
    peers with ordinary natural spread was missed 100% of the time across
    200 randomized trials; the same scenario with 4 peers caught it
    ~100% of the time). A tier that technically has 3-4 members but not
    enough for the check to be numerically trustworthy is treated the
    same as a tier with too few members at all: fall back wider, or admit
    "can't judge" (tier "none") rather than compute a technically-valid
    but practically meaningless bound.

    ratio_by_symbol here is keyed by the *tier's grouping value* (e.g. all
    "Sector" values map to {symbol: ratio}), one dict per tier, precomputed
    once by the caller for all three tiers.
    """
    candidates: list[tuple[str, PeerTier, str | None]] = [
        ("sector", "sector", record.sector),
        ("broad_sector", "broad_sector", record.broad_sector),
        ("cap_bucket", "cap_bucket", record.cap_bucket),
    ]
    for tier_key, tier_name, group_value in candidates:
        pool = ratio_by_symbol[tier_key].get(group_value, {})
        if len(pool) >= min_group_size:
            return group_value, tier_name, pool
    return None, "none", None


def _latest_record_per_symbol(records: list[FundamentalsRecord]) -> list[FundamentalsRecord]:
    """One record per symbol - whichever has the latest fiscal_year_end.
    Used to keep the anomaly check (a current-snapshot judgment) from
    counting the same company's multiple historical years as if they were
    separate peers, which would both inflate that sector's apparent size
    and skew its median toward whichever company happens to have the
    longest history.
    """
    latest: dict[str, FundamentalsRecord] = {}
    for record in records:
        current = latest.get(record.symbol)
        if current is None or record.fiscal_year_end > current.fiscal_year_end:
            latest[record.symbol] = record
    return list(latest.values())


def check_fundamentals_anomalies(
    records: list[FundamentalsRecord],
    iqr_k: float = 1.5,
    min_sector_size: int = 5,
) -> list[AnomalyCheckResult]:
    """Flag records whose Total Assets/EBIT or Other Liabilities/Capital
    Employed ratio falls outside its peer group's log-scale Tukey fences
    (decision 0010 - see _log_iqr_bounds for why log-scale, and why not a
    flat multiple-of-median: that flagged 51.1% of the full universe,
    driven by the severe right-skew both ratios naturally have, not by
    half the universe being genuinely anomalous).

    Two-sided: a ratio far below the lower fence is flagged too, not just
    far above the upper one - an unusually clean balance sheet is also a
    reason to look twice before trusting the number, not just an
    unusually distorted one.

    Peer group is chosen via a three-tier fallback, evaluated
    independently per record and per ratio (the two ratios can resolve at
    different tiers for the same company):
      1. screener.in's narrow "Sector" tag - tried first, most specific.
      2. screener.in's coarser "Broad Sector" tag - tried if tier 1 has
         fewer than min_sector_size peers with a computable ratio.
      3. cap_bucket (large/mid/small) - tried if tier 2 also falls short;
         the widest, last-resort peer group.
    If none of the three clears min_sector_size, the check is skipped
    (not flagged, not cleared) for that ratio - too few peers anywhere for
    IQR fences to mean anything (decision 0010: below ~5 peers, log-scale
    IQR has essentially no power to catch even a clear, real outlier -
    raised from the previous default of 3 for exactly this reason, not an
    arbitrary tightening). Which tier actually got used is recorded on
    the result (*_peer_tier) - that's a data-quality signal worth keeping,
    not just an implementation detail: a company resolved at "cap_bucket"
    is being compared to a much noisier, more heterogeneous peer group
    than one resolved at "sector", and a cluster of "cap_bucket"
    resolutions across an otherwise-normal sector is itself worth noticing.
    """
    ta_ebit_pools: dict[str, dict[str | None, dict[str, float]]] = {
        "sector": {}, "broad_sector": {}, "cap_bucket": {},
    }
    ol_ce_pools: dict[str, dict[str | None, dict[str, float]]] = {
        "sector": {}, "broad_sector": {}, "cap_bucket": {},
    }
    ta_ebit_by_symbol: dict[str, float] = {}
    ol_ce_by_symbol: dict[str, float] = {}

    for record in records:
        ebit_value = ebit(record.operating_profit, record.depreciation_amortization)
        if ebit_value > 0:
            ratio = record.total_assets / ebit_value
            ta_ebit_by_symbol[record.symbol] = ratio
            # Pool membership requires a strictly positive ratio, not just
            # a positive denominator: _log_iqr_bounds takes math.log() of
            # every pool value, which raises ValueError at exactly 0 and
            # is undefined for negative inputs. total_assets is >= 0 in
            # every real record checked so far, but this isn't assumed -
            # a record with an unusual/zero-total-assets snapshot must
            # stay out of the pool that computes bounds for everyone
            # else, the same way it wouldn't sensibly anchor a median.
            if ratio > 0:
                for tier_key, group_value in (
                    ("sector", record.sector),
                    ("broad_sector", record.broad_sector),
                    ("cap_bucket", record.cap_bucket),
                ):
                    ta_ebit_pools[tier_key].setdefault(group_value, {})[record.symbol] = ratio

        capital_employed = capital_employed_standard(record.total_assets, record.other_liabilities)
        if capital_employed > 0:
            ratio = record.other_liabilities / capital_employed
            ol_ce_by_symbol[record.symbol] = ratio
            # Same reasoning as above - found via a real crash: two real
            # companies (EFCIL, TPHQ) have other_liabilities == 0 exactly
            # (Total Liabilities = Equity + Borrowings, no residual "other"
            # bucket) with capital_employed > 0, giving ratio == 0 exactly
            # - passed the capital_employed > 0 guard but crashed
            # math.log(0) once it reached the pool.
            if ratio > 0:
                for tier_key, group_value in (
                    ("sector", record.sector),
                    ("broad_sector", record.broad_sector),
                    ("cap_bucket", record.cap_bucket),
                ):
                    ol_ce_pools[tier_key].setdefault(group_value, {})[record.symbol] = ratio

    results: list[AnomalyCheckResult] = []
    for record in records:
        ta_ebit_value = ta_ebit_by_symbol.get(record.symbol)
        ol_ce_value = ol_ce_by_symbol.get(record.symbol)

        ta_group, ta_tier, ta_pool = _resolve_peer_group(record, ta_ebit_pools, min_sector_size)
        ol_group, ol_tier, ol_pool = _resolve_peer_group(record, ol_ce_pools, min_sector_size)
        ta_median = median(ta_pool.values()) if ta_pool else None
        ol_median = median(ol_pool.values()) if ol_pool else None

        reasons = []
        if _deviates(ta_ebit_value, ta_pool, iqr_k):
            lo_log, hi_log = _log_iqr_bounds(list(ta_pool.values()), k=iqr_k)  # type: ignore[misc]
            reasons.append(
                f"Total Assets/EBIT = {ta_ebit_value:.2f} vs. {ta_tier} "
                f"({ta_group}) median {ta_median:.2f} "
                f"(expected range {math.exp(lo_log):.2f}-{math.exp(hi_log):.2f})"
            )
        if _deviates(ol_ce_value, ol_pool, iqr_k):
            lo_log, hi_log = _log_iqr_bounds(list(ol_pool.values()), k=iqr_k)  # type: ignore[misc]
            reasons.append(
                f"Other Liabilities/Capital Employed = {ol_ce_value:.3f} vs. {ol_tier} "
                f"({ol_group}) median {ol_median:.3f} "
                f"(expected range {math.exp(lo_log):.3f}-{math.exp(hi_log):.3f})"
            )

        results.append(
            AnomalyCheckResult(
                symbol=record.symbol,
                flagged=bool(reasons),
                reasons=reasons,
                total_assets_to_ebit=ta_ebit_value,
                total_assets_to_ebit_peer_group=ta_group,
                total_assets_to_ebit_peer_tier=ta_tier,
                total_assets_to_ebit_median=ta_median,
                other_liabilities_to_capital_employed=ol_ce_value,
                other_liabilities_to_capital_employed_peer_group=ol_group,
                other_liabilities_to_capital_employed_peer_tier=ol_tier,
                other_liabilities_to_capital_employed_median=ol_median,
            )
        )

    return results


def apply_anomaly_filter(
    records: list[FundamentalsRecord],
    results: list[AnomalyCheckResult],
    mode: Literal["exclude", "flag_only"] = "exclude",
) -> list[FundamentalsRecord]:
    """Apply check_fundamentals_anomalies()'s results to a record list.

    mode="exclude" (default): flagged records are dropped, each with a
    logged reason - a smaller basket is preferable to knowingly holding a
    stock whose ROCE inputs are already known to be unreliable.
    mode="flag_only": every record is kept (flagged ones get
    anomaly_reasons populated) for manual review; nothing is dropped.
    """
    result_by_symbol = {r.symbol: r for r in results}
    kept: list[FundamentalsRecord] = []
    for record in records:
        result = result_by_symbol.get(record.symbol)
        if result is None or not result.flagged:
            kept.append(record)
            continue

        record.anomaly_reasons = result.reasons
        for reason in result.reasons:
            logger.warning("%s: fundamentals anomaly flagged - %s", record.symbol, reason)

        if mode == "exclude":
            logger.warning(
                "%s: excluded from ranking due to fundamentals anomaly (mode=exclude)",
                record.symbol,
            )
            continue
        kept.append(record)

    return kept


# --- Extraction failure handling ------------------------------------------


@dataclass
class FailedExtraction:
    symbol: str
    statement: str
    attempts: int
    reason: str


@dataclass
class SectorExclusion:
    symbol: str
    sector: str | None
    broad_sector: str | None


@dataclass
class UniverseFetchResult:
    records: list[FundamentalsRecord]  # every fiscal year for every successful
    # company, flattened - many records can share a symbol; group by
    # .symbol and .fiscal_year_end downstream as needed (e.g.
    # magicformula.backtest.filter_point_in_time for a specific rebalance
    # date). total_symbols below counts companies, not rows in this list.
    failed_extractions: list[FailedExtraction]
    sector_excluded: list[SectorExclusion]
    total_symbols: int


def _fetch_one_statement(
    symbol: str,
    client: ScreenerClient,
    statement: str,
    extraction_max_retries: int,
) -> tuple[list[FundamentalsRecord], FailedExtraction | None]:
    """Attempt fetch+parse for one symbol under one statement type,
    retrying up to extraction_max_retries additional times after the first
    failure. Any retry forces a fresh fetch (bypassing the cache) rather
    than re-parsing the same possibly-corrupt cached page. Success returns
    every fiscal year found (see parse_company_html), not just one.

    SectorExcluded is deliberately not caught here - it isn't a transient
    failure retrying could fix (the company's sector won't change), so it
    propagates straight to the caller instead of burning the retry budget.
    """
    last_error: Exception | None = None
    total_attempts = extraction_max_retries + 1
    for attempt in range(1, total_attempts + 1):
        try:
            records = fetch_fundamentals(
                symbol, client, statement=statement, force_refresh=(attempt > 1)
            )
            return records, None
        except SectorExcluded:
            raise
        except Exception as exc:  # noqa: BLE001 - genuinely any other failure should retry/log
            last_error = exc
            logger.warning(
                "%s: %s extraction attempt %d/%d failed: %s",
                symbol, statement, attempt, total_attempts, exc,
            )

    return [], FailedExtraction(
        symbol=symbol, statement=statement, attempts=total_attempts, reason=str(last_error)
    )


def _fetch_with_retry(
    symbol: str,
    client: ScreenerClient,
    statement: str,
    extraction_max_retries: int,
) -> tuple[list[FundamentalsRecord], FailedExtraction | None, SectorExclusion | None]:
    """Try `statement` first; if every attempt fails, fall back to the
    other statement type (consolidated -> standalone or vice versa) before
    giving up entirely. Returns a non-empty records list on success, with
    failure/sector_exclusion both None; otherwise records is [] and
    exactly one of (failure, sector_exclusion) is set.

    Some companies have no usable consolidated financials on screener.in
    at all - not a 404, a 200 with an empty or stale-dated table (MNC
    subsidiaries without material subsidiaries of their own - Colgate,
    Pfizer, Bayer CropScience - and some PSUs like Garden Reach
    Shipbuilders were all found this way; see
    docs/decisions/0003-consolidated-standalone-fallback.md). Their
    standalone page has the real numbers. The reverse (standalone missing,
    consolidated fine) is rarer but the fallback works either direction.

    A company whose real sector is Financial Services/Utilities
    (SectorExcluded, decision 0004) short-circuits immediately - no
    fallback attempt either, since the sector is the same regardless of
    statement type.
    """
    fallback_statement = "standalone" if statement == "consolidated" else "consolidated"

    try:
        records, failure = _fetch_one_statement(symbol, client, statement, extraction_max_retries)
    except SectorExcluded as exc:
        logger.info(
            "%s: excluded after 1 fetch - real sector is %s/%s (financials/utilities "
            "pre-filter missed it)", exc.symbol, exc.broad_sector, exc.sector,
        )
        return [], None, SectorExclusion(exc.symbol, exc.sector, exc.broad_sector)

    if records:
        return records, None, None

    logger.info(
        "%s: %s financials unavailable/invalid after %d attempts, trying %s",
        symbol, statement, failure.attempts, fallback_statement,
    )
    try:
        records, fallback_failure = _fetch_one_statement(
            symbol, client, fallback_statement, extraction_max_retries
        )
    except SectorExcluded as exc:
        return [], None, SectorExclusion(exc.symbol, exc.sector, exc.broad_sector)

    if records:
        logger.info("%s: used %s financials (%s unavailable/invalid)", symbol, fallback_statement, statement)
        return records, None, None

    # Both statement types failed - report the fallback's failure (the one
    # actually meant to be a resort, so its reason is more informative than
    # a duplicate of the first) but attribute cost to both attempts.
    return [], FailedExtraction(
        symbol=symbol,
        statement=f"{statement}+{fallback_statement}",
        attempts=failure.attempts + fallback_failure.attempts,
        reason=fallback_failure.reason,
    ), None


FailureCategory = Literal["no_page_anywhere", "page_template_mismatch", "genuine_parse_error"]


def classify_failure_reason(reason: str) -> FailureCategory:
    """Best-effort classification of a FailedExtraction.reason into one of
    three causes, for reporting/triage only - never used to change
    pipeline behavior. Distinguishes known, already-diagnosed failure
    modes (docs/decisions/0003, 0005) from something genuinely new that
    needs a human look before scaling further:

    - "no_page_anywhere": a plain HTTP-level failure (404/timeout/etc.)
      under both statement types - ScreenerClient's own
      "failed to fetch ... after N attempts" message.
    - "page_template_mismatch": both statement types returned a page, but
      neither had the P&L/balance-sheet rows in the shape
      parse_company_html expects - the bank/NBFC-style template mismatch,
      or a company whose consolidated/standalone pair are both stale or
      empty (see decision 0003).
    - "genuine_parse_error": anything else. This is the category worth
      watching before scaling to a larger universe - it means something
      wasn't caught by either known pattern above, i.e. a real gap.
    """
    lower = reason.lower()
    if "failed to fetch" in lower and "attempts" in lower:
        return "no_page_anywhere"
    if "could not locate annual p&l" in lower or "missing required financial statement rows" in lower:
        return "page_template_mismatch"
    return "genuine_parse_error"


def write_failed_extractions(failures: list[FailedExtraction], path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", newline="", encoding="utf-8") as f:
        writer = csv.writer(f)
        writer.writerow(["symbol", "statement", "attempts", "reason"])
        for failure in failures:
            writer.writerow([failure.symbol, failure.statement, failure.attempts, failure.reason])


def fetch_universe_fundamentals(
    symbols: list[str],
    client: ScreenerClient,
    statement: str = "consolidated",
    extraction_max_retries: int = 2,
    failed_extractions_path: Path | None = None,
    iqr_k: float = 1.5,
    min_sector_size: int = 5,
    anomaly_mode: Literal["exclude", "flag_only"] = "exclude",
    cap_bucket_by_symbol: dict[str, str] | None = None,
) -> UniverseFetchResult:
    """Fetch fundamentals for a universe and apply the anomaly check before
    returning - built in from the start, not bolted on after a full
    universe pull already exists.

    `symbols` should already have financials/utilities filtered out
    upstream (magicformula.universe.exclude_likely_financials_and_utilities,
    run before this is ever called) - their screener.in page uses a
    completely different template this parser can't and shouldn't handle,
    so there's no reason to spend the retry budget (now doubled by the
    consolidated/standalone fallback below) fetching them at all. A
    company that slips past that name-heuristic pre-filter (decision 0004
    - "REC Limited" is the known case) is still caught here: its real
    Broad Sector tag is checked after exactly one fetch and routed to
    `sector_excluded`, not `failed_extractions` - it's a correct exclusion,
    not a failure.

    `cap_bucket_by_symbol` (optional - cap_bucket comes from universe.py's
    AMFI categorization, a separate concern this module doesn't otherwise
    know about) is applied to every fetched record *before* the anomaly
    check runs, not after. Decision 0009 found a caller (the full-universe
    fetch script) setting `record.cap_bucket` only after this function had
    already returned, which meant the anomaly check's third-tier
    cap_bucket peer-group fallback (decision 0004) never had cap_bucket
    available internally and could never fire, regardless of whether it
    was needed - silently unreachable rather than merely unused. Passing
    it in here, not patching records after the fact, is what makes the
    fallback actually reachable when sector and broad_sector both fall
    short of `min_sector_size`.

    Each symbol tries `statement` first, then falls back to the other
    statement type if every attempt fails (see _fetch_with_retry) - some
    companies have no usable consolidated financials on screener.in at all.
    Extraction failures (both statement types exhausted) are logged to
    failed_extractions_path if given, and are always reported in the
    returned UniverseFetchResult rather than silently dropped.

    Each successful symbol contributes *every* fiscal year screener.in
    shows (see parse_company_html), not just the latest - `records` is a
    flat list spanning all companies and all their available years. The
    anomaly check (decision 0001) still only makes sense as a per-company,
    current-snapshot judgment - it runs against each company's single
    latest fiscal year only (`_latest_record_per_symbol`), and a flagged
    company has *all* of its historical years excluded together, not just
    the latest, on the assumption that a structural distortion found in
    the current year (e.g. Ashok Leyland's consolidated NBFC subsidiary)
    was very likely present in its prior years too.
    """
    records: list[FundamentalsRecord] = []
    failures: list[FailedExtraction] = []
    sector_excluded: list[SectorExclusion] = []
    for symbol in symbols:
        symbol_records, failure, exclusion = _fetch_with_retry(
            symbol, client, statement, extraction_max_retries
        )
        if symbol_records:
            if cap_bucket_by_symbol is not None:
                for record in symbol_records:
                    record.cap_bucket = cap_bucket_by_symbol.get(symbol)
            records.extend(symbol_records)
        elif exclusion is not None:
            sector_excluded.append(exclusion)
        else:
            failures.append(failure)

    if failures and failed_extractions_path is not None:
        write_failed_extractions(failures, failed_extractions_path)

    logger.info(
        "%d of %d companies failed extraction%s; %d excluded on real sector "
        "(Financial Services/Utilities missed by the pre-fetch name heuristic)",
        len(failures),
        len(symbols),
        f" (see {failed_extractions_path})" if failures and failed_extractions_path else "",
        len(sector_excluded),
    )

    latest_records = _latest_record_per_symbol(records)
    anomaly_results = check_fundamentals_anomalies(latest_records, iqr_k, min_sector_size)
    filtered = apply_anomaly_filter(records, anomaly_results, mode=anomaly_mode)

    return UniverseFetchResult(
        records=filtered,
        failed_extractions=failures,
        sector_excluded=sector_excluded,
        total_symbols=len(symbols),
    )
