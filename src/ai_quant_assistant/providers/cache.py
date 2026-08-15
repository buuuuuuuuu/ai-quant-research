from __future__ import annotations

import hashlib
import json
import time
from collections.abc import Callable
from pathlib import Path
from typing import Any

import pandas as pd

from ai_quant_assistant.errors import ProviderError


class DataFrameCache:
    def __init__(self, root: Path) -> None:
        self.root = root
        self.root.mkdir(parents=True, exist_ok=True)

    @staticmethod
    def _key(namespace: str, params: dict[str, Any]) -> str:
        canonical = json.dumps(params, ensure_ascii=False, sort_keys=True, default=str)
        return hashlib.sha256(f"{namespace}:{canonical}".encode()).hexdigest()[:24]

    def paths(self, namespace: str, params: dict[str, Any]) -> tuple[Path, Path]:
        key = self._key(namespace, params)
        namespace_dir = self.root / namespace
        namespace_dir.mkdir(parents=True, exist_ok=True)
        return namespace_dir / f"{key}.csv.gz", namespace_dir / f"{key}.json"

    def get(self, namespace: str, params: dict[str, Any]) -> pd.DataFrame | None:
        data_path, _ = self.paths(namespace, params)
        if not data_path.exists():
            return None
        frame = pd.read_csv(data_path)
        frame.attrs["cache_status"] = "hit"
        return frame

    def put(self, namespace: str, params: dict[str, Any], frame: pd.DataFrame) -> None:
        data_path, meta_path = self.paths(namespace, params)
        frame.to_csv(data_path, index=False, compression="gzip", date_format="%Y-%m-%d")
        meta = {"namespace": namespace, "params": params, "rows": len(frame)}
        meta_path.write_text(json.dumps(meta, ensure_ascii=False, indent=2, default=str), "utf-8")


def load_or_fetch(
    *,
    cache: DataFrameCache,
    namespace: str,
    params: dict[str, Any],
    fetcher: Callable[[], pd.DataFrame],
    retries: int,
    refresh: bool = False,
) -> pd.DataFrame:
    cached = cache.get(namespace, params)
    if cached is not None and not refresh:
        return cached

    error: Exception | None = None
    for attempt in range(max(1, retries)):
        try:
            frame = fetcher()
            cache.put(namespace, params, frame)
            frame.attrs["cache_status"] = "refreshed"
            return frame
        except Exception as exc:  # noqa: BLE001 - provider boundary records original failure
            error = exc
            if attempt + 1 < max(1, retries):
                time.sleep(2**attempt)

    if cached is not None:
        cached.attrs["cache_status"] = "stale_fallback"
        return cached
    raise ProviderError(f"{namespace} failed after {retries} attempts: {error}") from error

