"""Loads config/settings.yaml into typed settings objects.

This is the single source of truth cli.py reads from - see
config/settings.yaml's own header comment for why these values live here
rather than re-hardcoded per-command. Library modules (formulas.py,
prices.py, backtest.py, ...) keep their own tested default parameter
values for direct/programmatic use; this loader is what feeds those same
parameters when a command runs through the CLI instead.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date
from pathlib import Path
from typing import Any

import yaml

DEFAULT_SETTINGS_PATH = Path("config/settings.yaml")


@dataclass
class UniverseSettings:
    amfi_xlsx_url: str
    as_of: date | None


@dataclass
class FundamentalsSettings:
    annual_filing_lag_days: int
    anomaly_iqr_k: float
    anomaly_min_sector_size: int
    anomaly_mode: str  # "flag_only" or "exclude"


@dataclass
class RankingSettings:
    roce_mode: str  # "standard" or "strict_greenblatt"


@dataclass
class PriceSettings:
    price_lookup_max_tolerance_days: int
    shares_lookup_max_tolerance_days: int


@dataclass
class PortfolioSettings:
    top_n: int
    buffer_multiplier: float
    max_sector_fraction: float


@dataclass
class BacktestSettings:
    rebalance_month: int
    rebalance_day: int
    start_year: int
    end_year: int | None
    transaction_cost_bps: float
    risk_free_rate: float
    periods_per_year: float


@dataclass
class Settings:
    universe: UniverseSettings
    fundamentals: FundamentalsSettings
    ranking: RankingSettings
    prices: PriceSettings
    portfolio: PortfolioSettings
    backtest: BacktestSettings


def _parse_optional_date(value: Any) -> date | None:
    if value is None:
        return None
    if isinstance(value, date):
        return value
    return date.fromisoformat(str(value))


def load_settings(path: Path | str = DEFAULT_SETTINGS_PATH) -> Settings:
    """Reads config/settings.yaml (or the given path) into a typed
    Settings object. Raises FileNotFoundError with a clear message if the
    file is missing - this project has no silent hardcoded fallback for
    these values anymore, the file is the single source of truth."""
    path = Path(path)
    if not path.exists():
        raise FileNotFoundError(
            f"Settings file not found at {path}. This is the single source "
            f"of truth for CLI-run parameters (basket size, tolerances, "
            f"transaction cost, rebalance dates, ...) - see "
            f"config/settings.yaml's own header for what belongs here."
        )
    with path.open("r", encoding="utf-8") as f:
        raw = yaml.safe_load(f)

    return Settings(
        universe=UniverseSettings(
            amfi_xlsx_url=raw["universe"]["amfi_xlsx_url"],
            as_of=_parse_optional_date(raw["universe"].get("as_of")),
        ),
        fundamentals=FundamentalsSettings(
            annual_filing_lag_days=raw["fundamentals"]["annual_filing_lag_days"],
            anomaly_iqr_k=raw["fundamentals"]["anomaly_iqr_k"],
            anomaly_min_sector_size=raw["fundamentals"]["anomaly_min_sector_size"],
            anomaly_mode=raw["fundamentals"]["anomaly_mode"],
        ),
        ranking=RankingSettings(
            roce_mode=raw["ranking"]["roce_mode"],
        ),
        prices=PriceSettings(
            price_lookup_max_tolerance_days=raw["prices"]["price_lookup_max_tolerance_days"],
            shares_lookup_max_tolerance_days=raw["prices"]["shares_lookup_max_tolerance_days"],
        ),
        portfolio=PortfolioSettings(
            top_n=raw["portfolio"]["top_n"],
            buffer_multiplier=raw["portfolio"]["buffer_multiplier"],
            max_sector_fraction=raw["portfolio"]["max_sector_fraction"],
        ),
        backtest=BacktestSettings(
            rebalance_month=raw["backtest"]["rebalance_month"],
            rebalance_day=raw["backtest"]["rebalance_day"],
            start_year=raw["backtest"]["start_year"],
            end_year=raw["backtest"].get("end_year"),
            transaction_cost_bps=raw["backtest"]["transaction_cost_bps"],
            risk_free_rate=raw["backtest"]["risk_free_rate"],
            periods_per_year=raw["backtest"]["periods_per_year"],
        ),
    )
