"""Versioned, offline performance analytics. Never downloads a benchmark."""

from __future__ import annotations

import warnings
from importlib.metadata import version

import numpy as np
import pandas as pd

PERFORMANCE_VERSION = "quantstats-v1"


def methodology() -> dict:
    return {
        "version": PERFORMANCE_VERSION,
        "quantstats": version("quantstats"),
        "annual_periods": 252,
        "risk_free_rate": 0.0,
        "returns": "adjacent observed NAV; first observation excluded; no filling",
        "win_rate": "positive daily returns / non-zero daily returns; not trade win rate",
        "rolling_window": 63,
    }


def finite(value):
    return float(value) if value is not None and np.isfinite(value) else None


def nav_returns(nav: pd.DataFrame, column: str) -> pd.Series:
    values = pd.to_numeric(nav[column], errors="raise").astype(float)
    dates = pd.DatetimeIndex(pd.to_datetime(nav["date"]))
    if dates.has_duplicates or not dates.is_monotonic_increasing:
        raise ValueError("绩效分析要求日期唯一且升序")
    if not np.isfinite(values).all() or (values <= 0).any():
        raise ValueError("绩效分析要求完整的正净值；不得填补缺失净值")
    return pd.Series(values.to_numpy(), index=dates).pct_change(fill_method=None).iloc[1:]


def performance_metrics(nav: pd.DataFrame) -> dict:
    if nav.empty:
        return {}
    from quantstats import stats as qs

    returns = nav_returns(nav, "portfolio_nav")
    benchmark = nav_returns(nav, "benchmark_nav")
    with warnings.catch_warnings(), np.errstate(all="ignore"):
        warnings.simplefilter("ignore", RuntimeWarning)
        volatility = (
            finite(qs.volatility(returns, periods=252, prepare_returns=False))
            if len(returns) > 1
            else None
        )
        drawdown = abs(float((nav.portfolio_nav / nav.portfolio_nav.cummax() - 1).min()))
        annualized = finite(qs.cagr(returns, periods=252)) if len(returns) else None
        result = {
            "total_return": float(nav.portfolio_nav.iloc[-1] / nav.portfolio_nav.iloc[0] - 1),
            "benchmark_total_return": float(
                nav.benchmark_nav.iloc[-1] / nav.benchmark_nav.iloc[0] - 1
            ),
            "annualized_return": annualized,
            "benchmark_annualized_return": finite(qs.cagr(benchmark, periods=252))
            if len(benchmark)
            else None,
            "annualized_volatility": volatility,
            "benchmark_annualized_volatility": finite(
                qs.volatility(benchmark, periods=252, prepare_returns=False)
            )
            if len(benchmark) > 1
            else None,
            "sharpe_ratio": finite(qs.sharpe(returns, rf=0, periods=252))
            if volatility and volatility > 0
            else None,
            "sortino_ratio": finite(qs.sortino(returns, rf=0, periods=252))
            if (returns < 0).any()
            else None,
            "calmar_ratio": finite(annualized / drawdown)
            if annualized is not None and drawdown > 0
            else None,
            "max_drawdown": drawdown,
            "daily_win_rate": finite(qs.win_rate(returns, prepare_returns=False))
            if returns.ne(0).any()
            else None,
        }
    result["excess_return"] = result["total_return"] - result["benchmark_total_return"]
    if "portfolio_value" in nav:
        result["final_value"] = float(nav.portfolio_value.iloc[-1])
    return result


def performance_frames(nav: pd.DataFrame) -> dict[str, pd.DataFrame]:
    returns = nav_returns(nav, "portfolio_nav")
    benchmark = nav_returns(nav, "benchmark_nav")
    if returns.empty:
        return {
            "monthly_returns": pd.DataFrame(
                columns=["month", "strategy_return", "benchmark_return"]
            ),
            "rolling_risk": pd.DataFrame(
                {
                    "date": pd.Series(dtype="datetime64[ns]"),
                    "rolling_sharpe": pd.Series(dtype=float),
                    "rolling_volatility": pd.Series(dtype=float),
                }
            ),
        }
    monthly = pd.DataFrame({"strategy_return": returns, "benchmark_return": benchmark})
    monthly = monthly.resample("ME").agg(lambda x: (1 + x).prod() - 1 if len(x) else np.nan)
    monthly.index.name = "month"
    monthly = monthly.reset_index()
    monthly["month"] = monthly["month"].dt.strftime("%Y-%m")
    window = returns.rolling(63, min_periods=63)
    std = window.std(ddof=1).replace(0, np.nan)
    rolling = pd.DataFrame(
        {
            "date": returns.index,
            "rolling_sharpe": (window.mean() / std * np.sqrt(252)).to_numpy(),
            "rolling_volatility": (std * np.sqrt(252)).to_numpy(),
        }
    )
    return {"monthly_returns": monthly, "rolling_risk": rolling}


def save_performance(directory, nav):
    from ai_quant_assistant.storage.snapshots import write_json

    paths = {}
    for name, frame in performance_frames(nav).items():
        path = directory / f"{name}.csv"
        frame.to_csv(path, index=False, date_format="%Y-%m-%d")
        paths[f"{name}_csv"] = path
    path = directory / "performance_methodology.json"
    write_json(path, methodology())
    paths["performance_methodology_json"] = path
    return paths


def report_context(nav):
    frames = performance_frames(nav)
    monthly = (
        frames["monthly_returns"]
        .tail(120)
        .astype(object)
        .where(frames["monthly_returns"].tail(120).notna(), None)
        .to_dict("records")
    )
    rolling = frames["rolling_risk"].copy()
    rolling["date"] = rolling.date.dt.strftime("%Y-%m-%d")
    return {
        "methodology": methodology(),
        "monthly": monthly,
        "rolling": rolling.astype(object).where(rolling.notna(), None).to_dict("records"),
    }
