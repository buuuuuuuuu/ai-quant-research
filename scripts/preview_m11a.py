"""Disposable fixture preview. Never reads deployment settings or production data."""

import sys
from pathlib import Path

import uvicorn

root = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(root / "tests"))
from test_data_explorer import _explorer_database  # noqa: E402
from test_web_workbench import _payload, _settings  # noqa: E402

from ai_quant_assistant.api.app import create_app  # noqa: E402
from ai_quant_assistant.api.schemas import FactorBacktestRequest  # noqa: E402
from ai_quant_assistant.research.factor_backtest import FactorBacktestService  # noqa: E402
from ai_quant_assistant.storage.research_run_index import register_run  # noqa: E402
from ai_quant_assistant.storage.task_repository import OperationTaskRepository  # noqa: E402

if __name__ == "__main__":
    import tempfile

    preview_root = Path(tempfile.mkdtemp(prefix="ai-quant-m11a-preview-"))
    database = _explorer_database(preview_root)
    settings = _settings(preview_root)
    spec = FactorBacktestRequest(**_payload()).to_spec()
    result = FactorBacktestService(database=database, artifact_dir=settings.artifact_dir).run(spec)
    for n in range(24):
        register_run(
            database,
            "factor_backtest",
            {
                "run_id": f"fixture-history-{n:02}",
                "spec": spec.to_dict(),
                "status": "succeeded",
                "completed_at": "2024-03-10T00:00:00Z",
                "metrics": {"total_return": 0.1, "max_drawdown": -0.03},
            },
        )
    tasks = OperationTaskRepository(database)
    task = tasks.create("backtest", spec=spec.to_dict())
    tasks.update(task["task_id"], status="failed", error="演示：缺少所选期间的数据")
    print("Fixture preview", result["run_id"], flush=True)
    uvicorn.run(create_app(settings), host="0.0.0.0", port=8765)
