"""Unit tests for magicformula.config - the CLI's single source of truth
for parameters previously hardcoded (differently) across multiple scripts.
"""

from datetime import date
from pathlib import Path

import pytest

from magicformula.config import load_settings


def test_load_settings_reads_the_real_committed_config_file():
    """The actual config/settings.yaml this repo ships, not a fixture -
    if this breaks, the CLI's real config is broken, not just a test."""
    settings = load_settings(Path("config/settings.yaml"))

    assert settings.portfolio.top_n == 30
    assert settings.portfolio.buffer_multiplier == 1.5
    assert settings.portfolio.max_sector_fraction == 0.30
    assert settings.prices.price_lookup_max_tolerance_days == 14
    assert settings.prices.shares_lookup_max_tolerance_days == 90
    assert settings.backtest.transaction_cost_bps == 25.0
    assert settings.backtest.start_year == 2019
    assert settings.fundamentals.anomaly_mode == "flag_only"
    assert settings.fundamentals.annual_filing_lag_days == 60
    assert settings.ranking.roce_mode == "standard"


def test_load_settings_missing_file_raises_clear_error():
    with pytest.raises(FileNotFoundError, match="Settings file not found"):
        load_settings(Path("does/not/exist.yaml"))


def test_load_settings_parses_explicit_as_of_date(tmp_path):
    config_text = """
universe:
  amfi_xlsx_url: "https://example.com/x.xlsx"
  as_of: "2026-01-15"
fundamentals:
  annual_filing_lag_days: 60
  anomaly_iqr_k: 1.5
  anomaly_min_sector_size: 5
  anomaly_mode: flag_only
ranking:
  roce_mode: standard
prices:
  price_lookup_max_tolerance_days: 14
  shares_lookup_max_tolerance_days: 90
portfolio:
  top_n: 30
  buffer_multiplier: 1.5
  max_sector_fraction: 0.30
backtest:
  rebalance_month: 6
  rebalance_day: 1
  start_year: 2019
  end_year: null
  transaction_cost_bps: 25.0
  risk_free_rate: 0.053
  periods_per_year: 1.0
"""
    config_path = tmp_path / "settings.yaml"
    config_path.write_text(config_text)

    settings = load_settings(config_path)

    assert settings.universe.as_of == date(2026, 1, 15)
    assert settings.backtest.end_year is None
