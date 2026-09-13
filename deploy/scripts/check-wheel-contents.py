"""Offline release check: the installed wheel must carry the stock UI/report."""
from pathlib import Path
from zipfile import ZipFile

required = {
    "ai_quant_assistant/api/run_center.py",
    "ai_quant_assistant/api/run_center_routes.py",
    "ai_quant_assistant/web/templates/app-nav.html",
    "ai_quant_assistant/web/templates/page-intro.html",
    "ai_quant_assistant/web/templates/run-center.html",
    "ai_quant_assistant/web/static/app-shell.css",
    "ai_quant_assistant/web/static/app-shell.js",
    "ai_quant_assistant/web/static/run-center.js",
    "ai_quant_assistant/web/templates/stock-research.html",
    "ai_quant_assistant/web/static/stock-research.js",
    "ai_quant_assistant/web/static/stock-research.css",
    "ai_quant_assistant/web/static/research-data.js",
    "ai_quant_assistant/reports/templates/stock_summary.html",
    "ai_quant_assistant/research/stock_workbench.py",
    "ai_quant_assistant/api/stock_research_routes.py",
    "ai_quant_assistant/api/research_dashboard_routes.py",
    "ai_quant_assistant/research/dashboard_brief.py",
    "ai_quant_assistant/web/templates/research-dashboard.html",
    "ai_quant_assistant/web/static/research-dashboard.js",
    "ai_quant_assistant/web/static/research-dashboard.css",
    "ai_quant_assistant/reports/templates/research_brief.html",
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
