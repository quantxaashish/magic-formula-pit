"""Hand-verified real-company fixtures for formulas.py (SPEC.md section 12).

Source: screener.in consolidated financials, FY ending Mar 2026, read directly
off the site on 2026-09-06 (https://www.screener.in/company/<SYMBOL>/consolidated/).
Operating Profit, Depreciation and Market Cap are exact, as-reported figures.

Known limitation: screener.in's free-tier balance sheet does not split
current vs. non-current assets/liabilities - it only reports "Other
Liabilities" (Total Liabilities - Equity - Borrowings) and "Investments" as
undifferentiated buckets. We approximate:
  - current_liabilities  := Other Liabilities
  - cash_and_equivalents := 0, other_liquid_investments := Investments
    (the Investments bucket is predominantly liquid treasury holdings for
    these five large-caps, so this is a reasonable EV proxy, not exact)

The resulting ROCE runs 3.3-6.6 percentage points below screener's own
published figure across all five names. This is NOT primarily because the
Other Liabilities proxy over/understates true current liabilities - it's
because screener's own ROCE uses a different capital-employed definition
entirely (averages opening/closing balances, excludes CWIP/Investments/
other non-current assets, uses TTM figures, excludes extraordinary items -
none of which SPEC.md's standard-mode formula does). See
docs/decisions/0001-fundamentals-current-liability-split.md for the full
diagnosis, including why this is a real blocker for fundamentals.py
(section 2.4) independent of whether we ever try to match screener's
displayed number.

These fixtures exist to sanity-check formulas.py's arithmetic against real
filings within a documented tolerance, per section 12 - not to reproduce
screener.in's (undisclosed) internal ROCE methodology exactly. All amounts
in Rs. Crore.
"""

from __future__ import annotations

from magicformula.formulas import FinancialInputs

SOURCE = "screener.in consolidated balance sheet, FY Mar 2026, read 2026-09-06"

REAL_COMPANY_FIXTURES = [
    {
        # https://www.screener.in/company/TCS/consolidated/
        "inputs": FinancialInputs(
            symbol="TCS",
            operating_profit=72_398,
            depreciation_amortization=5_560,
            total_assets=181_167,
            current_liabilities=62_644,  # "Other Liabilities" proxy
            market_cap=833_607,
            total_debt=11_283,
            cash_and_equivalents=0,
            other_liquid_investments=33_988,  # "Investments" proxy
        ),
        "published_roce_pct": 63.03,
        "roce_tolerance_pct": 8.0,
        "source": SOURCE,
    },
    {
        # https://www.screener.in/company/INFY/consolidated/
        "inputs": FinancialInputs(
            symbol="INFY",
            operating_profit=42_280,
            depreciation_amortization=4_902,
            total_assets=154_288,
            current_liabilities=52_260,
            market_cap=458_580,
            total_debt=9_176,
            cash_and_equivalents=0,
            other_liquid_investments=21_880,
        ),
        "published_roce_pct": 39.95,
        "roce_tolerance_pct": 8.0,
        "source": SOURCE,
    },
    {
        # https://www.screener.in/company/ITC/consolidated/
        "inputs": FinancialInputs(
            symbol="ITC",
            operating_profit=27_306,
            depreciation_amortization=1_711,
            total_assets=93_637,
            current_liabilities=18_731,
            market_cap=330_912,
            total_debt=2_399,
            cash_and_equivalents=0,
            other_liquid_investments=38_128,
        ),
        "published_roce_pct": 38.91,
        "roce_tolerance_pct": 8.0,
        "source": SOURCE,
    },
    {
        # https://www.screener.in/company/ASIANPAINT/consolidated/
        "inputs": FinancialInputs(
            symbol="ASIANPAINT",
            operating_profit=6_700,
            depreciation_amortization=1_229,
            total_assets=34_519,
            current_liabilities=9_218,
            market_cap=242_418,
            total_debt=3_929,
            cash_and_equivalents=0,
            other_liquid_investments=7_062,
        ),
        "published_roce_pct": 26.34,
        "roce_tolerance_pct": 8.0,
        "source": SOURCE,
    },
    {
        # https://www.screener.in/company/NESTLEIND/consolidated/
        "inputs": FinancialInputs(
            symbol="NESTLEIND",
            operating_profit=5_261,
            depreciation_amortization=699,
            total_assets=13_182,
            current_liabilities=7_581,
            market_cap=271_989,
            total_debt=444,
            cash_and_equivalents=0,
            other_liquid_investments=531,
        ),
        "published_roce_pct": 85.31,
        "roce_tolerance_pct": 8.0,
        "source": SOURCE,
    },
]
