"""Historical daily circulating market capitalization for mainland indices."""

from datetime import date, datetime
from decimal import Decimal, InvalidOperation
from zoneinfo import ZoneInfo

import requests

from ai_quant_assistant.providers.index_float_cap_sse import IndexFloatCap

LIXINGER_CMC = "lixinger:cn/index/fundamental:cmc"
ENDPOINT = "https://open.lixinger.com/api/cn/index/fundamental"


class LixingerIndexFloatCap:
    def __init__(self, token, client=requests):
        self._token = token
        self.client = client

    def fetch_range(self, index_code: str, start: date, end: date) -> list[IndexFloatCap]:
        if not self._token:
            raise ValueError("请在服务器配置 AI_QUANT_LIXINGER_TOKEN 并重启 Web 服务。")
        query = {"stockCodes": [index_code], "metricsList": ["cmc"], "token": self._token}
        if start == end:
            query["date"] = start.isoformat()
        else:
            query.update(startDate=start.isoformat(), endDate=end.isoformat())
        try:
            response = self.client.post(ENDPOINT, json=query, timeout=20, allow_redirects=False)
        except requests.RequestException:
            raise ValueError("理杏仁连接失败，请稍后重试。") from None
        if response.status_code != 200:
            raise ValueError(f"理杏仁返回 HTTP {response.status_code}，请检查 Token、接口权限和配额。")
        try:
            body = response.json(parse_float=Decimal)
        except ValueError:
            raise ValueError("理杏仁响应不是有效 JSON。") from None
        if not isinstance(body, dict) or body.get("code") != 1 or not isinstance(body.get("data"), list):
            raise ValueError("理杏仁未返回成功结果，请检查指数基本面接口权限、可查询日期和配额。")

        rows = []
        seen = set()
        for record in body["data"]:
            if not isinstance(record, dict) or record.get("stockCode") != index_code:
                raise ValueError("理杏仁返回了不匹配的指数代码。")
            try:
                stamp = datetime.fromisoformat(record["date"])
                day = stamp.astimezone(ZoneInfo("Asia/Shanghai")).date() if stamp.tzinfo else stamp.date()
            except (ValueError, TypeError, KeyError):
                raise ValueError("理杏仁交易日期无效。") from None
            if not start <= day <= end or day in seen:
                raise ValueError("理杏仁返回重复或区间外的交易日期。")
            seen.add(day)
            if record.get("cmc") is None:
                continue
            try:
                value = Decimal(str(record["cmc"]))
            except InvalidOperation:
                raise ValueError("理杏仁流通市值格式无效。") from None
            if not value.is_finite() or value <= 0:
                raise ValueError("理杏仁流通市值须为有限正数。")
            rows.append(IndexFloatCap(index_code, day, value, LIXINGER_CMC))
        return rows
