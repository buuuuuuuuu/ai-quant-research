"""Bounded CNINFO transport; retain existing report normalization and identities."""
from __future__ import annotations

import json
import math
import time
from datetime import datetime

import pandas as pd
import requests

from ai_quant_assistant.errors import ProviderError

BASE = "https://www.cninfo.com.cn"
COLUMNS = ["代码", "简称", "公告标题", "公告时间", "公告链接"]
CATEGORIES = {
    "年报": "category_ndbg_szsh", "半年报": "category_bndbg_szsh",
    "一季报": "category_yjdbg_szsh", "三季报": "category_sjdbg_szsh",
}


class AnnouncementResponseError(ProviderError):
    def __init__(self, stage: str, reason: str, *, status: int | None = None) -> None:
        self.stage, self.reason, self.status = stage, reason, status
        self.retryable = status not in {301, 302, 303, 307, 308, 400, 401, 403, 404, 429}
        self.stop_batch = status in {401, 403, 429}
        self.fingerprint = (stage, reason, status)
        labels = {
            "network": "连接失败或超时", "http": "HTTP 请求失败",
            "redirect": "接口发生跳转", "empty": "响应为空",
            "json": "响应不是有效 JSON（可能为拦截页或上游异常）",
            "schema": "响应字段不完整", "limit": "响应超出安全大小或分页上限",
            "pagination": "分页数量变化、重复或缺失", "symbol": "股票代码未在公告目录中找到",
        }
        # Never include the response body, cookies, headers or request exception text.
        action = (
            "；请核对股票代码及公告目录，未将该范围记为完整。" if reason == "symbol"
            else "；请缩小查询日期范围后重试。" if reason == "limit" and stage == "公告查询"
            else "；请检查服务器到 www.cninfo.com.cn 的访问，稍后重试。"
        )
        super().__init__(
            f"巨潮公告接口（{stage}）：{labels.get(reason, reason)}"
            + (f"，HTTP {status}" if status is not None else "")
            + action
        )


def announcement_error(exc: Exception) -> AnnouncementResponseError | None:
    """Keep typed diagnostics through load_or_fetch's ProviderError wrapper."""
    seen = set()
    while exc is not None and id(exc) not in seen:
        if isinstance(exc, AnnouncementResponseError):
            return exc
        seen.add(id(exc))
        exc = exc.__cause__
    return None


class CninfoAnnouncements:
    max_bytes = 8 * 1024 * 1024
    max_pages = 100
    page_size = 30

    def __init__(self, *, timeout_seconds: float = 30.0) -> None:
        self.timeout = max(1.0, min(float(timeout_seconds), 60.0))
        self._organizations: dict[str, str] | None = None

    def _request(self, session, method: str, path: str, stage: str, **kwargs):
        started = time.monotonic()
        try:
            with session.request(
                method, BASE + path, timeout=(min(10.0, self.timeout), self.timeout),
                allow_redirects=False, stream=True, **kwargs,
            ) as response:
                status = response.status_code
                if 300 <= status < 400:
                    raise AnnouncementResponseError(stage, "redirect", status=status)
                if status != 200:
                    raise AnnouncementResponseError(stage, "http", status=status)
                body = bytearray()
                for chunk in response.iter_content(65536):
                    if time.monotonic() - started > self.timeout * 2:
                        raise AnnouncementResponseError(stage, "network", status=status)
                    body.extend(chunk)
                    if len(body) > self.max_bytes:
                        raise AnnouncementResponseError(stage, "limit", status=status)
                if not body.strip():
                    raise AnnouncementResponseError(stage, "empty", status=status)
                try:
                    data = json.loads(body)
                except (ValueError, UnicodeError):
                    raise AnnouncementResponseError(stage, "json", status=status) from None
                if not isinstance(data, dict):
                    raise AnnouncementResponseError(stage, "schema", status=status)
                return data
        except requests.RequestException:
            raise AnnouncementResponseError(stage, "network") from None

    def fetch(self, *, symbol, market, category, start_date, end_date, keyword=""):
        if market != "沪深京" or category not in CATEGORIES or keyword:
            raise ValueError("unsupported announcement query")
        if len(symbol) != 6 or not symbol.isdigit():
            raise ValueError("unsupported announcement symbol")
        start = datetime.strptime(start_date, "%Y%m%d").date()
        end = datetime.strptime(end_date, "%Y%m%d").date()
        with requests.Session() as session:
            session.headers.update({"Accept": "application/json", "Referer": BASE + "/"})
            if self._organizations is None:
                catalog = self._request(session, "GET", "/new/data/szse_stock.json", "股票目录")
                rows = catalog.get("stockList")
                if not isinstance(rows, list) or not rows or any(
                    not isinstance(row, dict) or not row.get("code") or not row.get("orgId")
                    for row in rows
                ):
                    raise AnnouncementResponseError("股票目录", "schema")
                self._organizations = {str(row["code"]): str(row["orgId"]) for row in rows}
            if symbol not in self._organizations:
                raise AnnouncementResponseError("股票目录", "symbol")
            params = {
                "pageNum": "1", "pageSize": str(self.page_size), "column": "szse",
                "tabName": "fulltext", "plate": "", "stock": f"{symbol},{self._organizations[symbol]}",
                "searchkey": "", "secid": "", "category": CATEGORIES[category], "trade": "",
                "seDate": f"{start.isoformat()}~{end.isoformat()}",
                "sortName": "", "sortType": "", "isHLtitle": "true",
            }
            collected, seen = [], set()
            total = None
            page = 1
            while True:
                data = self._request(session, "POST", "/new/hisAnnouncement/query", "公告查询", data=params)
                count, items = data.get("totalAnnouncement"), data.get("announcements")
                # Some responses encode the total as a decimal string, as accepted by AkShare.
                if isinstance(count, str) and count.isascii() and count.isdigit() and len(count) <= 10:
                    count = int(count)
                if type(count) is not int or count < 0 or "announcements" not in data:
                    raise AnnouncementResponseError("公告查询", "schema")
                if count == 0 and items is None:
                    items = []
                if not isinstance(items, list):
                    raise AnnouncementResponseError("公告查询", "schema")
                if total is None:
                    total = count
                if total != count or len(items) != min(self.page_size, total - len(collected)):
                    raise AnnouncementResponseError("公告查询", "pagination")
                pages = max(1, math.ceil(total / self.page_size))
                if pages > self.max_pages:
                    raise AnnouncementResponseError("公告查询", "limit")
                for item in items:
                    if not isinstance(item, dict) or any(
                        item.get(key) is None for key in (
                            "secCode", "secName", "announcementTitle", "announcementTime",
                            "announcementId", "orgId",
                        )
                    ) or str(item["secCode"]) != symbol:
                        raise AnnouncementResponseError("公告查询", "schema")
                    identity = (str(item["secCode"]), str(item["announcementId"]))
                    if identity in seen:
                        raise AnnouncementResponseError("公告查询", "pagination")
                    seen.add(identity)
                    published = pd.to_datetime(item["announcementTime"], unit="ms", utc=True, errors="coerce")
                    if pd.isna(published):
                        raise AnnouncementResponseError("公告查询", "schema")
                    collected.append(item)
                if page >= pages:
                    break
                page += 1
                params = {**params, "pageNum": str(page)}
                time.sleep(0.2)
        if not collected:
            return pd.DataFrame(columns=COLUMNS)
        frame = pd.DataFrame(collected).rename(columns={
            "secCode": "代码", "secName": "简称", "announcementTitle": "公告标题",
            "announcementTime": "公告时间",
        })
        # Match AkShare's vector formatting, including all-midnight date-only strings.
        frame["公告时间"] = pd.to_datetime(frame["公告时间"], unit="ms", utc=True).dt.tz_convert(
            "Asia/Shanghai"
        ).dt.tz_localize(None).astype(str)
        frame["公告链接"] = [
            f"http://www.cninfo.com.cn/new/disclosure/detail?stockCode={code}&"
            f"announcementId={identity}&orgId={org}&announcementTime={published}"
            for code, identity, org, published in zip(
                frame["代码"], frame["announcementId"], frame["orgId"], frame["公告时间"], strict=True
            )
        ]
        return frame[COLUMNS]
