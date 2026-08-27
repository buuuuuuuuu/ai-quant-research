from __future__ import annotations

import math
from datetime import date, datetime
from typing import Any

import pandas as pd

from ai_quant_assistant.domain.models import normalize_benchmark_symbol
from ai_quant_assistant.storage.database import LocalDatabase

DATASETS = {
    "prices": "published_daily_bar",
    "benchmarks": "published_benchmark_daily_bar",
    "announcements": "published_announcement",
    "financials": "published_financial_metric",
}

DATE_FIELDS = {
    "announcement_date",
    "delisting_date",
    "end_date",
    "industry_effective_from",
    "listing_date",
    "price_end",
    "price_start",
    "report_period_end",
    "start_date",
    "trade_date",
}


class DataExplorer:
    """Read-only, paginated access to published research data and its lineage."""

    def __init__(self, database: LocalDatabase) -> None:
        self.database = database

    @staticmethod
    def _page(page: int, page_size: int) -> tuple[int, int, int]:
        page = max(1, page)
        page_size = min(200, max(1, page_size))
        return page, page_size, (page - 1) * page_size

    @staticmethod
    def _value(value: Any, field: str | None = None) -> Any:
        if value is None or (isinstance(value, float) and math.isnan(value)):
            return None
        if isinstance(value, (pd.Timestamp, datetime, date)):
            if field in DATE_FIELDS:
                return value.date().isoformat() if isinstance(value, datetime) else value.isoformat()
            return value.isoformat()
        return value

    @classmethod
    def _records(cls, frame: pd.DataFrame) -> list[dict[str, Any]]:
        return [
            {str(key): cls._value(value, str(key)) for key, value in row.items()}
            for row in frame.to_dict(orient="records")
        ]

    @staticmethod
    def _result(
        rows: list[dict[str, Any]], total: int, page: int, page_size: int
    ) -> dict[str, Any]:
        return {
            "items": rows,
            "pagination": {
                "page": page,
                "page_size": page_size,
                "total": total,
                "pages": max(1, math.ceil(total / page_size)),
            },
        }

    def datasets(self) -> dict[str, Any]:
        status = self.database.status()
        if not status.get("initialized"):
            return {"database": status, "datasets": []}
        queries = {
            "prices": ("trade_date", "published_daily_bar"),
            "benchmarks": ("trade_date", "published_benchmark_daily_bar"),
            "announcements": ("announcement_date", "published_announcement"),
            "financials": ("report_period_end", "published_financial_metric"),
        }
        with self.database.connect(read_only=True) as connection:
            items = []
            for key, (date_column, relation) in queries.items():
                start, end, rows, batches = connection.execute(
                    f"""
                    SELECT MIN({date_column}), MAX({date_column}), COUNT(*),
                           COUNT(DISTINCT source_batch_id)
                    FROM {relation}
                    """
                ).fetchone()
                items.append(
                    {
                        "dataset": key,
                        "rows": int(rows),
                        "batches": int(batches),
                        "start_date": self._value(start, "start_date"),
                        "end_date": self._value(end, "end_date"),
                    }
                )
            failed_checks = int(
                connection.execute("SELECT COUNT(*) FROM dq_result WHERE NOT passed").fetchone()[0]
            )
        return {"database": status, "datasets": items, "failed_quality_checks": failed_checks}

    def securities(
        self,
        *,
        query: str = "",
        dataset: str = "all",
        page: int = 1,
        page_size: int = 50,
    ) -> dict[str, Any]:
        if dataset not in {"all", "prices", "announcements", "financials"}:
            raise ValueError("unsupported dataset filter")
        page, page_size, offset = self._page(page, page_size)
        pattern = f"%{query.strip()}%"
        availability_filter = {
            "all": "TRUE",
            "prices": "COALESCE(prices.price_rows, 0) > 0",
            "announcements": "COALESCE(announcements.announcement_count, 0) > 0",
            "financials": "COALESCE(financials.report_count, 0) > 0",
        }[dataset]
        aggregates = """
            WITH prices AS (
                SELECT security_id, MIN(trade_date) AS price_start,
                       MAX(trade_date) AS price_end, COUNT(*) AS price_rows
                FROM published_daily_bar WHERE adjustment = 'qfq' GROUP BY security_id
            ), announcements AS (
                SELECT security_id, COUNT(*) AS announcement_count
                FROM published_announcement GROUP BY security_id
            ), financials AS (
                SELECT security_id, COUNT(*) AS report_count
                FROM published_financial_report GROUP BY security_id
            )
        """
        joins = f"""
            FROM security_master AS security
            LEFT JOIN prices USING (security_id)
            LEFT JOIN announcements USING (security_id)
            LEFT JOIN financials USING (security_id)
            WHERE (security.symbol ILIKE ? OR security.name ILIKE ?)
              AND {availability_filter}
        """
        with self.database.connect(read_only=True) as connection:
            total = int(
                connection.execute(
                    f"{aggregates} SELECT COUNT(*) {joins}", [pattern, pattern]
                ).fetchone()[0]
            )
            frame = connection.execute(
                f"""
                {aggregates}
                SELECT security.security_id, security.symbol, security.name, security.exchange,
                       security.list_date, security.delist_date, security.status,
                       prices.price_start, prices.price_end,
                       COALESCE(prices.price_rows, 0) AS price_rows,
                       COALESCE(announcements.announcement_count, 0) AS announcement_count,
                       COALESCE(financials.report_count, 0) AS report_count
                {joins}
                ORDER BY security.symbol LIMIT ? OFFSET ?
                """,
                [pattern, pattern, page_size, offset],
            ).fetchdf()
        return self._result(self._records(frame), total, page, page_size)

    def security(self, symbol: str) -> dict[str, Any]:
        with self.database.connect(read_only=True) as connection:
            row = connection.execute(
                """
                SELECT security.security_id, security.symbol, security.name, security.exchange,
                       security.list_date, security.delist_date, security.status,
                       industry.industry_name, industry.classification_system,
                       industry.history_mode, industry.effective_from AS industry_effective_from,
                       industry.source AS industry_source,
                       industry.source_batch_id AS industry_batch_id
                FROM security_master AS security
                LEFT JOIN LATERAL (
                    SELECT * FROM security_industry_history AS item
                    WHERE item.security_id = security.security_id
                    ORDER BY item.knowledge_time DESC LIMIT 1
                ) AS industry ON TRUE
                WHERE security.symbol = ?
                """,
                [symbol],
            ).fetchdf()
            if row.empty:
                raise KeyError(symbol)
            coverage = connection.execute(
                """
                WITH coverage AS (
                    SELECT security_id, adjustment, MIN(trade_date) AS start_date,
                           MAX(trade_date) AS end_date, COUNT(*) AS rows
                    FROM published_daily_bar GROUP BY security_id, adjustment
                )
                SELECT coverage.adjustment, coverage.start_date, coverage.end_date,
                       coverage.rows,
                       NULLIF((
                         SELECT COUNT(*) FROM trading_calendar calendar
                         WHERE calendar.exchange = security.exchange AND calendar.is_open
                           AND calendar.trade_date BETWEEN coverage.start_date
                                                       AND coverage.end_date
                       ), 0) - coverage.rows AS missing_trading_days
                FROM coverage JOIN security_master AS security USING (security_id)
                WHERE security.symbol = ?
                ORDER BY coverage.adjustment
                """,
                [symbol],
            ).fetchdf()
            counts = connection.execute(
                """
                SELECT
                  (SELECT COUNT(*) FROM published_announcement a WHERE a.security_id = s.security_id),
                  (SELECT COUNT(*) FROM published_financial_report r WHERE r.security_id = s.security_id),
                  (SELECT COUNT(*) FROM published_financial_metric m WHERE m.security_id = s.security_id),
                  (SELECT COUNT(*) FROM dq_result q WHERE NOT q.passed
                     AND (q.object_key LIKE '%' || s.symbol || '%'
                          OR q.object_key LIKE '%' || s.security_id || '%'))
                FROM security_master s WHERE s.symbol = ?
                """,
                [symbol],
            ).fetchone()
        result = self._records(row)[0]
        result["price_coverage"] = self._records(coverage)
        result["announcement_count"] = int(counts[0])
        result["report_count"] = int(counts[1])
        result["metric_count"] = int(counts[2])
        result["failed_quality_checks"] = int(counts[3])
        result["point_in_time_notice"] = (
            "行业为真实观察快照，不能据此推断首次观察日期之前的历史分类。"
            if result.get("history_mode") == "observed_snapshot"
            else "行业分类按其知识时间参与历史研究。"
        )
        return result

    def prices(
        self,
        symbol: str,
        *,
        start_date: str | None = None,
        end_date: str | None = None,
        adjustment: str = "qfq",
        page: int = 1,
        page_size: int = 50,
        order: str = "desc",
    ) -> dict[str, Any]:
        if adjustment not in {"none", "qfq", "hfq"} or order not in {"asc", "desc"}:
            raise ValueError("unsupported price filter")
        page, page_size, offset = self._page(page, page_size)
        filters = ["security.symbol = ?", "bar.adjustment = ?"]
        params: list[Any] = [symbol, adjustment]
        if start_date:
            filters.append("bar.trade_date >= ?")
            params.append(start_date)
        if end_date:
            filters.append("bar.trade_date <= ?")
            params.append(end_date)
        where = " AND ".join(filters)
        with self.database.connect(read_only=True) as connection:
            total = int(
                connection.execute(
                    f"SELECT COUNT(*) FROM published_daily_bar bar JOIN security_master security USING (security_id) WHERE {where}",
                    params,
                ).fetchone()[0]
            )
            frame = connection.execute(
                f"""
                SELECT security.symbol, security.name, bar.trade_date, bar.adjustment,
                       bar.open, bar.high, bar.low, bar.close, bar.volume, bar.amount,
                       bar.quality_status, bar.published_at,
                       'local:published_daily_bar' AS serving_source,
                       COALESCE(raw.source, 'derived_or_unknown') AS provider_source,
                       bar.source_batch_id,
                       COALESCE(partition.raw_path, '') AS raw_path,
                       security.symbol || ':' || bar.adjustment || ':' ||
                         CAST(bar.trade_date AS VARCHAR) AS record_id
                FROM published_daily_bar bar
                JOIN security_master security USING (security_id)
                LEFT JOIN daily_bar_raw raw
                  ON raw.security_id = bar.security_id AND raw.trade_date = bar.trade_date
                 AND raw.batch_id = bar.source_batch_id
                LEFT JOIN sync_partition partition
                  ON partition.batch_id = bar.source_batch_id
                 AND partition.security_id = bar.security_id
                 AND partition.adjustment = bar.adjustment
                 AND bar.trade_date BETWEEN partition.start_date AND partition.end_date
                WHERE {where}
                QUALIFY ROW_NUMBER() OVER (
                    PARTITION BY bar.security_id, bar.trade_date, bar.adjustment
                    ORDER BY partition.updated_at DESC NULLS LAST
                ) = 1
                ORDER BY bar.trade_date {order.upper()} LIMIT ? OFFSET ?
                """,
                [*params, page_size, offset],
            ).fetchdf()
            summary = connection.execute(
                f"""SELECT MIN(bar.trade_date), MAX(bar.trade_date), COUNT(*),
                           SUM(CASE WHEN bar.quality_status = 'warning' THEN 1 ELSE 0 END)
                    FROM published_daily_bar bar JOIN security_master security USING (security_id)
                    WHERE {where}""",
                params,
            ).fetchone()
        result = self._result(self._records(frame), total, page, page_size)
        result["summary"] = {
            "start_date": self._value(summary[0], "start_date"),
            "end_date": self._value(summary[1], "end_date"),
            "rows": int(summary[2]),
            "warning_rows": int(summary[3] or 0),
            "adjustment": adjustment,
        }
        return result

    def announcements(
        self,
        symbol: str,
        *,
        start_date: str | None = None,
        end_date: str | None = None,
        report_type: str | None = None,
        page: int = 1,
        page_size: int = 50,
    ) -> dict[str, Any]:
        if report_type and report_type not in {"q1", "interim", "q3", "annual"}:
            raise ValueError("unsupported report type")
        page, page_size, offset = self._page(page, page_size)
        filters = ["security.symbol = ?"]
        params: list[Any] = [symbol]
        for value, expression in (
            (start_date, "item.announcement_date >= ?"),
            (end_date, "item.announcement_date <= ?"),
            (report_type, "item.report_type = ?"),
        ):
            if value:
                filters.append(expression)
                params.append(value)
        where = " AND ".join(filters)
        base = f"FROM published_announcement item JOIN security_master security USING (security_id) WHERE {where}"
        with self.database.connect(read_only=True) as connection:
            total = int(connection.execute(f"SELECT COUNT(*) {base}", params).fetchone()[0])
            frame = connection.execute(
                f"""
                SELECT item.announcement_id AS record_id, security.symbol, security.name,
                       item.title, item.report_type, item.report_year, item.report_period_end,
                       item.announcement_date, item.published_at, item.url,
                       item.quality_status, 'local:published_announcement' AS serving_source,
                       item.source AS provider_source, item.source_batch_id,
                       COALESCE(partition.raw_path, '') AS raw_path
                FROM published_announcement item JOIN security_master security USING (security_id)
                LEFT JOIN research_sync_partition partition
                  ON partition.batch_id = item.source_batch_id
                 AND partition.dataset = 'announcement'
                 AND partition.object_id = item.security_id
                 AND partition.variant = item.report_type
                 AND item.announcement_date BETWEEN partition.start_date AND partition.end_date
                WHERE {where}
                QUALIFY ROW_NUMBER() OVER (PARTITION BY item.announcement_id ORDER BY partition.updated_at DESC NULLS LAST) = 1
                ORDER BY item.published_at DESC LIMIT ? OFFSET ?
                """,
                [*params, page_size, offset],
            ).fetchdf()
        result = self._result(self._records(frame), total, page, page_size)
        result["point_in_time_notice"] = (
            "published_at 是研究可用时点；report_period_end 仅代表报告期，不能作为当时已知日期。"
        )
        return result

    def financial_reports(
        self,
        symbol: str,
        *,
        start_date: str | None = None,
        end_date: str | None = None,
        report_type: str | None = None,
        page: int = 1,
        page_size: int = 50,
    ) -> dict[str, Any]:
        if report_type and report_type not in {"q1", "interim", "q3", "annual"}:
            raise ValueError("unsupported report type")
        page, page_size, offset = self._page(page, page_size)
        filters = ["security.symbol = ?"]
        params: list[Any] = [symbol]
        for value, expression in (
            (start_date, "report.report_period_end >= ?"),
            (end_date, "report.report_period_end <= ?"),
            (report_type, "report.report_type = ?"),
        ):
            if value:
                filters.append(expression)
                params.append(value)
        where = " AND ".join(filters)
        with self.database.connect(read_only=True) as connection:
            total = int(
                connection.execute(
                    f"SELECT COUNT(*) FROM published_financial_report report JOIN security_master security USING (security_id) WHERE {where}",
                    params,
                ).fetchone()[0]
            )
            frame = connection.execute(
                f"""
                SELECT report.report_id AS record_id, security.symbol, security.name,
                       report.report_period_end, report.report_type, report.published_at,
                       report.availability_source, report.quality_status,
                       COUNT(metric.metric_code) AS metric_count,
                       'local:published_financial_report' AS serving_source,
                       report.source AS provider_source, report.source_batch_id,
                       COALESCE(partition.raw_path, '') AS raw_path
                FROM published_financial_report report
                JOIN security_master security USING (security_id)
                LEFT JOIN published_financial_metric metric ON metric.report_id = report.report_id
                LEFT JOIN financial_sync_partition partition
                  ON partition.batch_id = report.source_batch_id
                 AND partition.security_id = report.security_id
                 AND report.report_period_end BETWEEN partition.start_date AND partition.end_date
                WHERE {where}
                GROUP BY ALL ORDER BY report.published_at DESC LIMIT ? OFFSET ?
                """,
                [*params, page_size, offset],
            ).fetchdf()
        result = self._result(self._records(frame), total, page, page_size)
        result["point_in_time_notice"] = (
            "同一报告期若存在修订，历史研究仅使用信号时点之前已公开的最新版本。"
        )
        return result

    def financial_metrics(
        self,
        symbol: str,
        *,
        report_id: str | None = None,
        metric_code: str | None = None,
        start_date: str | None = None,
        end_date: str | None = None,
        page: int = 1,
        page_size: int = 50,
    ) -> dict[str, Any]:
        page, page_size, offset = self._page(page, page_size)
        filters = ["security.symbol = ?"]
        params: list[Any] = [symbol]
        for value, expression in (
            (report_id, "report.report_id = ?"),
            (metric_code, "metric.metric_code = ?"),
            (start_date, "report.report_period_end >= ?"),
            (end_date, "report.report_period_end <= ?"),
        ):
            if value:
                filters.append(expression)
                params.append(value)
        where = " AND ".join(filters)
        base = f"FROM published_financial_metric metric JOIN published_financial_report report USING (report_id) JOIN security_master security ON security.security_id = report.security_id WHERE {where}"
        with self.database.connect(read_only=True) as connection:
            total = int(connection.execute(f"SELECT COUNT(*) {base}", params).fetchone()[0])
            frame = connection.execute(
                f"""
                SELECT report.report_id || ':' || metric.metric_code AS record_id,
                       security.symbol, security.name, report.report_period_end,
                       report.report_type, report.published_at, report.availability_source,
                       metric.metric_code, metric.metric_value, metric.unit,
                       metric.quality_status,
                       'local:published_financial_metric' AS serving_source,
                       report.source AS provider_source, report.source_batch_id,
                       COALESCE(partition.raw_path, '') AS raw_path
                FROM published_financial_metric metric
                JOIN published_financial_report report USING (report_id)
                JOIN security_master security ON security.security_id = report.security_id
                LEFT JOIN financial_sync_partition partition
                  ON partition.batch_id = report.source_batch_id
                 AND partition.security_id = report.security_id
                 AND report.report_period_end BETWEEN partition.start_date AND partition.end_date
                WHERE {where}
                QUALIFY ROW_NUMBER() OVER (
                    PARTITION BY report.report_id, metric.metric_code
                    ORDER BY partition.updated_at DESC NULLS LAST
                ) = 1
                ORDER BY report.published_at DESC, metric.metric_code LIMIT ? OFFSET ?
                """,
                [*params, page_size, offset],
            ).fetchdf()
            codes = [
                str(row[0])
                for row in connection.execute(
                    f"SELECT DISTINCT metric.metric_code {base} ORDER BY metric.metric_code", params
                ).fetchall()
            ]
        result = self._result(self._records(frame), total, page, page_size)
        result["metric_codes"] = codes
        result["point_in_time_notice"] = (
            "指标仅从财报 published_at 起可用于研究；缺失值不会被静默填充。"
        )
        return result

    def benchmarks(self, *, page: int = 1, page_size: int = 50) -> dict[str, Any]:
        page, page_size, offset = self._page(page, page_size)
        with self.database.connect(read_only=True) as connection:
            total = int(connection.execute("SELECT COUNT(*) FROM benchmark_master").fetchone()[0])
            frame = connection.execute(
                """
                SELECT benchmark.symbol, benchmark.name, MIN(bar.trade_date) AS start_date,
                       MAX(bar.trade_date) AS end_date, COUNT(bar.trade_date) AS price_rows,
                       COUNT(DISTINCT bar.source_batch_id) AS batches,
                       benchmark.source AS provider_source, benchmark.source_batch_id
                FROM benchmark_master benchmark
                LEFT JOIN published_benchmark_daily_bar bar USING (benchmark_id)
                GROUP BY benchmark.symbol, benchmark.name, benchmark.source, benchmark.source_batch_id
                ORDER BY benchmark.symbol LIMIT ? OFFSET ?
                """,
                [page_size, offset],
            ).fetchdf()
        return self._result(self._records(frame), total, page, page_size)

    def benchmark_prices(
        self,
        symbol: str,
        *,
        start_date: str | None = None,
        end_date: str | None = None,
        page: int = 1,
        page_size: int = 50,
        order: str = "desc",
    ) -> dict[str, Any]:
        if order not in {"asc", "desc"}:
            raise ValueError("unsupported order")
        symbol = normalize_benchmark_symbol(symbol)
        page, page_size, offset = self._page(page, page_size)
        filters = ["benchmark.symbol = ?"]
        params: list[Any] = [symbol]
        if start_date:
            filters.append("bar.trade_date >= ?")
            params.append(start_date)
        if end_date:
            filters.append("bar.trade_date <= ?")
            params.append(end_date)
        where = " AND ".join(filters)
        with self.database.connect(read_only=True) as connection:
            total = int(
                connection.execute(
                    f"SELECT COUNT(*) FROM published_benchmark_daily_bar bar JOIN benchmark_master benchmark USING (benchmark_id) WHERE {where}",
                    params,
                ).fetchone()[0]
            )
            frame = connection.execute(
                f"""
                SELECT benchmark.symbol, benchmark.name, bar.trade_date, bar.close,
                       bar.quality_status, bar.published_at,
                       'local:published_benchmark_daily_bar' AS serving_source,
                       bar.source AS provider_source, bar.source_batch_id,
                       COALESCE(partition.raw_path, '') AS raw_path,
                       benchmark.symbol || ':' || CAST(bar.trade_date AS VARCHAR) AS record_id
                FROM published_benchmark_daily_bar bar JOIN benchmark_master benchmark USING (benchmark_id)
                LEFT JOIN research_sync_partition partition
                  ON partition.batch_id = bar.source_batch_id
                 AND partition.dataset = 'benchmark_daily_bar'
                 AND partition.object_id = bar.benchmark_id
                 AND bar.trade_date BETWEEN partition.start_date AND partition.end_date
                WHERE {where}
                QUALIFY ROW_NUMBER() OVER (PARTITION BY bar.benchmark_id, bar.trade_date ORDER BY partition.updated_at DESC NULLS LAST) = 1
                ORDER BY bar.trade_date {order.upper()} LIMIT ? OFFSET ?
                """,
                [*params, page_size, offset],
            ).fetchdf()
        return self._result(self._records(frame), total, page, page_size)

    def lineage(self, dataset: str, record_id: str) -> dict[str, Any]:
        if dataset == "prices":
            try:
                symbol, adjustment, trade_date = record_id.split(":", 2)
            except ValueError as exc:
                raise KeyError(record_id) from exc
            result = self.prices(
                symbol,
                start_date=trade_date,
                end_date=trade_date,
                adjustment=adjustment,
                page_size=1,
            )
        elif dataset == "benchmarks":
            try:
                symbol, trade_date = record_id.split(":", 1)
            except ValueError as exc:
                raise KeyError(record_id) from exc
            result = self.benchmark_prices(
                symbol, start_date=trade_date, end_date=trade_date, page_size=1
            )
        elif dataset == "announcements":
            with self.database.connect(read_only=True) as connection:
                symbol_row = connection.execute(
                    "SELECT security.symbol FROM published_announcement item JOIN security_master security USING (security_id) WHERE item.announcement_id = ?",
                    [record_id],
                ).fetchone()
            if not symbol_row:
                raise KeyError(record_id)
            result = self.announcements(str(symbol_row[0]), page_size=200)
            result["items"] = [item for item in result["items"] if item["record_id"] == record_id]
        elif dataset == "financials":
            report_id, separator, metric_code = record_id.rpartition(":")
            with self.database.connect(read_only=True) as connection:
                symbol_row = connection.execute(
                    "SELECT security.symbol FROM published_financial_report report JOIN security_master security USING (security_id) WHERE report.report_id = ?",
                    [report_id if separator else record_id],
                ).fetchone()
            if not symbol_row:
                raise KeyError(record_id)
            if separator:
                result = self.financial_metrics(
                    str(symbol_row[0]), report_id=report_id, metric_code=metric_code, page_size=1
                )
            else:
                result = self.financial_reports(str(symbol_row[0]), page_size=200)
                result["items"] = [
                    item for item in result["items"] if item["record_id"] == record_id
                ]
        else:
            raise ValueError("unsupported lineage dataset")
        if not result["items"]:
            raise KeyError(record_id)
        item = result["items"][0]
        return {
            "dataset": dataset,
            "record_id": record_id,
            "lineage": {
                key: item.get(key)
                for key in (
                    "serving_source",
                    "provider_source",
                    "source_batch_id",
                    "raw_path",
                    "published_at",
                    "quality_status",
                )
            },
        }
