"""Immutable, explicitly supplied evidence claims; no source verification or ranking."""
import hashlib
import re
from datetime import date
from urllib.parse import urlsplit

from ai_quant_assistant.storage.index_fund_snapshots import _canonical


def normalize_claim(claim):
    fund = claim.get("fund_id", "")
    kind = claim.get("kind")
    if not re.fullmatch(r"(?:SSE|SZSE)\.[0-9]{6}", fund):
        raise ValueError("基金代码格式应为 SSE.510300 或 SZSE.159919")
    if kind not in {"listing", "delisting", "tracking"}:
        raise ValueError("不支持的证据类型")
    start, end = claim.get("effective_from"), claim.get("effective_to")
    if not isinstance(start, date) or (end is not None and not isinstance(end, date)):
        raise ValueError("请填写有效日期")
    if end is not None and end < start:
        raise ValueError("结束日期不能早于开始日期")
    index = claim.get("index_code")
    if kind == "tracking":
        if end is None or not isinstance(index, str) or not re.fullmatch(r"[0-9]{6}", index):
            raise ValueError("跟踪关系必须填写指数代码和证据支持的明确结束日期")
    elif end is not None or index is not None:
        raise ValueError("上市或退市证据不应填写结束日期或指数代码")
    title, source = claim.get("source_title", "").strip(), claim.get("source_url", "").strip()
    try:
        url = urlsplit(source)
        valid_url = url.scheme == "https" and bool(url.hostname) and not url.username and not url.password and url.port in (None, 443)
    except ValueError:
        valid_url = False
    if not title or not valid_url:
        raise ValueError("请填写来源标题和有效的 HTTPS 来源链接")
    digest = claim.get("source_sha256")
    if digest is not None and not re.fullmatch(r"[0-9a-f]{64}", digest):
        raise ValueError("来源摘要须为小写 SHA-256")
    published = claim.get("published_date")
    if published is not None and not isinstance(published, date):
        raise ValueError("公告日期无效")
    return {"fund_id": fund, "kind": kind, "effective_from": start.isoformat(),
            "effective_to": end.isoformat() if end else None, "index_code": index,
            "source_title": title, "source_url": source,
            "published_date": published.isoformat() if published else None,
            "source_sha256": digest, "note": (claim.get("note") or "").strip(),
            "verification": "unverified"}


class IndexFundEvidenceStore:
    def __init__(self, database):
        self.database = database

    def save(self, claim):
        normalized = normalize_claim(claim)
        encoded = _canonical(normalized)
        evidence_id = "fund-evidence-" + hashlib.sha256(encoded.encode()).hexdigest()
        self.database.initialize()
        with self.database.connect() as connection:
            connection.execute("INSERT INTO index_fund_evidence(evidence_id,fund_id,claim_json) VALUES (?,?,?) ON CONFLICT DO NOTHING", [evidence_id, normalized["fund_id"], encoded])
        return evidence_id

    def list_for_fund(self, fund_id):
        import json

        if not re.fullmatch(r"(?:SSE|SZSE)\.[0-9]{6}", fund_id):
            raise ValueError("基金代码格式应为 SSE.510300 或 SZSE.159919")
        if not self.database.path.is_file():
            return {"records": [], "truncated": False}
        self.database.initialize()
        with self.database.connect(read_only=True) as connection:
            rows = connection.execute("SELECT evidence_id,claim_json,created_at FROM index_fund_evidence WHERE fund_id=? ORDER BY created_at,evidence_id LIMIT 501", [fund_id]).fetchall()
        return {"records": [{"evidence_id": row[0], "claim": json.loads(row[1]), "created_at": row[2].isoformat()} for row in rows[:500]], "truncated": len(rows) > 500}
