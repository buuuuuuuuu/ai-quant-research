"""Classify missing daily bars without inventing prices or suspension evidence."""

from __future__ import annotations

from datetime import date
from typing import Any

GAP_CLASSES = {"verified_suspension", "missing_bar", "calendar_mismatch", "unverified_gap"}


def normalize_evidence(raw: Any) -> dict[str, dict[str, Any]]:
    if not isinstance(raw, dict):
        return {}
    result: dict[str, dict[str, Any]] = {}
    for key, value in raw.get("dates", {}).items() if isinstance(raw.get("dates"), dict) else []:
        try:
            parsed = date.fromisoformat(str(key))
        except ValueError:
            continue
        if not isinstance(value, dict) or value.get("classification") not in GAP_CLASSES:
            continue
        result[str(parsed)] = dict(value)
    return result


def classify_gaps(
    missing_by_adjustment: dict[str, set[date]], evidence: Any
) -> dict[str, list[dict[str, Any]]]:
    """Classify gaps. A one-sided none/qfq gap is always a real local bar gap."""
    saved = normalize_evidence(evidence)
    result: dict[str, list[dict[str, Any]]] = {key: [] for key in missing_by_adjustment}
    for adjustment, missing in missing_by_adjustment.items():
        other_missing = set().union(
            *(dates for key, dates in missing_by_adjustment.items() if key != adjustment)
        )
        for day in sorted(missing):
            item = dict(saved.get(str(day), {}))
            if day not in other_missing:
                item = {
                    "classification": "missing_bar",
                    "reason": "另一复权口径存在该交易日行情，本口径缺少已发布记录",
                    "source": "local_adjustment_cross_check",
                }
            elif item.get("classification") not in GAP_CLASSES:
                item = {
                    "classification": "unverified_gap",
                    "reason": "交易所开市，但本地两种复权口径均无行情；尚无可靠停牌证据",
                    "source": None,
                }
            result[adjustment].append({"date": str(day), **item})
    return result


def blocking_gaps(items: list[dict[str, Any]]) -> list[dict[str, Any]]:
    return [item for item in items if item.get("classification") != "verified_suspension"]


def gap_summary(classified: dict[str, list[dict[str, Any]]]) -> dict[str, int]:
    unique: dict[tuple[str, str], None] = {}
    for items in classified.values():
        for item in items:
            unique[(item["date"], item["classification"])] = None
    return {
        kind: sum(1 for _, classification in unique if classification == kind)
        for kind in sorted(GAP_CLASSES)
    }
