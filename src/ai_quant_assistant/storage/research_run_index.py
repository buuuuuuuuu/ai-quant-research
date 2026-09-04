"""Small immutable index written by producers; never walk artifact directories."""

from __future__ import annotations

import json
import math
import re


def canonical(value):
    return json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        default=str,
        allow_nan=False,
    )


def frozen_symbols(spec):
    if not isinstance(spec, dict):
        return []
    pool = spec.get("stock_pool") or {}
    values = pool.get("resolved_symbols") or spec.get("symbols") or [spec.get("symbol")]
    if not isinstance(values, list) or len(values) > 500:
        return []
    if not values or any(
        not isinstance(x, str) or not re.fullmatch(r"[0-9]{6}", x) for x in values
    ):
        return []
    return list(dict.fromkeys(values))


def register_run(database, kind, payload):
    spec = payload["spec"]
    symbols = frozen_symbols(spec)
    # Dynamic universes need actual selected symbols, not their seed input.
    if spec.get("universe_mode", "explicit") != "explicit":
        return
    if not symbols:
        return
    result = {
        key: payload[key]
        for key in ("status", "metrics", "summary", "pooled_summary")
        if key in payload
    }
    missing = []

    def normalize(value, path="result"):
        if isinstance(value, float) and not math.isfinite(value):
            missing.append(path)
            return None
        if isinstance(value, dict):
            return {key: normalize(item, f"{path}.{key}") for key, item in value.items()}
        if isinstance(value, (list, tuple)):
            return [normalize(item, f"{path}[{index}]") for index, item in enumerate(value)]
        return value

    result = normalize(result)
    if missing:
        result["missing_statistics"] = missing
        result["warning"] = "样本不足等原因产生的非有限统计量保留为 null，不填零。"
    values = [
        payload.get("run_id") or payload["batch_id"],
        kind,
        canonical(symbols),
        canonical(spec),
        canonical(result),
        payload["completed_at"],
    ]
    with database.connect() as connection:
        existing = connection.execute(
            "SELECT research_type,symbols_json,spec_json,result_json FROM stock_research_run WHERE run_id=?",
            [values[0]],
        ).fetchone()
        if existing:
            if tuple(values[1:5]) != existing:
                raise ValueError("研究索引冲突：不可覆盖已冻结输入或结果")
            return
        connection.execute("INSERT INTO stock_research_run VALUES (?, ?, ?, ?, ?, ?)", values)
