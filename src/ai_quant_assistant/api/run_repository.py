from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any

import pandas as pd

RUN_ID_RE = re.compile(r"^factor-backtest-\d{8}T\d{6}Z-[0-9a-f]{8}$")
DOWNLOAD_FILES = {
    "daily_nav.csv": "text/csv",
    "trades.csv": "text/csv",
    "positions.csv": "text/csv",
    "factor_selections.csv": "text/csv",
    "metrics.json": "application/json",
    "backtest_result.xlsx": "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
    "report.html": "text/html",
    "run.json": "application/json",
}
LAB_RUN_ID_RE = re.compile(r"^factor-lab-\d{8}T\d{6}Z-[0-9a-f]{8}$")
LAB_DOWNLOAD_FILES = {
    **{
        f"{item}.csv": "text/csv"
        for item in (
            "strategies",
            "segments",
            "cross_sections",
            "ic_series",
            "ic_summary",
            "group_returns",
            "coverage",
            "yearly",
            "walk_forward",
            "curves",
        )
    },
    "diagnostics.json": "application/json",
    "factor_lab.xlsx": "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
    "report.html": "text/html",
    "run.json": "application/json",
}


class BacktestRunRepository:
    def __init__(self, artifact_dir: Path) -> None:
        self.root = (artifact_dir / "backtests").resolve()
        self.root.mkdir(parents=True, exist_ok=True)

    def run_dir(self, run_id: str) -> Path:
        if not RUN_ID_RE.fullmatch(run_id):
            raise KeyError(run_id)
        directory = (self.root / run_id).resolve()
        if directory.parent != self.root or not directory.is_dir():
            raise KeyError(run_id)
        return directory

    def manifest(self, run_id: str) -> dict[str, Any]:
        path = self.run_dir(run_id) / "run.json"
        if not path.is_file():
            raise KeyError(run_id)
        return json.loads(path.read_text(encoding="utf-8"))

    def list(self, *, limit: int = 100) -> list[dict[str, Any]]:
        rows = []
        manifests = sorted(
            self.root.glob("factor-backtest-*/run.json"),
            key=lambda path: path.stat().st_mtime,
            reverse=True,
        )
        for path in manifests[:limit]:
            try:
                payload = json.loads(path.read_text(encoding="utf-8"))
                rows.append(
                    {
                        "run_id": payload["run_id"],
                        "status": payload.get("status", "unknown"),
                        "completed_at": payload.get("completed_at"),
                        "duration_seconds": payload.get("duration_seconds"),
                        "spec": payload.get("spec", {}),
                        "metrics": payload.get("metrics", {}),
                    }
                )
            except (OSError, KeyError, ValueError, json.JSONDecodeError):
                continue
        return rows

    @staticmethod
    def _records(
        path: Path, *, limit: int = 2_000, downsample: bool = False
    ) -> list[dict[str, Any]]:
        if not path.is_file() or path.stat().st_size == 0:
            return []
        frame = pd.read_csv(path)
        if downsample and len(frame) > limit:
            indexes = pd.Series(range(len(frame))).iloc[
                [round(item) for item in pd.Series(range(limit)) * (len(frame) - 1) / (limit - 1)]
            ]
            frame = frame.iloc[indexes.to_numpy()]
        elif len(frame) > limit:
            frame = frame.tail(limit)
        frame = frame.astype(object).where(pd.notna(frame), None)
        return frame.to_dict(orient="records")

    def detail(self, run_id: str) -> dict[str, Any]:
        directory = self.run_dir(run_id)
        payload = self.manifest(run_id)
        return {
            **payload,
            "series": self._records(directory / "daily_nav.csv", downsample=True),
            "trades": self._records(directory / "trades.csv"),
            "positions": self._records(directory / "positions.csv"),
            "selections": self._records(directory / "factor_selections.csv"),
            "downloads": list(DOWNLOAD_FILES),
            "report_url": f"/reports/backtests/{run_id}",
        }

    def file(self, run_id: str, filename: str) -> tuple[Path, str]:
        media_type = DOWNLOAD_FILES.get(filename)
        if media_type is None:
            raise KeyError(filename)
        path = self.run_dir(run_id) / filename
        if not path.is_file():
            raise KeyError(filename)
        return path, media_type


class FactorLabRunRepository(BacktestRunRepository):
    def __init__(self, artifact_dir: Path) -> None:
        self.root = (artifact_dir / "factor-lab").resolve()
        self.root.mkdir(parents=True, exist_ok=True)

    def run_dir(self, run_id: str) -> Path:
        if not LAB_RUN_ID_RE.fullmatch(run_id):
            raise KeyError(run_id)
        directory = (self.root / run_id).resolve()
        if directory.parent != self.root or not directory.is_dir():
            raise KeyError(run_id)
        return directory

    def list(self, *, limit: int = 100) -> list[dict[str, Any]]:
        results = []
        for path in sorted(
            self.root.glob("factor-lab-*/run.json"),
            key=lambda item: item.stat().st_mtime,
            reverse=True,
        )[:limit]:
            try:
                payload = json.loads(path.read_text(encoding="utf-8"))
                results.append(
                    {
                        key: payload.get(key)
                        for key in (
                            "run_id",
                            "status",
                            "completed_at",
                            "duration_seconds",
                            "spec",
                            "summary",
                            "ic_summary",
                        )
                    }
                )
            except (OSError, KeyError, ValueError, json.JSONDecodeError):
                continue
        return results

    def detail(self, run_id: str) -> dict[str, Any]:
        directory = self.run_dir(run_id)
        payload = self.manifest(run_id)
        return {
            **payload,
            "series": self._records(directory / "ic_series.csv", limit=2000),
            "group_returns": self._records(directory / "group_returns.csv", limit=2000),
            "coverage": self._records(directory / "coverage.csv", limit=2000),
            "segments": self._records(directory / "segments.csv", limit=2000),
            "yearly": self._records(directory / "yearly.csv", limit=2000),
            "walk_forward": self._records(directory / "walk_forward.csv", limit=2000),
            "curves": self._records(directory / "curves.csv", limit=5000, downsample=True),
            "downloads": list(LAB_DOWNLOAD_FILES),
            "report_url": f"/reports/factor-lab/{run_id}",
        }

    def file(self, run_id: str, filename: str) -> tuple[Path, str]:
        media_type = LAB_DOWNLOAD_FILES.get(filename)
        if media_type is None:
            raise KeyError(filename)
        path = self.run_dir(run_id) / filename
        if not path.is_file():
            raise KeyError(filename)
        return path, media_type
