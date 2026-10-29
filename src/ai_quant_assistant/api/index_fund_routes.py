import csv
import html
import io
import re
from datetime import date
from decimal import Decimal

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import Response
from pydantic import BaseModel, ConfigDict, Field, StrictBool

from ai_quant_assistant.storage.index_fund_snapshots import IndexFundSnapshotStore

INDEXES = {
    "000300": "沪深300", "000016": "上证50", "000905": "中证500",
    "000852": "中证1000", "932000": "中证2000", "000510": "中证A500",
    "399006": "创业板指", "000688": "科创50",
}


class SyncRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    index_code: str
    start: date
    end: date
    funds: list[str] = Field(min_length=1, max_length=10)
    confirm: StrictBool


def index_fund_router(database, templates, asset_version, actor, manager):
    router = APIRouter(tags=["index-fund-observation"])
    store = IndexFundSnapshotStore(database)

    @router.get("/market/index-funds", include_in_schema=False)
    def page(request: Request):
        return templates.TemplateResponse(request=request, name="index-funds.html", context={"asset_version": asset_version, "username": actor})

    @router.get("/api/v1/index-funds/{index_code}")
    def latest(index_code: str):
        if index_code not in INDEXES:
            raise HTTPException(404, "不支持的指数")
        if not database.path.is_file():
            return {"index_code": index_code, "index_name": INDEXES[index_code], "snapshot": None}
        return {"index_code": index_code, "index_name": INDEXES[index_code], "snapshot": store.latest(index_code)}

    @router.get("/api/v1/index-fund-snapshots/{snapshot_id}/csv")
    def export_csv(snapshot_id: str):
        if not re.fullmatch(r"index-fund-[0-9a-f]{64}", snapshot_id):
            raise HTTPException(404, "快照不存在")
        try:
            snapshot = store.get(snapshot_id)
        except KeyError as exc:
            raise HTTPException(404, "快照不存在") from exc
        output = io.StringIO(newline="")
        writer = csv.writer(output)
        writer.writerow(["snapshot_id", "index_code", "fund_ids", "scope", "sources", "trade_date", "numerator_cny", "denominator_cny", "ratio_fraction", "ratio_percent", "availability", "missing"])
        spec, inputs = snapshot["spec"], snapshot["inputs"]
        def safe(value):
            text = str(value if value is not None else "")
            return "'" + text if text.lstrip().startswith(("=", "+", "-", "@")) else text
        for point in snapshot["result"].get("points", []):
            ratio = point.get("ratio_fraction")
            percent = str(Decimal(ratio) * 100) if ratio is not None else ""
            values = [snapshot_id, snapshot["index_code"], ";".join(spec.get("funds", [])), "fixed_group_unverified", ";".join(inputs.get("sources", [])), point.get("trade_date"), point.get("numerator_cny"), point.get("denominator_cny"), ratio, percent, point.get("availability"), ";".join(point.get("missing", []))]
            writer.writerow([safe(value) for value in values])
        return Response("\ufeff" + output.getvalue(), media_type="text/csv; charset=utf-8", headers={"Content-Disposition": f'attachment; filename="{snapshot_id}.csv"', "X-Content-Type-Options": "nosniff"})

    @router.get("/api/v1/index-fund-snapshots/{snapshot_id}/html")
    def export_html(snapshot_id: str):
        if not re.fullmatch(r"index-fund-[0-9a-f]{64}", snapshot_id):
            raise HTTPException(404, "快照不存在")
        try:
            snapshot = store.get(snapshot_id)
        except KeyError as exc:
            raise HTTPException(404, "快照不存在") from exc
        def esc(value):
            return html.escape(str(value if value is not None else "—"), quote=True)
        spec, inputs, result = snapshot["spec"], snapshot["inputs"], snapshot["result"]
        rows = []
        for point in result.get("points", []):
            ratio = point.get("ratio_fraction")
            percent = str(Decimal(ratio) * 100) + "%" if ratio is not None else "—"
            cells = [point.get("trade_date"), point.get("numerator_cny"), point.get("denominator_cny"), percent, point.get("availability"), "; ".join(point.get("missing", []))]
            rows.append("<tr>" + "".join(f"<td>{esc(v)}</td>" for v in cells) + "</tr>")
        shares = {(row["date"], row["fund_id"]): row["shares"] for row in inputs.get("shares", [])}
        nav = {(row["date"], row["fund_id"]): row["unit_nav"] for row in inputs.get("nav", [])}
        details = []
        for point in result.get("points", []):
            day = point.get("trade_date")
            for fund in spec.get("funds", []):
                share, value = shares.get((day, fund)), nav.get((day, fund))
                amount = Decimal(share) * Decimal(value) if share is not None and value is not None else None
                denominator = point.get("denominator_cny")
                ratio = amount / Decimal(denominator) * 100 if amount is not None and denominator is not None and Decimal(denominator) > 0 else None
                cells = [day, fund, share, value, amount, f"{ratio}%" if ratio is not None else None]
                details.append("<tr>" + "".join(f"<td>{esc(v)}</td>" for v in cells) + "</tr>")
        sources = "; ".join(inputs.get("sources", [])) or "未记录"
        fund_ids = "; ".join(spec.get("funds", [])) or "未记录"
        warning = result.get("warning") or "未记录"
        document = f"""<!doctype html><html lang="zh-CN"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1"><title>指数基金规模观察快照</title>
<style>body{{font-family:sans-serif;margin:2rem;color:#183326}}table{{border-collapse:collapse;width:100%}}th,td{{border:1px solid #bbb;padding:.45rem;text-align:left}}.scroll{{overflow-x:auto}}p{{overflow-wrap:anywhere}}</style></head><body>
<h1>指数基金规模观察快照</h1><p>快照：{esc(snapshot_id)}</p><p>指数：{esc(snapshot['index_code'])}；范围：{esc(spec.get('start'))} 至 {esc(spec.get('end'))}</p>
<p>快照创建时间：{esc(snapshot.get('created_at'))}；计算版本：未记录；公开时间：未验证</p>
<p>基金组：{esc(fund_ids)}；来源：{esc(sources)}</p><p>{esc(warning)}</p>
<p>比例＝所选基金当日份额×当日单位净值之和÷指数当日流通市值。缺失项不填补；比例不是实际持股比例或资金净流入。公开时间未验证，不用于回测。</p>
<div class="scroll"><table><thead><tr><th>交易日</th><th>基金规模（元）</th><th>流通市值（元）</th><th>占比</th><th>状态</th><th>缺失</th></tr></thead><tbody>{''.join(rows)}</tbody></table></div>
<h2>基金每日明细</h2><div class="scroll"><table><thead><tr><th>交易日</th><th>基金代码</th><th>份额（份）</th><th>单位净值（元）</th><th>规模（元）</th><th>单只占比</th></tr></thead><tbody>{''.join(details)}</tbody></table></div>
</body></html>"""
        return Response(document, media_type="text/html; charset=utf-8", headers={"Content-Disposition": f'attachment; filename="{snapshot_id}.html"', "Content-Security-Policy": "default-src 'none'; style-src 'unsafe-inline'; sandbox", "X-Content-Type-Options": "nosniff"})

    @router.post("/api/v1/index-funds/sync", status_code=202)
    def sync(body: SyncRequest):
        try:
            return manager.submit(**body.model_dump())
        except (ValueError, TypeError) as exc:
            raise HTTPException(422, str(exc)) from exc

    @router.get("/api/v1/index-funds/tasks/{task_id}")
    def task(task_id: str):
        try:
            item = manager.service.tasks.get(task_id)
            if item["spec"].get("dataset") != manager.service.dataset:
                raise KeyError(task_id)
            return item
        except KeyError as exc:
            raise HTTPException(404, "任务不存在") from exc

    return router
