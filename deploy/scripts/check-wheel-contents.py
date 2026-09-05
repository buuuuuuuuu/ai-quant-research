"""Offline release check: the installed wheel must carry the stock UI/report."""
from pathlib import Path
from zipfile import ZipFile

required = {
    "ai_quant_assistant/web/templates/stock-research.html",
    "ai_quant_assistant/web/static/stock-research.js",
    "ai_quant_assistant/web/static/stock-research.css",
    "ai_quant_assistant/web/static/research-data.js",
    "ai_quant_assistant/reports/templates/stock_summary.html",
    "ai_quant_assistant/research/stock_workbench.py",
    "ai_quant_assistant/api/stock_research_routes.py",
}
wheel = sorted(Path("dist").glob("*.whl"))[-1]
with ZipFile(wheel) as archive:
    names = set(archive.namelist())
    missing = required - names
    if missing:
        raise SystemExit(f"Wheel is missing M10-A assets: {sorted(missing)}")
    forbidden = [name for name in names if name.endswith((".duckdb", ".pdf", ".env"))]
    if forbidden:
        raise SystemExit(f"Unexpected runtime data in wheel: {forbidden}")
print(f"Verified {wheel.name}: stock UI, shared readiness and summary template included")
