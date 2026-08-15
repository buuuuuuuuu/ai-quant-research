from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True, slots=True)
class Settings:
    artifact_dir: Path = Path("artifacts")
    cache_dir: Path = Path("data/cache")
    duckdb_path: Path = Path("data/catalog.duckdb")
    akshare_retries: int = 3
    akshare_timeout_seconds: float = 30.0
    log_level: str = "INFO"

    @classmethod
    def from_env(cls) -> Settings:
        return cls(
            artifact_dir=Path(os.getenv("AI_QUANT_ARTIFACT_DIR", "artifacts")),
            cache_dir=Path(os.getenv("AI_QUANT_CACHE_DIR", "data/cache")),
            duckdb_path=Path(os.getenv("AI_QUANT_DUCKDB_PATH", "data/catalog.duckdb")),
            akshare_retries=int(os.getenv("AI_QUANT_AKSHARE_RETRIES", "3")),
            akshare_timeout_seconds=float(
                os.getenv("AI_QUANT_AKSHARE_TIMEOUT_SECONDS", "30")
            ),
            log_level=os.getenv("AI_QUANT_LOG_LEVEL", "INFO"),
        )

    def ensure_directories(self) -> None:
        self.artifact_dir.mkdir(parents=True, exist_ok=True)
        self.cache_dir.mkdir(parents=True, exist_ok=True)
        self.duckdb_path.parent.mkdir(parents=True, exist_ok=True)
