from __future__ import annotations

ALLOWED_RESEARCH_TOOLS = frozenset({"run_interim_report_event_study"})


def require_allowed_tool(name: str) -> None:
    if name not in ALLOWED_RESEARCH_TOOLS:
        raise PermissionError(f"research tool is not allowlisted: {name}")
