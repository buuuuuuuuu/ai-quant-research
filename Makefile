.PHONY: install test lint demo api

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

