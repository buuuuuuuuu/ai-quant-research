#!/usr/bin/env python3
"""Explicit, offline-by-default diagnostic; never activates a market-data provider."""
import argparse
import json
import re
import subprocess
import sys
from datetime import date
from decimal import Decimal, InvalidOperation

from ai_quant_assistant.providers.index_fund_close import IndexFundCloseProvider


def compare(primary, candidate):
    """Compare the observed dates, without inferring the expected trading calendar."""
    left, right = dict(primary), dict(candidate)
    if len(left) != len(primary) or len(right) != len(candidate):
        raise ValueError("duplicate trade date")
    common = sorted(left.keys() & right.keys())
    mismatch = [day for day in common if left[day] != right[day]]
    only_primary = sorted(left.keys() - right.keys())
    only_candidate = sorted(right.keys() - left.keys())
    return {
        "primary_rows": len(left), "candidate_rows": len(right), "overlap": len(common),
        "status": "no_overlap" if not common else "different" if mismatch or only_primary or only_candidate else "observed_rows_match",
        "only_primary": only_primary,
        "only_candidate": only_candidate,
        "mismatches": [{"date": day, "primary": str(left[day]), "candidate": str(right[day])} for day in mismatch],
        "note": "仅比较有记录的日期；不证明交易日历完整、来源可靠或分红口径。",
    }


def read_sina(symbol, start, end, timeout):
    # AKShare's Sina implementation has no request timeout. Limit the entire
    # isolated process; a hung provider cannot indefinitely block this diagnostic.
    program = '''import akshare as ak, json, sys\nx=ak.fund_etf_hist_sina(sys.argv[1])\nprint(json.dumps([[str(r.date), str(r.close), str(r.volume)] for r in x.itertuples()]))'''
    result = subprocess.run([sys.executable, "-c", program, symbol], capture_output=True, text=True, timeout=timeout, check=True)
    data = json.loads(result.stdout)
    rows = []
    for day, close, volume in data:
        parsed_day = date.fromisoformat(day)
        if not start <= parsed_day <= end:
            continue
        try:
            price, quantity = Decimal(close), Decimal(volume)
        except InvalidOperation as exc:
            raise ValueError("新浪价格数据格式无效") from exc
        if not price.is_finite() or price <= 0 or not quantity.is_finite() or quantity < 0:
            raise ValueError("新浪价格或成交量无效")
        if quantity > 0:
            rows.append((day, price))
    return rows


def main():
    parser = argparse.ArgumentParser(description="主动联网比较东方财富和候选新浪 ETF 日收盘价；结果不代表来源验收。")
    parser.add_argument("fund", help="SSE.510300 或 SZSE.159919")
    parser.add_argument("start", type=date.fromisoformat)
    parser.add_argument("end", type=date.fromisoformat)
    parser.add_argument("--timeout", type=int, default=40, help="新浪诊断子进程超时秒数，1 至 90")
    args = parser.parse_args()
    if not re.fullmatch(r"(?:SSE|SZSE)\.[0-9]{6}", args.fund) or args.start > args.end or (args.end - args.start).days > 1830 or not 1 <= args.timeout <= 90:
        parser.error("基金代码、日期范围或超时不合法")
    exchange, code = args.fund.split(".")
    symbol = ("sh" if exchange == "SSE" else "sz") + code
    output = {"fund": args.fund, "start": str(args.start), "end": str(args.end)}
    errors = {}
    try:
        primary = [(str(row.trade_date), row.close) for row in IndexFundCloseProvider(timeout=10).fetch(args.fund, args.start, args.end)]
    except Exception as exc:
        primary = None
        errors["eastmoney"] = f"{type(exc).__name__}: {exc}"
    try:
        candidate = read_sina(symbol, args.start, args.end, args.timeout)
    except subprocess.TimeoutExpired:
        candidate = None
        errors["sina"] = "诊断子进程超时"
    except (subprocess.CalledProcessError, ValueError, json.JSONDecodeError) as exc:
        candidate = None
        errors["sina"] = f"{type(exc).__name__}: {exc}"
    output["errors"] = errors
    if primary is not None and candidate is not None:
        output["comparison"] = compare(primary, candidate)
    else:
        output["eastmoney_rows"] = len(primary) if primary is not None else None
        output["sina_rows"] = len(candidate) if candidate is not None else None
    print(json.dumps(output, ensure_ascii=False, indent=2))
    return 1 if errors or output["comparison"]["status"] != "observed_rows_match" else 0


if __name__ == "__main__":
    sys.exit(main())
