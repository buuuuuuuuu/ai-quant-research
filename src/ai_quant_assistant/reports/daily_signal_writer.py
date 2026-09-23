from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pandas as pd
from jinja2 import Environment, FileSystemLoader, select_autoescape
from openpyxl.styles import Font, PatternFill

from ai_quant_assistant.reports.presentation import install_filters, rule_label

FRAME_COLUMNS = {
    "signals": [
        "signal_date",
        "planned_execution_date",
        "symbol",
        "action",
        "status",
        "signal_price",
        "candidate_eligible",
        "candidate_score",
        "candidate_rank",
        "planned_shares",
        "rule_evidence",
    ],
    "orders": [
        "signal_date",
        "planned_execution_date",
        "execution_date",
        "symbol",
        "side",
        "status",
        "shares",
        "signal_price",
        "execution_price",
        "notional",
        "cost",
        "reason",
        "next_action",
        "candidate_score",
        "candidate_rank",
    ],
    "positions": [
        "date",
        "symbol",
        "shares",
        "price",
        "market_value",
        "candidate_score",
        "candidate_rank",
        "pending_exit",
    ],
    "candidates": [
        "signal_date",
        "symbol",
        "composite_score",
        "rank",
        "selected",
        "component_values",
        "component_sources",
    ],
}


class DailySignalReportWriter:
    def __init__(self) -> None:
        self.environment = Environment(
            loader=FileSystemLoader(Path(__file__).parent / "templates"),
            autoescape=select_autoescape(["html", "xml"], default=True),
        )
        install_filters(self.environment)

    def write(
        self,
        directory: Path,
        *,
        spec: dict[str, Any],
        metrics: dict[str, Any],
        nav: pd.DataFrame,
        signals: pd.DataFrame,
        orders: pd.DataFrame,
        positions: pd.DataFrame,
        candidates: pd.DataFrame,
        diagnostics: dict[str, Any],
    ) -> dict[str, Path]:
        directory.mkdir(parents=True, exist_ok=True)

        def with_columns(name: str, frame: pd.DataFrame) -> pd.DataFrame:
            expected = FRAME_COLUMNS[name]
            return frame.reindex(
                columns=[*expected, *(x for x in frame.columns if x not in expected)]
            )

        signals = with_columns("signals", signals)
        orders = with_columns("orders", orders)
        positions = with_columns("positions", positions)
        candidates = with_columns("candidates", candidates)
        paths = {
            "daily_nav_csv": directory / "daily_nav.csv",
            "signals_csv": directory / "signals.csv",
            "orders_csv": directory / "orders.csv",
            "positions_csv": directory / "positions.csv",
            "candidate_selections_csv": directory / "candidate_selections.csv",
            "metrics_json": directory / "metrics.json",
            "diagnostics_json": directory / "diagnostics.json",
            "excel": directory / "daily_signal_result.xlsx",
            "html_report": directory / "report.html",
        }
        frames = {
            "daily_nav": nav,
            "signals": signals,
            "orders": orders,
            "positions": positions,
            "candidates": candidates,
        }
        for name, frame in frames.items():
            key = "candidate_selections_csv" if name == "candidates" else f"{name}_csv"
            frame.to_csv(paths[key], index=False, date_format="%Y-%m-%d")
        paths["metrics_json"].write_text(
            json.dumps(metrics, ensure_ascii=False, indent=2, default=str), encoding="utf-8"
        )
        paths["diagnostics_json"].write_text(
            json.dumps(diagnostics, ensure_ascii=False, indent=2, default=str), encoding="utf-8"
        )
        with pd.ExcelWriter(paths["excel"], engine="openpyxl") as writer:
            pd.DataFrame([{"metric": k, "value": v} for k, v in metrics.items()]).to_excel(
                writer, sheet_name="metrics", index=False
            )
            for name, frame in frames.items():
                frame.to_excel(writer, sheet_name=name, index=False)
            for sheet in writer.book.worksheets:
                sheet.freeze_panes = "A2"
                for cell in sheet[1]:
                    cell.font = Font(bold=True, color="FFFFFF")
                    cell.fill = PatternFill("solid", fgColor="173B5E")
        signal_preview = signals.tail(80).to_dict(orient="records")
        for row in signal_preview:
            try:
                evidence = json.loads(row["rule_evidence"])
            except (TypeError, json.JSONDecodeError):
                row["rule_summary"] = "退出候选名单（candidate_drop）"
            else:
                row["rule_summary"] = "；".join(
                    f"{rule_label(item.get('rule'))}："
                    f"{'满足' if item.get('matched') is True else '不满足或数据不足'}"
                    for item in evidence
                )
        html = self.environment.get_template("daily_signal.html.j2").render(
            spec=spec,
            metrics=metrics,
            diagnostics=diagnostics,
            signals=signal_preview,
            orders=orders.tail(80).to_dict(orient="records"),
            candidates=candidates.loc[candidates.get("selected", False)]
            .tail(40)
            .to_dict(orient="records")
            if not candidates.empty
            else [],
        )
        paths["html_report"].write_text(html, encoding="utf-8")
        return paths
