"""Reconstruct A-share circulating cap from dated public membership and daily valuations."""

import hashlib
import re
from datetime import date
from decimal import Decimal, InvalidOperation

import requests
from bs4 import BeautifulSoup

MEMBER_COUNTS = {"000905": 500, "000852": 1000, "932000": 2000, "000510": 500, "399006": 100}
PUBLIC_CAP_SOURCE = "sina:dated_index_members+eastmoney:NOTLIMITED_MARKETCAP_A"
SINA = "https://vip.stock.finance.sina.com.cn/corp"
EASTMONEY = "https://datacenter-web.eastmoney.com/api/data/v1/get"


class PublicMemberCap:
    def __init__(self, client=requests):
        self.client = client

    def membership(self, index_code):
        intervals = []
        for kind in ("Newest", "History"):
            page = 1
            total_pages = None
            seen = set()
            while True:
                endpoint = f"vII_{kind}Component"
                if page == 1:
                    url = f"{SINA}/go.php/{endpoint}/indexid/{index_code}.phtml"
                    params = {}
                else:
                    url = f"{SINA}/view/{endpoint}.php"
                    params = {"indexid": index_code, "page": page}
                response = self.client.get(url, params=params, timeout=20)
                response.raise_for_status()
                try:
                    html = response.content.decode("utf-8-sig")
                except UnicodeDecodeError:
                    html = response.content.decode("gb18030", errors="replace")
                soup = BeautifulSoup(html, "html.parser")
                text = soup.get_text(" ", strip=True)
                if kind == "History" and page == 1 and "暂时没有相关记录" in text:
                    break
                if (
                    not soup.title
                    or index_code not in soup.title.get_text()
                    or "纳入日期" not in text
                ):
                    raise ValueError("新浪成分页缺少指数标识或纳入日期，请稍后重试")
                paging = re.search(r"第\s*(\d+)\s*页\s*[,，]\s*共\s*(\d+)\s*页", text)
                current, pages = map(int, paging.groups()) if paging else (1, 1)
                if current != page or not page <= pages <= 500 or total_pages not in (None, pages):
                    raise ValueError("新浪成分记录分页发生变化，请重试")
                total_pages = pages
                before = len(seen)
                for tr in soup.find_all("tr"):
                    cells = [td.get_text(strip=True) for td in tr.find_all("td", recursive=False)]
                    if not cells or not re.fullmatch(r"\d{6}", cells[0]):
                        continue
                    if len(cells) != (3 if kind == "Newest" else 4):
                        raise ValueError("新浪成分表字段发生变化")
                    first = date.fromisoformat(cells[2]).isoformat()
                    last = date.fromisoformat(cells[3]).isoformat() if kind == "History" else None
                    row = (cells[0], first, last)
                    if last and last < first:
                        raise ValueError("新浪成分区间倒置")
                    if row in seen:
                        continue
                    seen.add(row)
                    intervals.append(row)
                if before == len(seen):
                    raise ValueError("新浪成分页没有有效记录")
                if page == total_pages:
                    break
                page += 1
        current_codes = [code for code, _, end in intervals if end is None]
        if len(current_codes) != MEMBER_COUNTS[index_code] or len(set(current_codes)) != len(
            current_codes
        ):
            # An incomplete current roster cannot anchor a historical reconstruction.
            return []
        return intervals

    def daily(self, index_code, day, intervals):
        stamp = day.isoformat()
        # Exclude removal dates: the publisher does not document whether the boundary is inclusive.
        if any(end == stamp for _, _, end in intervals):
            return None
        members = [
            code
            for code, start, end in intervals
            if start <= stamp and (end is None or stamp < end)
        ]
        required = set(members)
        if len(required) != MEMBER_COUNTS[index_code] or len(required) != len(members):
            return None
        caps = {}
        page = 1
        total_pages = None
        while True:
            response = self.client.get(
                EASTMONEY,
                params={
                    "reportName": "RPT_VALUEANALYSIS_DET",
                    "columns": "SECURITY_CODE,TRADE_DATE,NOTLIMITED_MARKETCAP_A",
                    "filter": f"(TRADE_DATE='{stamp}')",
                    "sortColumns": "SECURITY_CODE",
                    "sortTypes": "1",
                    "pageSize": "5000",
                    "pageNumber": str(page),
                    "source": "WEB",
                    "client": "WEB",
                },
                timeout=20,
            )
            response.raise_for_status()
            payload = response.json(parse_float=Decimal)
            if not isinstance(payload, dict):
                raise ValueError("东方财富估值接口响应格式无效")
            if payload.get("result") is None and "无返回数据" in str(payload.get("message")):
                if page == 1:
                    return None
                raise ValueError("东方财富估值后续分页缺失，请重试")
            if payload.get("success") is not True or not isinstance(payload.get("result"), dict):
                raise ValueError("东方财富估值接口未返回成功结果")
            result = payload["result"]
            rows = result.get("data")
            if rows == [] and page == 1 and result.get("count") == 0:
                return None
            pages = result.get("pages")
            if (
                not isinstance(pages, int)
                or not page <= pages <= 200
                or total_pages not in (None, pages)
            ):
                raise ValueError("东方财富估值分页不完整")
            total_pages = pages
            if not isinstance(rows, list) or not rows:
                raise ValueError("东方财富估值分页为空")
            for row in rows:
                code = row["SECURITY_CODE"]
                if date.fromisoformat(str(row["TRADE_DATE"]).split(" ")[0]) != day:
                    raise ValueError("东方财富返回了其他交易日的估值")
                if code not in required:
                    continue
                if code in caps:
                    raise ValueError("同一成分股存在重复估值")
                raw = row["NOTLIMITED_MARKETCAP_A"]
                try:
                    value = Decimal(str(raw)) if raw is not None else None
                except InvalidOperation as exc:
                    raise ValueError("东方财富个股流通市值格式无效") from exc
                caps[code] = value
            if page == pages:
                break
            page += 1
        if caps.keys() != required or any(
            v is None or not v.is_finite() or v <= 0 for v in caps.values()
        ):
            return None
        evidence = "\n".join(f"{code}:{caps[code]}" for code in sorted(required))
        return {
            "date": stamp,
            "value": str(sum(caps.values())),
            "members": len(required),
            "member_cap_sha256": hashlib.sha256(evidence.encode()).hexdigest(),
        }
