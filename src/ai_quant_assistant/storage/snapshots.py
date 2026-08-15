from __future__ import annotations

import hashlib
import json
from dataclasses import asdict, dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pandas as pd


@dataclass(frozen=True, slots=True)
class SnapshotRef:
    name: str
    path: str
    format: str
    rows: int
    sha256: str
    created_at: str

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


class SnapshotStore:
    def __init__(self, run_dir: Path) -> None:
        self.root = run_dir / "snapshots"
        self.root.mkdir(parents=True, exist_ok=True)

    @staticmethod
    def dataframe_digest(frame: pd.DataFrame) -> str:
        canonical = frame.to_csv(index=False, date_format="%Y-%m-%d", lineterminator="\n")
        return hashlib.sha256(canonical.encode("utf-8")).hexdigest()

    def save(self, name: str, frame: pd.DataFrame) -> SnapshotRef:
        path = self.root / f"{name}.parquet"
        data_format = "parquet"
        try:
            frame.to_parquet(path, index=False)
        except (ImportError, ModuleNotFoundError):
            path = self.root / f"{name}.csv.gz"
            data_format = "csv.gz"
            frame.to_csv(path, index=False, compression="gzip", date_format="%Y-%m-%d")
        ref = SnapshotRef(
            name=name,
            path=str(path),
            format=data_format,
            rows=len(frame),
            sha256=self.dataframe_digest(frame),
            created_at=datetime.now(UTC).isoformat(),
        )
        return ref


def write_json(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2, default=str), encoding="utf-8"
    )
    temporary.replace(path)

