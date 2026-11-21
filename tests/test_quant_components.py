import json
from datetime import date

import numpy as np
import pandas as pd
import pytest
import talib

from ai_quant_assistant.api.schemas import DailySignalRuleRequest
from ai_quant_assistant.domain.models import DailySignalRule, FactorComponent
from ai_quant_assistant.research.factor_analytics import analyze_factors
from ai_quant_assistant.research.performance import (
    nav_returns,
    performance_frames,
    performance_metrics,
)
from ai_quant_assistant.research.technical_indicators import (
    add_indicators,
    evaluate,
    required_observations,
)


def test_hand_calculated_sharpe_no_initial_zero():
    values = np.array([1, 1.1, 0.99, 1.188])
    nav = pd.DataFrame(
        {
            "date": pd.bdate_range("2024-01-01", periods=4),
            "portfolio_nav": values,
            "benchmark_nav": 1.0,
        }
    )
    metrics = performance_metrics(nav)
    returns = np.array([0.1, -0.1, 0.2])
    assert metrics["sharpe_ratio"] == pytest.approx(
        returns.mean() / returns.std(ddof=1) * np.sqrt(252)
    )
    assert metrics["max_drawdown"] == pytest.approx(0.1)
    assert metrics["total_return"] == pytest.approx(0.188)
    assert metrics["annualized_return"] == pytest.approx(1.188 ** (252 / 3) - 1)
    assert metrics["daily_win_rate"] == pytest.approx(2 / 3)
    frames = performance_frames(nav)
    assert frames["monthly_returns"].strategy_return.iloc[0] == pytest.approx(0.188)
    assert frames["rolling_risk"].rolling_sharpe.isna().all()
    json.dumps(metrics, allow_nan=False)


def test_flat_nav_undefined_ratios_and_no_fill():
    nav = pd.DataFrame(
        {
            "date": pd.bdate_range("2024-01-01", periods=3),
            "portfolio_nav": 1.0,
            "benchmark_nav": 1.0,
        }
    )
    result = performance_metrics(nav)
    assert result["sharpe_ratio"] is None
    assert result["sortino_ratio"] is None
    assert result["calmar_ratio"] is None
    nav.loc[1, "portfolio_nav"] = np.nan
    with pytest.raises(ValueError):
        nav_returns(nav, "portfolio_nav")


def frame(n=90):
    close = 100 + np.sin(np.arange(n) / 4) * 5 + np.arange(n) / 10
    return pd.DataFrame(
        {"signal_close": close, "signal_high": close + 2, "signal_low": close - 2},
        index=pd.bdate_range("2024-01-01", periods=n),
    )


def test_talib_gap_reset_and_independent_indicators():
    data = frame()
    rules = [
        DailySignalRule("rsi_threshold", "below", window=14, threshold=30),
        DailySignalRule("atr_threshold", "above", window=14, threshold=3),
        DailySignalRule("macd_cross", "cross_above"),
        DailySignalRule("bollinger", "above", window=20),
    ]
    result = add_indicators(data.copy(), rules)
    np.testing.assert_allclose(
        result.rsi_14, talib.RSI(data.signal_close.to_numpy(), 14), equal_nan=True
    )
    np.testing.assert_allclose(
        result.atr_14,
        talib.ATR(
            data.signal_high.to_numpy(),
            data.signal_low.to_numpy(),
            data.signal_close.to_numpy(),
            14,
        ),
        equal_nan=True,
    )
    dif, dea, hist = talib.MACD(data.signal_close.to_numpy(), 12, 26, 9)
    np.testing.assert_allclose(result.macd, dif, equal_nan=True)
    np.testing.assert_allclose(result.macd_signal, dea, equal_nan=True)
    data.iloc[40, 0] = np.nan
    result = add_indicators(data, rules)
    assert result.rsi_14.iloc[40:55].isna().all()
    assert pd.notna(result.rsi_14.iloc[55])
    assert required_observations(rules) == 35
    assert required_observations([rules[0]]) == 16
    matched, evidence = evaluate(rules[0], result.iloc[41], result.iloc[40])
    assert matched is None
    assert "rsi_14" in evidence


@pytest.mark.parametrize(
    "kind,operator,window,threshold",
    [
        ("rsi_threshold", "below", 14, 30),
        ("atr_threshold", "above", 14, 12),
        ("bollinger", "above", 20, None),
        ("macd_cross", "cross_below", None, None),
    ],
)
def test_new_api_rules(kind, operator, window, threshold):
    assert (
        DailySignalRuleRequest(kind=kind, operator=operator, window=window, threshold=threshold)
        .to_rule()
        .kind
        == kind
    )


@pytest.mark.parametrize(
    "kind,threshold",
    [
        ("rsi_threshold", 101),
        ("atr_threshold", -1),
        ("return_threshold", 11),
        ("rsi_threshold", float("nan")),
    ],
)
def test_invalid_thresholds(kind, threshold):
    with pytest.raises(ValueError):
        DailySignalRuleRequest(kind=kind, operator="above", window=14, threshold=threshold)


def test_alphalens_horizons_next_open_and_tail_missing():
    dates = pd.bdate_range("2024-01-01", periods=80)
    snapshots = pd.DataFrame(
        [
            {"signal_date": day, "symbol": symbol, "roe": score}
            for day in (dates[0], dates[20], dates[-2])
            for symbol, score in [("A", 1), ("B", 2), ("C", 3)]
        ]
    )
    prices = pd.DataFrame(
        [
            {"date": day, "symbol": symbol, "open": 100 * (1 + score / 100) ** i, "volume": 100}
            for i, day in enumerate(dates)
            for symbol, score in [("A", 1), ("B", 2), ("C", 3)]
        ]
    )
    output, metadata = analyze_factors(snapshots, prices, dates, [FactorComponent("roe")], 3)
    summary = output["factor_horizons"].set_index("horizon")
    assert summary.loc[5, "mean_rank_ic"] == pytest.approx(1)
    assert summary.loc[5, "sample_count"] == 6
    assert summary.loc[60, "sample_count"] == 3
    assert summary.loc[5, "top_minus_bottom"] == pytest.approx(1.03**5 - 1.01**5)
    assert summary.loc[5, "mean_top_turnover"] == 0
    groups = output["factor_horizon_groups"]
    assert groups.date.max() <= dates[20]
    # Future entry/exit gaps must not change PIT quantile assignment.
    prices = prices.loc[~((prices.symbol == "C") & (prices.date == dates[6]))]
    output, _ = analyze_factors(snapshots.iloc[:3], prices, dates, [FactorComponent("roe")], 3)
    assert pd.isna(output["factor_horizons"].set_index("horizon").loc[5, "top_minus_bottom"])


def test_new_indicators_execute_and_persist(tmp_path):
    from test_daily_signal import _spec
    from test_factor_backtest import _database

    from ai_quant_assistant.research.daily_signal import DailySignalStrategyService

    database = _database(tmp_path)
    spec = _spec(
        start_date=date(2024, 2, 20),
        end_date=date(2024, 3, 8),
        buy_rules=(DailySignalRule("rsi_threshold", "above", window=2, threshold=0),),
        sell_rules=(DailySignalRule("macd_cross", "cross_below"),),
    )
    result = DailySignalStrategyService(database=database, artifact_dir=tmp_path / "runs").run(spec)
    indicators = pd.read_csv(result["artifacts"]["indicator_series_csv"])
    assert {"rsi_2", "macd", "macd_signal"} <= set(indicators)
    assert indicators.rsi_2.notna().any()
    assert result["diagnostics"]["performance_methodology"]["version"] == "quantstats-v1"
    assert "monthly_returns_csv" in result["artifacts"]
    signals = pd.read_csv(result["artifacts"]["signals_csv"])
    assert not signals.empty
    from pathlib import Path

    report = Path(result["artifacts"]["html_report"]).read_text()
    assert "report-rolling-risk" in report and "RSI" in report
    assert "drawIndicators" in report
