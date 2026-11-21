"""Alphalens on saved PIT snapshots, next-open forward returns, no data fetching."""

from __future__ import annotations

import warnings
from importlib.metadata import version

import numpy as np
import pandas as pd

HORIZONS = (5, 20, 60)
SUMMARY_COLUMNS = [
    "factor",
    "horizon",
    "sample_count",
    "periods",
    "mean_rank_ic",
    "top_minus_bottom",
    "mean_top_turnover",
]


def analyze_factors(cross_sections, prices, calendar, factors, quantiles):
    from alphalens import performance as ap

    from ai_quant_assistant.research.performance import finite

    calendar = pd.DatetimeIndex(calendar).normalize().drop_duplicates().sort_values()
    offsets = {day: i for i, day in enumerate(calendar)}
    bars = prices.copy()
    bars["date"] = pd.to_datetime(bars.date).dt.normalize()
    bars = bars.set_index(["date", "symbol"])
    rows = []
    for record in cross_sections.to_dict("records"):
        signal = pd.Timestamp(record["signal_date"]).normalize()
        index = offsets.get(signal)
        if index is None or index + 1 >= len(calendar):
            continue
        entry_key = (calendar[index + 1], record["symbol"])
        entry = bars.loc[entry_key] if entry_key in bars.index else None
        item = {**record, "date": signal, "asset": record["symbol"]}
        for horizon in HORIZONS:
            item[f"{horizon}D"] = np.nan
            if (
                entry is None
                or not np.isfinite(entry.open)
                or entry.open <= 0
                or not np.isfinite(entry.volume)
                or entry.volume <= 0
            ):
                continue
            if index + 1 + horizon >= len(calendar):
                continue
            key = (calendar[index + 1 + horizon], record["symbol"])
            if key not in bars.index:
                continue
            exit_bar = bars.loc[key]
            if (
                np.isfinite(exit_bar.open)
                and np.isfinite(exit_bar.volume)
                and exit_bar.open > 0
                and exit_bar.volume > 0
            ):
                item[f"{horizon}D"] = float(exit_bar.open / entry.open - 1)
        rows.append(item)
    records = pd.DataFrame(rows)
    summaries, ic_rows, quantile_rows = [], [], []
    configs = [(f.code, f.direction) for f in factors]
    if len(factors) > 1:
        configs.append(("composite_score", "desc"))
    for name, direction in configs:
        if records.empty:
            break
        base = records[["date", "asset", name, *(f"{h}D" for h in HORIZONS)]].copy()
        base = base.rename(columns={name: "factor"}).set_index(["date", "asset"]).sort_index()
        base["factor"] = pd.to_numeric(base.factor, errors="coerce") * (
            1 if direction == "desc" else -1
        )
        base = base.loc[np.isfinite(base.factor)].copy()

        # Equal factor values stay in the same quantile. Never split ties by symbol order.
        def bucket(group):
            if group.nunique() < 2:
                return pd.Series(np.nan, index=group.index)
            return pd.qcut(group, min(quantiles, len(group)), labels=False, duplicates="drop") + 1

        base["factor_quantile"] = base.groupby(level="date")["factor"].transform(bucket)
        base = base.dropna(subset=["factor_quantile"])
        if base.empty:
            continue
        top = base.groupby(level="date").factor_quantile.transform("max")
        # Binary membership makes top group comparable when tied quantiles collapse.
        membership = pd.Series(np.where(base.factor_quantile.eq(top), 2, 1), index=base.index)
        # Alphalens resamples by date frequency. Map observation snapshots to
        # consecutive days so a monthly gap is not interpreted as missing daily holdings.
        observed_dates = membership.index.get_level_values("date").unique().sort_values()
        ordinal_dates = dict(
            zip(
                observed_dates,
                pd.date_range("2000-01-01", periods=len(observed_dates)),
                strict=True,
            )
        )
        membership.index = pd.MultiIndex.from_arrays(
            [
                membership.index.get_level_values("date").map(ordinal_dates),
                membership.index.get_level_values("asset"),
            ],
            names=["date", "asset"],
        )
        turnover = ap.quantile_turnover(membership, 2, period=1)
        for horizon in HORIZONS:
            column = f"{horizon}D"
            data = base[["factor", "factor_quantile", column]].dropna(subset=[column])
            if data.empty:
                summaries.append(
                    dict(
                        factor=name,
                        horizon=horizon,
                        sample_count=0,
                        periods=0,
                        mean_rank_ic=None,
                        top_minus_bottom=None,
                        mean_top_turnover=finite(turnover.mean()),
                    )
                )
                continue
            with warnings.catch_warnings():
                warnings.simplefilter("ignore", FutureWarning)
                warnings.simplefilter("ignore", RuntimeWarning)
                ic = ap.factor_information_coefficient(data)[column]
                means, _ = ap.mean_return_by_quantile(data, by_date=True, demeaned=False)
            spreads = []
            for day, group in means.reset_index().groupby("date"):
                ordered = group.sort_values("factor_quantile")
                expected = base.xs(day, level="date").factor_quantile
                observed = ordered.set_index("factor_quantile")[column]
                if (
                    expected.min() < expected.max()
                    and expected.min() in observed
                    and expected.max() in observed
                ):
                    spreads.append(
                        float(observed.loc[expected.max()] - observed.loc[expected.min()])
                    )
                for rec in ordered.to_dict("records"):
                    quantile_rows.append(
                        {
                            "factor": name,
                            "horizon": horizon,
                            "date": day,
                            "quantile": int(rec["factor_quantile"]),
                            "mean_return": finite(rec[column]),
                        }
                    )
            ic_rows.extend(
                {"factor": name, "horizon": horizon, "date": day, "rank_ic": finite(v)}
                for day, v in ic.items()
            )
            summaries.append(
                {
                    "factor": name,
                    "horizon": horizon,
                    "sample_count": len(data),
                    "periods": int(ic.notna().sum()),
                    "mean_rank_ic": finite(ic.mean()),
                    "top_minus_bottom": finite(np.mean(spreads)) if spreads else None,
                    "mean_top_turnover": finite(turnover.mean()),
                }
            )
    return {
        "factor_horizons": pd.DataFrame(summaries, columns=SUMMARY_COLUMNS),
        "factor_horizon_ic": pd.DataFrame(
            ic_rows, columns=["factor", "horizon", "date", "rank_ic"]
        ),
        "factor_horizon_groups": pd.DataFrame(
            quantile_rows, columns=["factor", "horizon", "date", "quantile", "mean_return"]
        ),
    }, {
        "engine": "alphalens-reloaded",
        "version": version("alphalens-reloaded"),
        "horizons_trading_days": list(HORIZONS),
        "entry": "next benchmark trading day open",
        "exit": "entry plus N benchmark trading days open; bounded by research end",
        "quantiles": "oriented raw PIT factor; equal values stay together; empty groups omitted",
        "turnover": "top-quantile entrants / current top members between observed signal snapshots",
        "limitation": "descriptive forward returns, not executable portfolio P&L; not used for model selection",
    }
