.PHONY: install test lint demo api data-init data-status data-sync data-sync-index data-sync-announcements

install:
	uv sync --extra dev

test:
	uv run pytest

lint:
	uv run ruff check .

demo:
	uv run ai-quant event-study --source fixture --symbol 600519 --start-date 2012-01-01 --end-date 2025-12-31 --windows 5 10 20 --cost-bps 20

api:
	uv run uvicorn ai_quant_assistant.api.app:create_app --factory --host 0.0.0.0 --port 8000 --reload

data-init:
	uv run ai-quant data init --with-fixture

data-status:
	uv run ai-quant data status

data-sync:
	uv run ai-quant data sync --start-date 2024-01-01 --symbols 600519

data-sync-index:
	uv run ai-quant data sync-index --start-date 2024-01-01 --symbols csi000300

data-sync-announcements:
	uv run ai-quant data sync-announcements --start-date 2024-01-01 --symbols 600519 --types interim annual
