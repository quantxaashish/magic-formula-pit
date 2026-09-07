"""Magic Formula (Greenblatt) core calculations: EBIT, ROCE/ROC, EV, EY.

Pure, independently testable functions per SPEC.md section 3. Ranking and
basket construction live elsewhere (ranker.py, portfolio.py) - this module
only knows how to turn one company's raw financials into per-stock metrics,
including the exclusion rules that must run before anything gets ranked.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Literal

logger = logging.getLogger(__name__)

RoceMode = Literal["standard", "strict_greenblatt"]

EXCLUDE_NON_POSITIVE_EBIT = "non-positive EBIT"
EXCLUDE_NON_POSITIVE_EV = "non-positive EV (net cash exceeds market cap plus debt)"
EXCLUDE_NON_POSITIVE_CAPITAL_EMPLOYED = (
    "non-positive capital employed (current liabilities meet or exceed total assets)"
)


def ebit(operating_profit: float, depreciation_amortization: float) -> float:
    """EBIT = Operating Profit (EBITDA-equivalent) - Depreciation & Amortization."""
    return operating_profit - depreciation_amortization


def capital_employed_standard(total_assets: float, current_liabilities: float) -> float:
    """Standard-mode Capital Employed = Total Assets - Current Liabilities."""
    return total_assets - current_liabilities


def roce_standard(ebit_value: float, capital_employed: float) -> float:
    return ebit_value / capital_employed


def net_working_capital(current_assets: float, current_liabilities: float) -> float:
    return current_assets - current_liabilities


def net_fixed_assets(net_ppe: float, capital_work_in_progress: float) -> float:
    return net_ppe + capital_work_in_progress


def roc_strict(ebit_value: float, nwc: float, nfa: float) -> float:
    """Strict Greenblatt ROC = EBIT / (Net Working Capital + Net Fixed Assets)."""
    return ebit_value / (nwc + nfa)


def enterprise_value(
    market_cap: float,
    total_debt: float,
    cash_and_equivalents: float,
    other_liquid_investments: float = 0.0,
) -> float:
    """EV = Market Cap + Total Debt - Cash - other liquid short-term investments."""
    return market_cap + total_debt - cash_and_equivalents - other_liquid_investments


def earnings_yield(ebit_value: float, ev: float) -> float:
    return ebit_value / ev


@dataclass
class FinancialInputs:
    """Raw per-stock inputs for one fiscal period.

    Strict-Greenblatt fields are optional: when roce_mode="strict_greenblatt"
    is requested but any of them is missing, compute_metrics falls back to
    standard mode for that stock and logs it (SPEC.md section 3).
    """

    symbol: str
    operating_profit: float
    depreciation_amortization: float
    total_assets: float
    current_liabilities: float
    market_cap: float
    total_debt: float
    cash_and_equivalents: float
    other_liquid_investments: float = 0.0
    current_assets: float | None = None
    net_ppe: float | None = None
    capital_work_in_progress: float | None = None


@dataclass
class RankingMetrics:
    symbol: str
    ebit: float
    capital_employed: float
    roce: float
    ev: float
    ey: float
    roce_mode_used: RoceMode
    excluded: bool
    exclusion_reason: str | None = None


def compute_metrics(
    inputs: FinancialInputs, roce_mode: RoceMode = "standard"
) -> RankingMetrics:
    """Compute ROCE/ROC, EV and EY for one stock, applying section 3's exclusions.

    EBIT <= 0, capital employed <= 0, and EV <= 0 each exclude the stock
    rather than producing a nonsensical (or, for capital employed
    exactly 0, division-by-zero-crashing) ranked value; a strict-mode
    request with missing balance sheet fields falls back to standard
    mode rather than failing outright.

    The capital-employed check was missing until a real small-cap
    company (Total Assets == Other Liabilities, so standard-mode capital
    employed computed to exactly 0) crashed a live backtest run with
    ZeroDivisionError - found this way, not by inspection, because the
    two large+mid-only pilots never happened to include a company thin
    enough to hit it. Checked the wider full-universe data directly
    after the crash: 5 real companies (one exactly 0, four negative -
    GTL's capital employed is -7,237 Cr, a real reflection of severe
    financial distress, not a data error) hit this at a single rebalance
    date in a small-cap-inclusive run. Negative or zero capital employed
    means current liabilities meet or exceed total assets - there is no
    meaningful "return on capital employed" to compute, the same way
    negative EBIT or negative EV already aren't ranked.
    """
    ebit_value = ebit(inputs.operating_profit, inputs.depreciation_amortization)

    if ebit_value <= 0:
        return RankingMetrics(
            symbol=inputs.symbol,
            ebit=ebit_value,
            capital_employed=float("nan"),
            roce=float("nan"),
            ev=float("nan"),
            ey=float("nan"),
            roce_mode_used=roce_mode,
            excluded=True,
            exclusion_reason=EXCLUDE_NON_POSITIVE_EBIT,
        )

    roce_mode_used: RoceMode = roce_mode
    if roce_mode == "strict_greenblatt":
        missing_strict_fields = (
            inputs.current_assets is None
            or inputs.net_ppe is None
            or inputs.capital_work_in_progress is None
        )
        if missing_strict_fields:
            logger.warning(
                "%s: missing balance sheet fields for strict Greenblatt ROC, "
                "falling back to standard mode",
                inputs.symbol,
            )
            roce_mode_used = "standard"

    if roce_mode_used == "strict_greenblatt":
        assert inputs.current_assets is not None
        assert inputs.net_ppe is not None
        assert inputs.capital_work_in_progress is not None
        nwc = net_working_capital(inputs.current_assets, inputs.current_liabilities)
        nfa = net_fixed_assets(inputs.net_ppe, inputs.capital_work_in_progress)
        capital_employed = nwc + nfa
    else:
        capital_employed = capital_employed_standard(
            inputs.total_assets, inputs.current_liabilities
        )

    if capital_employed <= 0:
        return RankingMetrics(
            symbol=inputs.symbol,
            ebit=ebit_value,
            capital_employed=capital_employed,
            roce=float("nan"),
            ev=float("nan"),
            ey=float("nan"),
            roce_mode_used=roce_mode_used,
            excluded=True,
            exclusion_reason=EXCLUDE_NON_POSITIVE_CAPITAL_EMPLOYED,
        )

    if roce_mode_used == "strict_greenblatt":
        roce = roc_strict(ebit_value, nwc, nfa)
    else:
        roce = roce_standard(ebit_value, capital_employed)

    ev = enterprise_value(
        inputs.market_cap,
        inputs.total_debt,
        inputs.cash_and_equivalents,
        inputs.other_liquid_investments,
    )

    if ev <= 0:
        return RankingMetrics(
            symbol=inputs.symbol,
            ebit=ebit_value,
            capital_employed=capital_employed,
            roce=roce,
            ev=ev,
            ey=float("nan"),
            roce_mode_used=roce_mode_used,
            excluded=True,
            exclusion_reason=EXCLUDE_NON_POSITIVE_EV,
        )

    ey = earnings_yield(ebit_value, ev)

    return RankingMetrics(
        symbol=inputs.symbol,
        ebit=ebit_value,
        capital_employed=capital_employed,
        roce=roce,
        ev=ev,
        ey=ey,
        roce_mode_used=roce_mode_used,
        excluded=False,
        exclusion_reason=None,
    )
