"""TA-Lib adapters, preserving gaps and the legacy pandas MA semantics."""

from __future__ import annotations

import numpy as np
import pandas as pd

TA_KINDS = {"rsi_threshold", "atr_threshold", "macd_cross", "bollinger"}
INDICATOR_PATTERN = r"rsi_\d+|atr_\d+|macd|macd_signal|macd_hist|bb_(?:upper|middle|lower)_\d+"


def required_observations(rules, windows=()) -> int:
    required = max(windows, default=2) + 1
    for rule in rules:
        item = rule if isinstance(rule, dict) else rule.to_dict()
        if item["kind"] == "macd_cross":
            required = max(required, 35)  # 26 + 9 - 2 lookback + current and previous
        else:
            n = max(item.get(k) or 0 for k in ("window", "short_window", "long_window"))
            required = max(
                required, n + (2 if item["kind"] in {"rsi_threshold", "atr_threshold"} else 1)
            )
    return required


def add_indicators(frame: pd.DataFrame, rules) -> pd.DataFrame:
    import talib

    specs = {(rule.kind, rule.window) for rule in rules if rule.kind in TA_KINDS}
    close = pd.to_numeric(frame.signal_close, errors="coerce")
    for kind, window in sorted(specs, key=str):
        fields = ["signal_close"] + (
            ["signal_high", "signal_low"] if kind == "atr_threshold" else []
        )
        values = frame.reindex(columns=fields).apply(pd.to_numeric, errors="coerce")
        valid = pd.Series(np.isfinite(values).all(axis=1), index=frame.index) & values.gt(0).all(
            axis=1
        )
        if kind == "atr_threshold":
            valid &= (
                values.signal_high.ge(values.signal_low)
                & values.signal_high.ge(close)
                & values.signal_low.le(close)
            )
        groups = (~valid).cumsum()
        names = {
            "rsi_threshold": [f"rsi_{window}"],
            "atr_threshold": [f"atr_{window}"],
            "macd_cross": ["macd", "macd_signal", "macd_hist"],
            "bollinger": [f"bb_upper_{window}", f"bb_middle_{window}", f"bb_lower_{window}"],
        }[kind]
        for name in names:
            frame[name] = np.nan
        for _, block in values.loc[valid].groupby(groups.loc[valid]):
            c = block.signal_close.to_numpy(dtype=float)
            if kind == "rsi_threshold":
                outputs = [talib.RSI(c, timeperiod=window)]
            elif kind == "atr_threshold":
                outputs = [
                    talib.ATR(
                        block.signal_high.to_numpy(dtype=float),
                        block.signal_low.to_numpy(dtype=float),
                        c,
                        timeperiod=window,
                    )
                ]
            elif kind == "macd_cross":
                outputs = talib.MACD(c, fastperiod=12, slowperiod=26, signalperiod=9)
            else:
                outputs = talib.BBANDS(c, timeperiod=window, nbdevup=2, nbdevdn=2, matype=0)
            for name, output in zip(names, outputs, strict=True):
                frame.loc[block.index, name] = output
    return frame


def evaluate(rule, current, before):
    if rule.kind == "macd_cross":
        keys = ("macd", "macd_signal")
        evidence = {k: current[k] for k in keys}
        evidence.update({f"previous_{k}": before[k] for k in keys})
        if not all(np.isfinite(v) for v in evidence.values()):
            return None, evidence
        if rule.operator == "cross_above":
            matched = before.macd <= before.macd_signal and current.macd > current.macd_signal
        else:
            matched = before.macd >= before.macd_signal and current.macd < current.macd_signal
        return bool(matched), evidence
    key = (
        f"rsi_{rule.window}"
        if rule.kind == "rsi_threshold"
        else f"atr_{rule.window}"
        if rule.kind == "atr_threshold"
        else f"bb_{'upper' if rule.operator == 'above' else 'lower'}_{rule.window}"
    )
    left, right = (
        (current.signal_close, current[key])
        if rule.kind == "bollinger"
        else (current[key], rule.threshold)
    )
    evidence = {key: current[key], "left": left, "right": right}
    if not np.isfinite(left) or not np.isfinite(right):
        return None, evidence
    return bool(left > right if rule.operator == "above" else left < right), evidence
