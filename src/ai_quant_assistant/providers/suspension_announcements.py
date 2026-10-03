"""Bounded official-announcement evidence for historical A-share suspensions."""

from __future__ import annotations

import hashlib
import re
import tempfile
from collections.abc import Callable
from datetime import UTC, date, datetime, timedelta
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

from curl_cffi import requests

from ai_quant_assistant.documents.service import (
    DOCUMENT_USER_AGENT,
    DocumentIntelligenceService,
    _canonicalize_document_url,
    _validate_remote_url,
)
from ai_quant_assistant.errors import ProviderError
from ai_quant_assistant.providers.cninfo import CninfoAnnouncements

PARSER_VERSION = "m12e4-v1"
SOURCE = "cninfo:official_announcement_chain"
MAX_CANDIDATES = 24
MAX_PDF_BYTES = 12 * 1024 * 1024
MAX_PAGES = 60
MAX_TEXT_CHARS = 300_000
OFFICIAL_PDF_HOSTS = {"static.cninfo.com.cn"}
DATE_PATTERN = r"(20\d{2})\s*年\s*(\d{1,2})\s*月\s*(\d{1,2})\s*日"
CODE_PATTERN = r"(?:股票|证券)代码\s*[:：]?\s*{symbol}(?!\d)"
START_PATTERNS = (
    rf"(?:自|于|已申请于)\s*{DATE_PATTERN}[^。；\n]{{0,80}}?(?:开始)?停牌",
    rf"股票[^。；\n]{{0,50}}?自\s*{DATE_PATTERN}[^。；\n]{{0,60}}?停牌",
)
RESUME_PATTERNS = (
    rf"(?:自|于|将于)\s*{DATE_PATTERN}[^。；\n]{{0,80}}?(?:复牌(?:交易)?|恢复交易)",
    rf"股票[^。；\n]{{0,50}}?自\s*{DATE_PATTERN}[^。；\n]{{0,60}}?(?:复牌|恢复交易)",
)


def _parsed_dates(text: str, patterns: tuple[str, ...]) -> set[date]:
    result: set[date] = set()
    for pattern in patterns:
        for match in re.finditer(pattern, text):
            try:
                result.add(date(*(int(value) for value in match.groups()[-3:])))
            except ValueError:
                continue
    return result


def parse_official_suspension_document(symbol: str, text: str) -> dict[str, Any] | None:
    """Extract explicit boundaries only; title matches and inferred dates are rejected."""
    normalized = re.sub(r"[\t\r ]+", " ", str(text)).strip()
    if not re.search(CODE_PATTERN.format(symbol=re.escape(symbol)), normalized):
        return None
    starts = _parsed_dates(normalized, START_PATTERNS)
    resumes = _parsed_dates(normalized, RESUME_PATTERNS)
    if not starts and not resumes:
        return None
    return {
        "suspension_starts": sorted(starts),
        "resume_dates": sorted(resumes),
    }


def _closed_intervals(starts: list[date], resumes: list[date]) -> set[tuple[date, date]]:
    """Build non-conflicting start-inclusive, resume-exclusive intervals."""
    intervals: set[tuple[date, date]] = set()
    pending: list[date] = []
    start_set, resume_set = set(starts), set(resumes)
    for day in sorted(start_set | resume_set):
        if day in start_set:
            pending.append(day)
        if day in resume_set:
            if len(pending) == 1 and pending[0] < day:
                intervals.add((pending[0], day))
            # Multiple distinct start boundaries before one resume boundary
            # conflict.  Discard the whole open chain rather than choosing one.
            pending = []
    return intervals


class OfficialSuspensionAnnouncements:
    """Resolve unverified gaps from exact official announcement bodies."""

    def __init__(
        self,
        client: CninfoAnnouncements,
        *,
        timeout_seconds: float,
        fetch_pdf: Callable[[str], bytes] | None = None,
        extract_pdf: Callable[[bytes], str] | None = None,
    ) -> None:
        self.client = client
        self.timeout = max(1.0, min(float(timeout_seconds), 60.0))
        self._fetch_pdf = fetch_pdf or self._download_pdf
        self._extract_pdf = extract_pdf or self._pdf_text

    def _download_pdf(self, source_url: str) -> bytes:
        url = _canonicalize_document_url(source_url)
        _validate_remote_url(url)
        if (urlsplit(url).hostname or "").lower() not in OFFICIAL_PDF_HOSTS:
            raise ProviderError("官方停复牌证据只允许读取巨潮静态公告PDF")
        response = None
        try:
            with requests.Session(
                impersonate="chrome",
                headers={
                    "Accept": "application/pdf,application/octet-stream;q=0.9",
                    "Referer": "https://www.cninfo.com.cn/",
                    "User-Agent": DOCUMENT_USER_AGENT,
                },
            ) as session:
                response = session.request(
                    "GET",
                    url,
                    timeout=(min(10.0, self.timeout), self.timeout),
                    allow_redirects=False,
                    stream=True,
                )
                if response.status_code != 200:
                    raise ProviderError(
                        f"官方停复牌公告读取失败，HTTP {response.status_code}"
                    )
                media_type = str(response.headers.get("Content-Type", "")).split(";", 1)[0]
                if media_type not in {"application/pdf", "application/octet-stream"}:
                    raise ProviderError("官方停复牌公告返回的不是PDF")
                declared = response.headers.get("Content-Length")
                if declared and int(declared) > MAX_PDF_BYTES:
                    raise ProviderError("官方停复牌公告超过安全大小限制")
                payload = bytearray()
                for chunk in response.iter_content(64 * 1024):
                    payload.extend(chunk)
                    if len(payload) > MAX_PDF_BYTES:
                        raise ProviderError("官方停复牌公告超过安全大小限制")
        except requests.exceptions.RequestException as exc:
            raise ProviderError("官方停复牌公告连接失败或超时") from exc
        finally:
            if response is not None:
                response.close()
        result = bytes(payload)
        if b"%PDF-" not in result[:1_024]:
            raise ProviderError("官方停复牌公告内容不是有效PDF")
        return result

    @staticmethod
    def _pdf_text(payload: bytes) -> str:
        if b"%PDF-" not in payload[:1_024]:
            raise ProviderError("官方停复牌公告内容不是有效PDF")
        temporary: Path | None = None
        try:
            with tempfile.NamedTemporaryFile(suffix=".pdf", delete=False) as handle:
                handle.write(payload)
                temporary = Path(handle.name)
            pages = DocumentIntelligenceService._native_pages(temporary)
        except Exception as exc:  # noqa: BLE001 - bounded parser boundary
            raise ProviderError("官方停复牌公告正文无法安全解析") from exc
        finally:
            if temporary is not None:
                temporary.unlink(missing_ok=True)
        if len(pages) > MAX_PAGES:
            raise ProviderError("官方停复牌公告页数超过安全限制")
        text = "\n".join(pages).strip()
        if not text:
            raise ProviderError("官方停复牌公告没有可解析正文")
        if len(text) > MAX_TEXT_CHARS:
            raise ProviderError("官方停复牌公告正文超过安全限制")
        return text

    def verify(self, symbol: str, dates: tuple[date, ...]) -> dict[str, Any]:
        requested = tuple(sorted(set(dates)))
        if not requested:
            return {"verified": [], "errors": [], "evidence_chains": [], "source": SOURCE}
        query_start = min(requested) - timedelta(days=30)
        query_end = max(requested) + timedelta(days=60)
        frame = self.client.fetch_keywords(
            symbol=symbol,
            start_date=query_start.strftime("%Y%m%d"),
            end_date=query_end.strftime("%Y%m%d"),
        )
        if len(frame) > MAX_CANDIDATES:
            raise ProviderError("官方停复牌候选公告超过安全数量限制")

        documents: list[dict[str, Any]] = []
        failures: list[str] = []
        for row in frame.to_dict("records"):
            try:
                attachment_url = str(row.get("附件链接") or "")
                if not attachment_url:
                    raise ProviderError("官方公告元数据未提供可核验正文附件")
                payload = self._fetch_pdf(attachment_url)
                digest = hashlib.sha256(payload).hexdigest()
                parsed = parse_official_suspension_document(
                    symbol, self._extract_pdf(payload)
                )
                if parsed is None:
                    continue
                documents.append(
                    {
                        "announcement_id": str(row["公告ID"]),
                        "title": re.sub(r"<[^>]+>", "", str(row["公告标题"])),
                        "published_at": str(row["公告时间"]),
                        "source_url": str(row["公告链接"]),
                        "attachment_url": attachment_url,
                        "content_sha256": digest,
                        **parsed,
                    }
                )
            except Exception as exc:  # noqa: BLE001 - independent official document boundary
                failures.append(f"{row.get('公告ID', 'unknown')}: {type(exc).__name__}: {exc}"[:500])

        starts = sorted(
            {day for document in documents for day in document["suspension_starts"]}
        )
        resumes = sorted({day for document in documents for day in document["resume_dates"]})
        intervals = _closed_intervals(starts, resumes)
        checked_at = datetime.now(UTC).isoformat()
        verified: list[dict[str, Any]] = []
        chains: dict[tuple[date, date], dict[str, Any]] = {}
        for day in requested:
            covering = sorted(interval for interval in intervals if interval[0] <= day < interval[1])
            if len(covering) != 1:
                continue
            start, resume = covering[0]
            sources = [
                document
                for document in documents
                if start in document["suspension_starts"]
                or resume in document["resume_dates"]
            ]
            chain = chains.setdefault(
                (start, resume),
                {
                    "source_type": "official_announcement_chain",
                    "source": SOURCE,
                    "symbol": symbol,
                    "suspension_start": str(start),
                    "resume_date": str(resume),
                    "announcement_ids": [item["announcement_id"] for item in sources],
                    "announcement_titles": [item["title"] for item in sources],
                    "source_urls": [item["source_url"] for item in sources],
                    "attachment_urls": [item["attachment_url"] for item in sources],
                    "content_sha256": [item["content_sha256"] for item in sources],
                    "parser_version": PARSER_VERSION,
                    "verified_at": checked_at,
                    "covered_dates": [],
                },
            )
            chain["covered_dates"].append(str(day))
            verified.append(
                {
                    "date": str(day),
                    "classification": "verified_suspension",
                    "source": SOURCE,
                    "checked_at": checked_at,
                    "reason": f"官方公告明确记录{start}停牌、{resume}复牌",
                    "evidence": chain,
                }
            )
        if not len(frame):
            diagnostic = "官方公告查询未找到停牌或复牌候选记录"
        elif not documents:
            diagnostic = "候选公告正文未包含目标股票的明确停牌或复牌日期边界"
        elif starts and not resumes:
            diagnostic = "官方公告仅识别到开始停牌边界，尚未识别到明确复牌日期"
        elif resumes and not starts:
            diagnostic = "官方公告仅识别到复牌边界，尚未识别到明确开始停牌日期"
        elif starts and resumes and not intervals:
            diagnostic = "官方公告中的停牌边界相互冲突，系统未自动选择"
        else:
            diagnostic = "官方公告形成的闭合停牌区间未覆盖该日期"
        unresolved_reason = "；".join([*failures, diagnostic])[:500]
        errors = [
            {"date": str(day), "error": unresolved_reason}
            for day in requested
            if str(day) not in {item["date"] for item in verified}
        ]
        return {
            "verified": verified,
            "errors": errors,
            "evidence_chains": list(chains.values()),
            "source": SOURCE,
            "candidate_count": len(frame),
            "parsed_count": len(documents),
            "chain_diagnostic": diagnostic,
            "query_start": str(query_start),
            "query_end": str(query_end),
        }
