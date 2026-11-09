"""Public Eastmoney bars plus existing exchange share/NAV adapters. No browser calls."""

from datetime import date
from decimal import Decimal

import requests

from ai_quant_assistant.providers.index_fund_shares import IndexFundShareProvider


class NationalTeamProvider:
    def __init__(self):
        self.shares = IndexFundShareProvider()

    def bars(self, code, start, end, minute=False):
        market, symbol = code.split(".")
        secid = f"{1 if market in {'SSE', 'INDEX'} else 0}.{symbol}"
        params = {"secid": secid, "fields1": "f1,f2,f3,f4,f5,f6,f7,f8,f9,f10,f11",
                  "fields2": "f51,f52,f53,f54,f55,f56,f57,f58"}
        if minute:
            path = "trends2"
            params.update(ndays="5", iscr="0")
        else:
            path = "kline"
            params.update(klt="101", fqt="0", beg=start.replace("-", ""), end=end.replace("-", ""))
        response = requests.get(f"https://push2his.eastmoney.com/api/qt/stock/{path}/get",
                                params=params, timeout=20)
        response.raise_for_status()
        data = response.json().get("data")
        if not data:
            return []
        result = []
        for raw in data.get("trends" if minute else "klines", []):
            values = raw.split(",")
            stamp = values[0][:16] if minute else values[0]
            if not start <= stamp[:10] <= end:
                continue
            amount, close = Decimal(values[6]), Decimal(values[2])
            if not amount.is_finite() or amount < 0 or not close.is_finite() or close <= 0:
                raise ValueError("行情金额或价格无效")
            result.append({"kind": "minute" if minute else "daily", "code": code,
                           "stamp": stamp, "amount": str(amount), "close": str(close)})
        return result

    def fetch_shares(self, exchange, day, codes):
        if exchange == "SSE":
            response = requests.get("https://query.sse.com.cn/commonQuery.do", timeout=20,
                                    headers={"Referer": "https://www.sse.com.cn/", "User-Agent": "Mozilla/5.0"},
                                    params={"isPagination": "true", "pageHelp.pageSize": "10000", "pageHelp.pageNo": "1",
                                            "sqlId": "COMMON_SSE_ZQPZ_ETFZL_XXPL_ETFGM_SEARCH_L", "STAT_DATE": day})
            response.raise_for_status()
            payload = response.json()
            records = payload.get("result", [])
            if int(payload.get("pageHelp", {}).get("total", len(records))) > len(records):
                raise ValueError("上交所份额响应被截断")
            result = []
            for row in records:
                if row["STAT_DATE"][:10] != day:
                    raise ValueError("上交所返回了不同统计日期")
                code = "SSE." + row["SEC_CODE"]
                if code in codes:
                    result.append({"kind": "shares", "code": code, "stamp": day,
                                   "shares": str(Decimal(str(row["TOT_VOL"])) * 10000)})
            return result
        return [{"kind": "shares", "code": row.fund_id, "stamp": day, "shares": str(row.shares)}
                for row in self.shares.fetch_day(exchange, date.fromisoformat(day)) if row.fund_id in codes]

    def fetch_nav(self, code, start, end):
        result, expected = [], None
        for page in range(1, 21):
            response = requests.get("https://api.fund.eastmoney.com/f10/lsjz", timeout=20,
                                    params={"fundCode": code.split(".")[1], "pageIndex": page, "pageSize": 200,
                                            "startDate": start, "endDate": end},
                                    headers={"Referer": "https://fundf10.eastmoney.com/", "User-Agent": "Mozilla/5.0"})
            response.raise_for_status()
            payload = response.json()
            count = int(payload["TotalCount"])
            if expected is not None and count != expected:
                raise ValueError("净值分页总数发生变化，请重试")
            expected = count
            records = payload["Data"]["LSJZList"]
            for row in records:
                if not start <= row["FSRQ"] <= end:
                    raise ValueError("净值来源返回范围外数据")
                result.append({"kind": "nav", "code": code, "stamp": row["FSRQ"], "nav": row["DWJZ"]})
            if len(result) == expected:
                return result
            if not records or len(result) > expected:
                break
        raise ValueError("净值分页不完整，未发布此基金净值")
