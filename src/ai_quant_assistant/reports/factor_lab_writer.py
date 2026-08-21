from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pandas as pd
from jinja2 import Environment, FileSystemLoader, select_autoescape
from openpyxl.styles import Font, PatternFill


class FactorLabReportWriter:
    def __init__(self) -> None:
        self.environment = Environment(
            loader=FileSystemLoader(Path(__file__).parent / "templates"),
            autoescape=select_autoescape(["html", "xml"]),
        )
        self.environment.filters["pct"] = lambda value: (
            "—" if value is None or pd.isna(value) else f"{float(value) * 100:.2f}%"
        )
        self.environment.filters["number"] = lambda value: (
            "—" if value is None or pd.isna(value) else f"{float(value):.4f}"
        )

    def write(self, directory: Path, **data: Any) -> dict[str, Path]:
        frames = {name: frame for name, frame in data.items() if isinstance(frame, pd.DataFrame)}
        paths = {f"{name}_csv": directory / f"{name}.csv" for name in frames}
        for name, frame in frames.items():
            frame.to_csv(paths[f"{name}_csv"], index=False, date_format="%Y-%m-%d")
        paths["diagnostics_json"] = directory / "diagnostics.json"
        paths["diagnostics_json"].write_text(
            json.dumps(data["diagnostics"], ensure_ascii=False, indent=2, default=str),
            encoding="utf-8",
        )
        paths["excel"] = directory / "factor_lab.xlsx"
        with pd.ExcelWriter(paths["excel"], engine="openpyxl") as writer:
            for name, frame in frames.items():
                frame.to_excel(writer, sheet_name=name[:31], index=False)
            for sheet in writer.book.worksheets:
                sheet.freeze_panes = "A2"
                for cell in sheet[1]:
                    cell.font = Font(bold=True, color="FFFFFF")
                    cell.fill = PatternFill("solid", fgColor="173B5E")
        paths["html_report"] = directory / "report.html"
        context = {
            name: frame.tail(120).to_dict(orient="records") for name, frame in frames.items()
        }
        context.update(spec=data["spec"], diagnostics=data["diagnostics"])
        paths["html_report"].write_text(
            self.environment.get_template("factor_lab.html.j2").render(**context), encoding="utf-8"
        )
        return paths
