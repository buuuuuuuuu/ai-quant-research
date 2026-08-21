from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True, slots=True)
class Settings:
    data_dir: Path = Path("data")
    artifact_dir: Path = Path("artifacts")
    cache_dir: Path = Path("data/cache")
    duckdb_path: Path = Path("data/catalog.duckdb")
    akshare_retries: int = 3
    akshare_timeout_seconds: float = 30.0
    data_sync_rate_limit_seconds: float = 0.2
    log_level: str = "INFO"

    @classmethod
    def from_env(cls) -> Settings:
        data_dir = Path(os.getenv("AI_QUANT_DATA_DIR", "data"))
        return cls(
            data_dir=data_dir,
            artifact_dir=Path(os.getenv("AI_QUANT_ARTIFACT_DIR", "artifacts")),
            cache_dir=Path(os.getenv("AI_QUANT_CACHE_DIR", str(data_dir / "cache"))),
            duckdb_path=Path(
                os.getenv("AI_QUANT_DUCKDB_PATH", str(data_dir / "catalog.duckdb"))
            ),
            akshare_retries=int(os.getenv("AI_QUANT_AKSHARE_RETRIES", "3")),
            akshare_timeout_seconds=float(
                os.getenv("AI_QUANT_AKSHARE_TIMEOUT_SECONDS", "30")
            ),
            data_sync_rate_limit_seconds=float(
                os.getenv("AI_QUANT_DATA_SYNC_RATE_LIMIT_SECONDS", "0.2")
            ),
            log_level=os.getenv("AI_QUANT_LOG_LEVEL", "INFO"),
        )

    def ensure_directories(self) -> None:
        for name in ("raw", "silver", "gold", "snapshots"):
            (self.data_dir / name).mkdir(parents=True, exist_ok=True)
        self.artifact_dir.mkdir(parents=True, exist_ok=True)
        self.cache_dir.mkdir(parents=True, exist_ok=True)
        self.duckdb_path.parent.mkdir(parents=True, exist_ok=True)
