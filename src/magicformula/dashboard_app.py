"""Streamlit dashboard - browse the current basket, rank table, and
backtest results (SPEC.md section 9, per the user's own scoping: these
three views, none of which need quality_overlay.py's Phase 2 output).

Launched via `magicformula dashboard` (cli.py shells out to
`streamlit run` on this file) or directly with
`streamlit run src/magicformula/dashboard_app.py`.

Reads only files the CLI's other commands already produce
(data/processed/current_basket.csv, current_ranking.csv,
backtest_stats.json, backtest_baskets*.csv) - no new computation happens
here, this is a viewer, not a second implementation of the pipeline.
"""

from __future__ import annotations

import json
from pathlib import Path

import pandas as pd
import streamlit as st

PROCESSED_DIR = Path("data/processed")

st.set_page_config(page_title="Magic Formula PIT", layout="wide")
st.title("Magic Formula PIT")

tab_basket, tab_rank, tab_backtest = st.tabs(["Current Basket", "Rank Table", "Backtest Results"])

with tab_basket:
    basket_path = PROCESSED_DIR / "current_basket.csv"
    if not basket_path.exists():
        st.info(f"No basket found at `{basket_path}` yet - run `magicformula basket` first.")
    else:
        basket_df = pd.read_csv(basket_path)
        holdings_state_path = PROCESSED_DIR / "current_holdings.json"
        as_of = None
        if holdings_state_path.exists():
            with holdings_state_path.open() as f:
                as_of = json.load(f).get("as_of")

        st.subheader(f"Current basket{f' - as of {as_of}' if as_of else ''}")
        col1, col2, col3, col4 = st.columns(4)
        col1.metric("Total holdings", len(basket_df))
        for col, bucket in zip((col2, col3, col4), ("large", "mid", "small")):
            col.metric(bucket.capitalize(), int((basket_df["cap_bucket"] == bucket).sum()))

        bucket_filter = st.multiselect(
            "Filter by cap bucket", options=["large", "mid", "small"],
            default=["large", "mid", "small"], key="basket_bucket_filter",
        )
        filtered = basket_df[basket_df["cap_bucket"].isin(bucket_filter)]
        st.dataframe(
            filtered.sort_values("combined_rank")[["symbol", "cap_bucket", "weight", "roce", "ey", "combined_rank"]],
            use_container_width=True, hide_index=True,
        )

with tab_rank:
    ranking_path = PROCESSED_DIR / "current_ranking.csv"
    if not ranking_path.exists():
        st.info(f"No ranking found at `{ranking_path}` yet - run `magicformula rank` first.")
    else:
        rank_df = pd.read_csv(ranking_path)
        as_of = rank_df["as_of"].iloc[0] if len(rank_df) else None
        st.subheader(f"Full ranking{f' - as of {as_of}' if as_of else ''} ({len(rank_df)} stocks)")

        bucket_filter = st.multiselect(
            "Filter by cap bucket", options=sorted(rank_df["cap_bucket"].dropna().unique()),
            default=sorted(rank_df["cap_bucket"].dropna().unique()), key="rank_bucket_filter",
        )
        top_n = st.slider("Show top N per bucket", min_value=5, max_value=100, value=30, key="rank_top_n")
        filtered = rank_df[rank_df["cap_bucket"].isin(bucket_filter)]
        filtered = filtered.groupby("cap_bucket", group_keys=False).apply(
            lambda g: g.sort_values("position").head(top_n)
        )
        st.dataframe(
            filtered.sort_values(["cap_bucket", "position"])[
                ["cap_bucket", "position", "symbol", "roce", "ey", "combined_rank"]
            ],
            use_container_width=True, hide_index=True,
        )

with tab_backtest:
    # Prefer the real, Nifty-500-TRI-benchmarked research result
    # (backtest_stats.json, decision 0012/0013) if this repo has one -
    # falls back to the CLI's own cli_backtest_stats.json (flat 0%
    # benchmark unless --benchmark-returns-json was passed) for a fresh
    # clone that hasn't produced that richer file.
    stats_path = PROCESSED_DIR / "backtest_stats.json"
    baskets_default_name = "backtest_baskets_2019_2026.csv"
    if not stats_path.exists():
        stats_path = PROCESSED_DIR / "cli_backtest_stats.json"
        baskets_default_name = "cli_backtest_baskets.csv"
    if not stats_path.exists():
        st.info(f"No backtest results found yet - run `magicformula backtest` first.")
    else:
        with stats_path.open() as f:
            data = json.load(f)
        stats = data["stats"]

        st.subheader(f"Backtest: {data['rebalance_dates'][0]} to {data['rebalance_dates'][-1]}")
        col1, col2, col3, col4, col5 = st.columns(5)
        col1.metric("CAGR", f"{stats['cagr']:+.2%}")
        col2.metric("Volatility", f"{stats['annualized_volatility']:.2%}")
        col3.metric("Sharpe", f"{stats['sharpe_ratio']:.3f}")
        col4.metric("Max drawdown", f"{stats['max_drawdown']:.2%}")
        col5.metric("Mean turnover", f"{stats['mean_turnover']:.2%}")

        st.caption(
            f"Risk-free rate used: {stats['risk_free_rate_used']:.2%} | "
            f"Periods: {stats['periods']} | "
            "Survivorship bias applies to every number above - see README's "
            "Known Limitations before treating these as ground truth."
        )

        curve_df = pd.DataFrame({
            "date": data["rebalance_dates"],
            "Strategy": data["equity_curve"],
            "Benchmark": data["benchmark_equity_curve"],
        }).set_index("date")
        st.line_chart(curve_df)

        equity_png = PROCESSED_DIR / "equity_curve.png"
        if equity_png.exists():
            st.image(str(equity_png), caption="Equity curve vs Nifty 500 TRI vs cap-tilt-matched blend")

        baskets_path = PROCESSED_DIR / baskets_default_name
        if not baskets_path.exists():
            baskets_path = PROCESSED_DIR / "cli_backtest_baskets.csv"
        if baskets_path.exists():
            st.subheader("Basket composition by rebalance date")
            baskets_df = pd.read_csv(baskets_path)
            selected_date = st.selectbox(
                "Rebalance date", options=sorted(baskets_df["rebalance_date"].unique(), reverse=True),
            )
            st.dataframe(
                baskets_df[baskets_df["rebalance_date"] == selected_date],
                use_container_width=True, hide_index=True,
            )
