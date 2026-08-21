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
    backup_dir: Path | None = None
    web_username: str = "admin"
    web_password: str = ""
    web_session_secret: str = ""
    web_session_secure: bool = False
    web_session_ttl_seconds: int = 43_200
    sync_lookback_days: int = 7
    sync_announcement_lookback_days: int = 120
    sync_financial_lookback_days: int = 400
    sync_symbols: tuple[str, ...] = ()
    sync_benchmark_symbol: str = "csi000300"
    backup_retention_days: int = 30
    backup_minimum_count: int = 7
    alert_sync_stale_hours: int = 48
    alert_backup_stale_hours: int = 36
    alert_disk_free_percent: float = 10.0
    alert_login_failure_threshold: int = 5
    alert_webhook_url: str = ""
    alert_webhook_timeout_seconds: float = 5.0

    @property
    def resolved_backup_dir(self) -> Path:
        return self.backup_dir or self.data_dir / "backups"

    @staticmethod
    def _project_root() -> Path:
        current = Path.cwd().resolve()
        for candidate in (current, *current.parents):
            if (candidate / "pyproject.toml").is_file() and (
                candidate / "src" / "ai_quant_assistant"
            ).is_dir():
                return candidate
        return current

    @staticmethod
    def _catalog_rank(path: Path) -> tuple[int, int, int]:
        if not path.is_file():
            return (-1, -1, -1)

        try:
            import duckdb

            with duckdb.connect(str(path), read_only=True) as connection:
                available = {
                    str(row[0])
                    for row in connection.execute(
                        """
                        SELECT table_name FROM information_schema.tables
                        WHERE table_schema = 'main'
                        """
                    ).fetchall()
                }
                if "schema_migrations" not in available:
                    return (-1, -1, -1)
                version = int(
                    connection.execute(
                        "SELECT COALESCE(MAX(version), 0) FROM schema_migrations"
                    ).fetchone()[0]
                )
                published_rows = sum(
                    int(connection.execute(f'SELECT COUNT(*) FROM "{name}"').fetchone()[0])
                    for name in (
                        "published_daily_bar",
                        "published_benchmark_daily_bar",
                        "published_announcement",
                        "published_financial_report",
                        "published_financial_metric",
                    )
                    if name in available
                )
                security_count = (
                    int(connection.execute("SELECT COUNT(*) FROM security_master").fetchone()[0])
                    if "security_master" in available
                    else 0
                )
            return (version, published_rows, security_count)
        except Exception:  # noqa: BLE001 - an unreadable candidate must not block another catalog
            return (-1, -1, -1)

    @classmethod
    def _default_data_dir(cls, project_root: Path) -> Path:
        canonical = project_root / "data"
        nested = canonical / "data"
        candidates = (canonical, nested)
        return max(candidates, key=lambda root: cls._catalog_rank(root / "catalog.duckdb"))

    @classmethod
    def from_env(cls) -> Settings:
        project_root = cls._project_root()
        explicit_data_dir = os.getenv("AI_QUANT_DATA_DIR")
        explicit_duckdb_path = os.getenv("AI_QUANT_DUCKDB_PATH")
        if explicit_data_dir is not None:
            data_dir = Path(explicit_data_dir)
        elif explicit_duckdb_path is not None:
            data_dir = Path(explicit_duckdb_path).parent
        else:
            data_dir = cls._default_data_dir(project_root)

        return cls(
            data_dir=data_dir,
            artifact_dir=Path(
                os.getenv("AI_QUANT_ARTIFACT_DIR", str(project_root / "artifacts"))
            ),
            cache_dir=Path(os.getenv("AI_QUANT_CACHE_DIR", str(data_dir / "cache"))),
            duckdb_path=Path(explicit_duckdb_path or data_dir / "catalog.duckdb"),
            akshare_retries=int(os.getenv("AI_QUANT_AKSHARE_RETRIES", "3")),
            akshare_timeout_seconds=float(
                os.getenv("AI_QUANT_AKSHARE_TIMEOUT_SECONDS", "30")
            ),
            data_sync_rate_limit_seconds=float(
                os.getenv("AI_QUANT_DATA_SYNC_RATE_LIMIT_SECONDS", "0.2")
            ),
            log_level=os.getenv("AI_QUANT_LOG_LEVEL", "INFO"),
            backup_dir=Path(os.getenv("AI_QUANT_BACKUP_DIR", str(data_dir / "backups"))),
            web_username=os.getenv("AI_QUANT_WEB_USERNAME", "admin"),
            web_password=os.getenv("AI_QUANT_WEB_PASSWORD", ""),
            web_session_secret=os.getenv("AI_QUANT_WEB_SESSION_SECRET", ""),
            web_session_secure=os.getenv("AI_QUANT_WEB_SESSION_SECURE", "false").lower()
            in {"1", "true", "yes", "on"},
            web_session_ttl_seconds=int(os.getenv("AI_QUANT_WEB_SESSION_TTL_SECONDS", "43200")),
            sync_lookback_days=int(os.getenv("AI_QUANT_SYNC_LOOKBACK_DAYS", "7")),
            sync_announcement_lookback_days=int(
                os.getenv("AI_QUANT_SYNC_ANNOUNCEMENT_LOOKBACK_DAYS", "120")
            ),
            sync_financial_lookback_days=int(
                os.getenv("AI_QUANT_SYNC_FINANCIAL_LOOKBACK_DAYS", "400")
            ),
            sync_symbols=tuple(
                symbol.strip()
                for symbol in os.getenv("AI_QUANT_SYNC_SYMBOLS", "").replace(",", " ").split()
                if symbol.strip()
            ),
            sync_benchmark_symbol=os.getenv("AI_QUANT_SYNC_BENCHMARK", "csi000300"),
            backup_retention_days=int(os.getenv("AI_QUANT_BACKUP_RETENTION_DAYS", "30")),
            backup_minimum_count=int(os.getenv("AI_QUANT_BACKUP_MINIMUM_COUNT", "7")),
            alert_sync_stale_hours=int(os.getenv("AI_QUANT_ALERT_SYNC_STALE_HOURS", "48")),
            alert_backup_stale_hours=int(os.getenv("AI_QUANT_ALERT_BACKUP_STALE_HOURS", "36")),
            alert_disk_free_percent=float(os.getenv("AI_QUANT_ALERT_DISK_FREE_PERCENT", "10")),
            alert_login_failure_threshold=int(
                os.getenv("AI_QUANT_ALERT_LOGIN_FAILURE_THRESHOLD", "5")
            ),
            alert_webhook_url=os.getenv("AI_QUANT_ALERT_WEBHOOK_URL", ""),
            alert_webhook_timeout_seconds=float(
                os.getenv("AI_QUANT_ALERT_WEBHOOK_TIMEOUT_SECONDS", "5")
            ),
        )

    def ensure_directories(self) -> None:
        for name in ("raw", "silver", "gold", "snapshots"):
            (self.data_dir / name).mkdir(parents=True, exist_ok=True)
        self.artifact_dir.mkdir(parents=True, exist_ok=True)
        self.cache_dir.mkdir(parents=True, exist_ok=True)
        self.duckdb_path.parent.mkdir(parents=True, exist_ok=True)
