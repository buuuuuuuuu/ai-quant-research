from __future__ import annotations

import time
from threading import Event

import pytest

from ai_quant_assistant.api.managed_stock_tasks import ManagedStockTaskManager
from ai_quant_assistant.storage.database import LocalDatabase
from ai_quant_assistant.storage.layout import DataLayout
from ai_quant_assistant.storage.task_repository import OperationTaskRepository


class DispatcherService:
    def __init__(self, database: LocalDatabase, *, fail_attempts: int = 0) -> None:
        self.database = database
        self.tasks = OperationTaskRepository(database)
        self.fail_attempts = fail_attempts
        self.calls: list[str] = []
        self.called = Event()

    def run(self, task_id: str) -> dict[str, object]:
        self.calls.append(task_id)
        self.called.set()
        if self.fail_attempts:
            self.fail_attempts -= 1
            raise RuntimeError("download slot is temporarily busy")
        owner = f"fixture-owner-{len(self.calls)}"
        if self.tasks.start(task_id, owner_id=owner, message="fixture running"):
            self.tasks.succeed(task_id, owner_id=owner, message="fixture done")
        return self.tasks.get(task_id)


@pytest.fixture
def database(tmp_path) -> LocalDatabase:
    layout = DataLayout.from_root(tmp_path / "data")
    database = LocalDatabase(layout.root / "catalog.duckdb", layout)
    database.initialize()
    return database


def wait_until(predicate, *, timeout: float = 3.0) -> None:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return
        time.sleep(0.01)
    raise AssertionError("timed out waiting for dispatcher")


def manager(service, datasets, name) -> ManagedStockTaskManager:
    return ManagedStockTaskManager(service, datasets=datasets, worker_name=name)


def test_dispatchers_only_claim_their_configured_datasets(database) -> None:
    tasks = OperationTaskRepository(database)
    managed = tasks.create("sync", spec={"dataset": "managed_stock"})
    research = tasks.create("sync", spec={"dataset": "research_data_plan"})
    unknown = tasks.create("sync", spec={"dataset": "future_dataset"})
    managed_service = DispatcherService(database)
    research_service = DispatcherService(database)
    managed_manager = manager(
        managed_service,
        ("managed_stock", "managed_stock_gap_verification"),
        "managed-stock-test",
    )
    research_manager = manager(
        research_service,
        ("research_data_plan",),
        "research-data-test",
    )
    try:
        managed_manager.dispatch()
        research_manager.dispatch()
        wait_until(lambda: not managed_manager.inflight and not research_manager.inflight)
        assert managed_service.calls == [managed["task_id"]]
        assert research_service.calls == [research["task_id"]]
        assert tasks.get(managed["task_id"])["status"] == "succeeded"
        assert tasks.get(research["task_id"])["status"] == "succeeded"
        assert tasks.get(unknown["task_id"])["status"] == "queued"
    finally:
        managed_manager.shutdown()
        research_manager.shutdown()


def test_started_dispatcher_recovers_persisted_research_data_queue(database) -> None:
    tasks = OperationTaskRepository(database)
    queued = tasks.create("sync", spec={"dataset": "research_data_plan"})
    service = DispatcherService(database)
    dispatcher = manager(service, ("research_data_plan",), "research-data-restart-test")
    try:
        dispatcher.start()
        assert service.called.wait(3)
        wait_until(lambda: tasks.get(queued["task_id"])["status"] == "succeeded")
        assert service.calls == [queued["task_id"]]
    finally:
        dispatcher.shutdown()


def test_failed_attempt_remains_queued_and_can_be_dispatched_again(database) -> None:
    tasks = OperationTaskRepository(database)
    queued = tasks.create("sync", spec={"dataset": "research_data_plan"})
    service = DispatcherService(database, fail_attempts=1)
    dispatcher = manager(service, ("research_data_plan",), "research-data-retry-test")
    try:
        dispatcher.dispatch()
        wait_until(lambda: not dispatcher.inflight)
        assert tasks.get(queued["task_id"])["status"] == "queued"
        dispatcher.dispatch()
        wait_until(lambda: tasks.get(queued["task_id"])["status"] == "succeeded")
        assert service.calls == [queued["task_id"], queued["task_id"]]
    finally:
        dispatcher.shutdown()


def test_dispatcher_requires_explicit_dataset_scope(database) -> None:
    with pytest.raises(ValueError, match="at least one dataset"):
        manager(DispatcherService(database), (), "empty-test")
