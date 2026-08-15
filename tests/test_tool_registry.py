import pytest

from ai_quant_assistant.ai.tool_registry import require_allowed_tool


def test_only_allowlisted_research_tool_can_run() -> None:
    require_allowed_tool("run_interim_report_event_study")
    with pytest.raises(PermissionError):
        require_allowed_tool("execute_arbitrary_python")

