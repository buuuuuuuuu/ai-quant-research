from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pandas as pd
from jinja2 import Environment, FileSystemLoader, select_autoescape
from openpyxl.styles import Alignment, Font, PatternFill

from ai_quant_assistant.domain.models import EventStudyResult


class ReportWriter:
    def __init__(self) -> None:
        template_dir = Path(__file__).parent / "templates"
        self.environment = Environment(
            loader=FileSystemLoader(template_dir),
            autoescape=select_autoescape(["html", "xml"]),
        )
        self.environment.filters["pct"] = self._pct
        self.environment.filters["date"] = self._date

    @staticmethod
    def _pct(value: Any) -> str:
        if value is None or pd.isna(value):
            return "-"
        return f"{float(value) * 100:.2f}%"

    @staticmethod
    def _date(value: Any) -> str:
        if value is None or pd.isna(value):
            return "-"
        return pd.Timestamp(value).date().isoformat()

    @staticmethod
    def _records(frame: pd.DataFrame) -> list[dict[str, Any]]:
        records: list[dict[str, Any]] = []
        for row in frame.to_dict(orient="records"):
            records.append(
                {
                    key: (value.isoformat() if isinstance(value, pd.Timestamp) else value)
                    for key, value in row.items()
                }
            )
        return records

    def write(self, run_dir: Path, result: EventStudyResult) -> dict[str, Path]:
        run_dir.mkdir(parents=True, exist_ok=True)
        summary_csv = run_dir / "summary.csv"
        events_csv = run_dir / "events.csv"
        diagnostics_json = run_dir / "diagnostics.json"
        excel_path = run_dir / "result.xlsx"
        html_path = run_dir / "report.html"

        result.summary.to_csv(summary_csv, index=False)
        result.events.to_csv(events_csv, index=False, date_format="%Y-%m-%d")
        diagnostics_json.write_text(
            json.dumps(result.diagnostics, ensure_ascii=False, indent=2, default=str), "utf-8"
        )
        self._write_excel(excel_path, result)
        self._write_html(html_path, result)
        return {
            "summary_csv": summary_csv,
            "events_csv": events_csv,
            "diagnostics_json": diagnostics_json,
            "excel": excel_path,
            "html_report": html_path,
        }

    def _write_html(self, path: Path, result: EventStudyResult) -> None:
        template = self.environment.get_template("event_study.html.j2")
        summary_records = self._records(result.summary)
        max_abs_mean = max(
            (abs(float(row["mean_excess_return"])) for row in summary_records), default=0.01
        )
        chart_rows = []
        for row in summary_records:
            mean = float(row["mean_excess_return"])
            chart_rows.append(
                {
                    "window": row["window"],
                    "mean": mean,
                    "width": 100 * abs(mean) / max(max_abs_mean, 1e-12),
                    "direction": "positive" if mean >= 0 else "negative",
                }
            )
        html = template.render(
            spec=result.spec.to_dict(),
            summary=summary_records,
            events=self._records(result.events),
            diagnostics=result.diagnostics,
            chart_rows=chart_rows,
        )
        path.write_text(html, "utf-8")

    @staticmethod
    def _write_excel(path: Path, result: EventStudyResult) -> None:
        diagnostics = pd.DataFrame(
            [
                {"item": key, "value": json.dumps(value, ensure_ascii=False, default=str)}
                for key, value in result.diagnostics.items()
            ]
        )
        spec = pd.DataFrame(
            [{"item": key, "value": json.dumps(value, ensure_ascii=False, default=str)} for key, value in result.spec.to_dict().items()]
        )
        with pd.ExcelWriter(path, engine="openpyxl") as writer:
            result.summary.to_excel(writer, sheet_name="summary", index=False)
            result.events.to_excel(writer, sheet_name="events", index=False)
            spec.to_excel(writer, sheet_name="spec", index=False)
            diagnostics.to_excel(writer, sheet_name="diagnostics", index=False)
            workbook = writer.book
            for sheet in workbook.worksheets:
                sheet.freeze_panes = "A2"
                sheet.auto_filter.ref = sheet.dimensions
                for cell in sheet[1]:
                    cell.font = Font(bold=True, color="FFFFFF")
                    cell.fill = PatternFill("solid", fgColor="173B5E")
                    cell.alignment = Alignment(horizontal="center", vertical="center")
                for column in sheet.columns:
                    letter = column[0].column_letter
                    max_length = min(max(len(str(cell.value or "")) for cell in column) + 2, 48)
                    sheet.column_dimensions[letter].width = max(10, max_length)

