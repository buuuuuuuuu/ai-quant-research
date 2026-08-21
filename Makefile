.PHONY: install test lint demo api data-init data-status data-sync data-sync-index data-sync-announcements data-sync-financial data-financial-status data-backfill batch-demo

install:
	uv sync --extra dev

test:
	uv run pytest

lint:
	uv run ruff check .

demo:
	uv run ai-quant event-study --source fixture --symbol 600519 --start-date 2012-01-01 --end-date 2025-12-31 --windows 5 10 20 --cost-bps 20

api:
	uv run uvicorn ai_quant_assistant.api.app:create_app --factory --host 0.0.0.0 --port 8080 --reload

data-init:
	uv run ai-quant data init --with-fixture

data-status:
	uv run ai-quant data status

data-sync:
	uv run ai-quant data sync --start-date 2024-01-01 --symbols 600519

data-sync-index:
	uv run ai-quant data sync-index --start-date 2024-01-01 --symbols csi000300

data-sync-announcements:
	uv run ai-quant data sync-announcements --start-date 2024-01-01 --symbols 600519 --types q1 interim q3 annual

data-sync-financial:
	uv run ai-quant data sync-financial --start-date 2024-01-01 --symbols 600519

data-financial-status:
	uv run ai-quant data financial-status --symbol 600519

data-backfill:
	uv run ai-quant data backfill-research --start-date 2012-01-01 --symbols 600519 000001

batch-demo:
	uv run ai-quant batch-event-study --source fixture --symbols 600519 --start-date 2012-01-01 --end-date 2025-12-31
