"""Does the 'Other Liabilities as Current Liabilities' proxy scramble
cross-sectional ROCE rank order, or just shift the level?

Context: docs/decisions/0001-fundamentals-current-liability-split.md found
that our standard-mode Capital Employed proxy (Total Assets - Other
Liabilities, since screener.in's free tier doesn't split current vs.
non-current liabilities) runs 3.3-6.6pp below screener's own published ROCE
for 5 large-caps. That's an absolute-level question. What actually matters
for Magic Formula is a relative-order question: does the same proxy
preserve rank order across companies, since a uniform level shift is
harmless to ranking and a reordering is not (worst of all near the top of
the ranking, which is what decides basket membership).

This script computes Spearman rank correlation between our formula's ROCE
(using the Other Liabilities proxy, via the actual production
compute_metrics()) and screener.in's own displayed ROCE, across 64 real
companies spanning large/mid/small cap and a range of sectors (financials
and utilities excluded per SPEC.md section 5, since ROCE isn't meaningful
for them anyway). All raw figures hand-read from screener.in's consolidated
financials on 2026-09-06 (FY ending Mar 2026), same method as
tests/fixtures/real_companies.py.

One company (VIP Industries) is dropped: negative EBIT means our own
exclusion rule (section 3) removes it from ranking entirely, so there's no
"our rank" to compare against screener's rank for it.

Limitation to keep in mind when reading the result: this is a 64-company
stratified sample, not the full ~2000-company investable universe. "Top
half of this sample" is the best available proxy for "names near a
realistic basket cutoff" until fundamentals.py exists and this same check
can be rerun at full universe scale.
"""

from __future__ import annotations

from scipy.stats import spearmanr

from magicformula.formulas import FinancialInputs, compute_metrics

# name, cap_bucket, market_cap_cr, published_roce_pct, operating_profit_cr,
# depreciation_cr, total_assets_cr, other_liabilities_cr
# (all from screener.in consolidated financials, FY Mar 2026, read 2026-09-06)
RAW = [
    ("TCS", "large", 833607, 63.03, 72398, 5560, 181167, 62644),
    ("Infosys", "large", 458580, 39.95, 42280, 4902, 154288, 52260),
    ("ITC", "large", 330912, 38.91, 27306, 1711, 93637, 18731),
    ("Asian Paints", "large", 242418, 26.34, 6700, 1229, 34519, 9218),
    ("Nestle India", "large", 271989, 85.31, 5261, 699, 13182, 7581),
    ("Reliance Industries", "large", 1789001, 10.3, 179065, 57688, 2177546, 870554),
    ("HCL Technologies", "large", 350985, 30.4, 26752, 4355, 115112, 34732),
    ("Hindustan Unilever", "large", 463668, 28.4, 15039, 1333, 79738, 29521),
    ("Maruti Suzuki", "large", 399103, 18.9, 21530, 6742, 148880, 41622),
    ("Eicher Motors", "large", 209474, 30.5, 5789, 840, 32164, 6549),
    ("Sun Pharma", "large", 455634, 20.5, 16501, 2938, 108407, 20210),
    ("Dr Reddy's Labs", "large", 96072, 13.0, 6454, 2059, 56577, 10951),
    ("Cipla", "large", 111888, 15.5, 5883, 1211, 42343, 7297),
    ("Divi's Labs", "large", 241576, 22.0, 3442, 463, 20019, 3251),
    ("Titan Company", "large", 445669, 20.5, 8357, 826, 60561, 14237),
    ("UltraTech Cement", "large", 336170, 12.7, 17004, 4644, 141315, 40936),
    ("JSW Steel", "large", 324023, 11.0, 29464, 9601, 269658, 70295),
    ("Tata Steel", "large", 235677, 12.5, 34352, 11955, 296515, 101965),
    ("Hindalco Industries", "large", 227195, 13.2, 34880, 8830, 344681, 108933),
    ("Coal India", "large", 255969, 35.0, 37172, 10137, 283956, 150782),
    ("Larsen & Toubro", "large", 545407, 14.6, 35411, 4365, 452383, 217596),
    ("Bharti Airtel", "large", 1148252, 17.6, 116514, 52711, 545373, 200904),
    ("Pidilite Industries", "large", 165864, 31.0, 3521, 395, 15399, 4149),
    ("Havells India", "large", 72421, 24.9, 2241, 432, 14746, 5026),
    ("Dabur India", "large", 67570, 20.3, 2450, 469, 17480, 4773),
    ("Mphasis", "mid", 46230, 22.1, 2978, 555, 17609, 4246),
    ("Persistent Systems", "mid", 89018, 34.4, 2796, 403, 11344, 3029),
    ("Coforge", "mid", 87391, 23.5, 2936, 682, 14846, 4581),
    ("Marico", "mid", 105838, 47.0, 2328, 202, 9950, 5183),
    ("Godrej Consumer Products", "mid", 89846, 18.8, 3169, 268, 20942, 3873),
    ("Britannia Industries", "mid", 122879, 56.0, 3514, 337, 9730, 3243),
    ("Voltas", "mid", 38714, 9.04, 516, 84, 14496, 7127),
    ("Crompton Greaves Consumer", "mid", 14965, 18.1, 827, 172, 6078, 2913),
    ("Bata India", "mid", 8647, 12.7, 726, 420, 3778, 796),
    ("Trent", "mid", 152131, 28.3, 3745, 1361, 11729, 2183),
    ("Jubilant Foodworks", "mid", 31851, 14.8, 1902, 959, 9367, 2172),
    ("Escorts Kubota", "mid", 33214, 13.9, 1496, 255, 15792, 3257),
    ("Ashok Leyland", "mid", 99268, 13.6, 2697, 1138, 100704, 22527),
    ("Samvardhana Motherson", "mid", 169610, 13.4, 12124, 5134, 109426, 49276),
    ("Bosch", "mid", 138101, 21.5, 2650, 392, 21680, 6716),
    ("SRF", "mid", 75499, 14.6, 3410, 852, 24097, 4971),
    ("PI Industries", "mid", 37399, 15.0, 1732, 407, 13405, 1832),
    ("Aarti Industries", "mid", 17848, 6.86, 1168, 474, 13300, 2379),
    ("Berger Paints", "mid", 56586, 21.6, 1833, 392, 10035, 2484),
    ("Century Plyboards", "small", 16629, 11.5, 651, 182, 5072, 697),
    ("V-Guard Industries", "small", 14654, 18.4, 527, 108, 3698, 1160),
    ("TTK Prestige", "small", 7708, 12.1, 277, 81, 2714, 560),
    ("Kajaria Ceramics", "small", 19479, 23.4, 869, 169, 4027, 733),
    ("Cera Sanitaryware", "small", 7464, 22.4, 302, 41, 1863, 442),
    ("JK Lakshmi Cement", "small", 6402, 12.0, 1000, 324, 8548, 2077),
    ("Ratnamani Metals", "small", 19114, 17.9, 758, 132, 5385, 957),
    ("Grindwell Norton", "small", 21729, 21.2, 576, 105, 3436, 840),
    ("Carborundum Universal", "small", 21539, 10.5, 582, 247, 5271, 959),
    ("Schaeffler India", "small", 61840, 27.3, 1766, 344, 8512, 2317),
    ("Timken India", "small", 23508, 19.0, 632, 106, 3699, 770),
    ("Graphite India", "small", 14333, 4.58, 202, 95, 7577, 1350),
    ("Welspun Corp", "small", 68338, 22.9, 2236, 355, 20400, 8889),
    ("APL Apollo Tubes", "small", 62473, 31.8, 1808, 231, 8833, 3039),
    ("HEG", "small", 14054, 8.35, 399, 213, 6166, 612),
    ("Nilkamal", "small", 3158, 10.6, 332, 142, 2518, 522),
    ("Deepak Nitrite", "small", 23356, 11.4, 987, 225, 8654, 1180),
    ("Vinati Organics", "small", 13587, 19.8, 655, 111, 3579, 417),
    ("Fine Organic Industries", "small", 15796, 21.5, 480, 56, 2999, 266),
    ("Galaxy Surfactants", "small", 7676, 13.5, 467, 123, 3865, 885),
    ("VIP Industries", "small", 4461, -28.9, -241, 127, 1604, 577),  # negative EBIT
]


def main() -> None:
    rows = []
    for name, bucket, mcap, published_roce, op, dep, ta, ol in RAW:
        inputs = FinancialInputs(
            symbol=name,
            operating_profit=op,
            depreciation_amortization=dep,
            total_assets=ta,
            current_liabilities=ol,
            market_cap=mcap,
            total_debt=0,
            cash_and_equivalents=0,
        )
        result = compute_metrics(inputs, roce_mode="standard")
        rows.append(
            {
                "name": name,
                "bucket": bucket,
                "published_roce": published_roce,
                "our_roce_pct": None if result.excluded else result.roce * 100,
                "excluded": result.excluded,
            }
        )

    excluded = [r for r in rows if r["excluded"]]
    ranked_rows = [r for r in rows if not r["excluded"]]

    print(f"Total companies: {len(rows)}")
    print(f"Excluded by our own EBIT<=0 rule (dropped from correlation): "
          f"{[r['name'] for r in excluded]}")
    print(f"Companies in correlation set: {len(ranked_rows)}\n")

    our_roce = [r["our_roce_pct"] for r in ranked_rows]
    published_roce = [r["published_roce"] for r in ranked_rows]

    rho_full, p_full = spearmanr(our_roce, published_roce)
    print(f"Spearman rank correlation (full sample, n={len(ranked_rows)}): "
          f"rho={rho_full:.4f} (p={p_full:.2e})")

    # "Top half by published ROCE" as the closest available proxy, within
    # this sample, for "names near a realistic basket cutoff" - see module
    # docstring for why this isn't literally top-50-of-2000.
    sorted_by_published = sorted(ranked_rows, key=lambda r: -r["published_roce"])
    top_half = sorted_by_published[: len(sorted_by_published) // 2]
    top_half_our = [r["our_roce_pct"] for r in top_half]
    top_half_published = [r["published_roce"] for r in top_half]
    rho_top, p_top = spearmanr(top_half_our, top_half_published)
    print(f"Spearman rank correlation (top half by published ROCE, n={len(top_half)}): "
          f"rho={rho_top:.4f} (p={p_top:.2e})")

    # Per-company rank displacement, sorted by |displacement|, largest first.
    def rank_of(rows_subset, key):
        ordered = sorted(rows_subset, key=lambda r: -r[key])
        return {r["name"]: i + 1 for i, r in enumerate(ordered)}

    our_rank = rank_of(ranked_rows, "our_roce_pct")
    pub_rank = rank_of(ranked_rows, "published_roce")

    displacement = sorted(
        ranked_rows,
        key=lambda r: -abs(our_rank[r["name"]] - pub_rank[r["name"]]),
    )

    print(f"\n{'Name':<28}{'Bucket':<8}{'OurROCE%':>10}{'PubROCE%':>10}"
          f"{'OurRank':>9}{'PubRank':>9}{'Displacement':>13}")
    for r in displacement:
        name = r["name"]
        print(
            f"{name:<28}{r['bucket']:<8}{r['our_roce_pct']:>10.2f}"
            f"{r['published_roce']:>10.2f}{our_rank[name]:>9}{pub_rank[name]:>9}"
            f"{our_rank[name] - pub_rank[name]:>13}"
        )


if __name__ == "__main__":
    main()
