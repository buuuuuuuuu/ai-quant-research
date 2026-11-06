"""Local display cache for Shanghai Gold Exchange Au99.99 daily closes."""

import json
import math
from datetime import UTC, date, datetime
from pathlib import Path
from tempfile import NamedTemporaryFile
from threading import Lock

from curl_cffi import requests

SYMBOL = "Au99.99"
SOURCE = "https://www.sge.com.cn/sjzx/mrhq"
ENDPOINT = "https://www.sge.com.cn/graph/Dailyhq"


class GoldSourceError(ValueError):
    pass


def fetch_closes():
    try:
        response = requests.post(
            ENDPOINT,
            data={"instid": SYMBOL},
            headers={"Referer": SOURCE, "X-Requested-With": "XMLHttpRequest"},
            impersonate="chrome",
            timeout=20,
        )
        response.raise_for_status()
        payload = response.json()
    except (requests.RequestsError, ValueError) as exc:
        raise GoldSourceError("无法读取上海黄金交易所行情，请稍后重试。") from exc
    rows = payload.get("time") if isinstance(payload, dict) else None
    if not isinstance(rows, list):
        raise GoldSourceError("交易所行情格式已变化，请检查数据源。")

    closes = {}
    for row in rows:
        if not isinstance(row, list) or len(row) != 5:
            raise GoldSourceError("交易所日线字段与预期不符。")
        # The SGE history feed orders columns as date, open, close, low, high.
        try:
            day = date.fromisoformat(str(row[0]).replace("/", "-")).isoformat()
            raw = row[2]
            value = None if raw in (None, "", "-", "--") else float(raw)
        except (ValueError, TypeError, OverflowError) as exc:
            raise GoldSourceError("交易所返回了无法识别的日期或价格。") from exc
        close = value if value is not None and math.isfinite(value) and value > 0 else None
        if day in closes and closes[day] != close:
            raise GoldSourceError("交易所同一天的收盘价有冲突。")
        closes[day] = close
    if not any(closes.values()):
        raise GoldSourceError("交易所暂未返回有效收盘价。")
    return closes


class GoldHistory:
    def __init__(self, cache_dir: Path):
        self.path = cache_dir / "sge-au9999-daily.json"
        self.update_lock = Lock()

    def read(self):
        try:
            return json.loads(self.path.read_text(encoding="utf-8"))
        except FileNotFoundError:
            return {"updated_at": None, "closes": {}}

    def refresh(self):
        if not self.update_lock.acquire(blocking=False):
            raise BlockingIOError("行情正在更新，请稍后刷新页面。")
        try:
            incoming = fetch_closes()
            snapshot = self.read()
            closes = snapshot["closes"] | incoming
            snapshot = {
                "updated_at": datetime.now(UTC).isoformat(timespec="seconds"),
                "closes": dict(sorted(closes.items())),
            }
            self.path.parent.mkdir(parents=True, exist_ok=True)
            with NamedTemporaryFile(dir=self.path.parent, delete=False) as tmp:
                temp_path = Path(tmp.name)
            try:
                temp_path.write_text(json.dumps(snapshot, allow_nan=False), encoding="utf-8")
                temp_path.replace(self.path)
            finally:
                temp_path.unlink(missing_ok=True)
        finally:
            self.update_lock.release()
