from datetime import date

import pytest
from test_research_datasets import FakeResearchProvider, _services

from ai_quant_assistant.domain.models import AnnouncementSyncSpec
from ai_quant_assistant.errors import ProviderError
from ai_quant_assistant.providers.cninfo import AnnouncementResponseError


@pytest.mark.parametrize("status,attempted", [(200, 3), (403, 1), (429, 1)])
def test_outage_pauses_unattempted_partitions_and_resume_recovers(tmp_path, monkeypatch, status, attempted):
    provider = FakeResearchProvider()
    service, _, database = _services(tmp_path, provider)
    calls = []
    original = provider.get_report_announcements

    def unavailable(*args):
        calls.append(args)
        try:
            raise AnnouncementResponseError("公告查询", "json" if status == 200 else "http", status=status)
        except AnnouncementResponseError as exc:
            raise ProviderError("wrapped provider error") from exc

    monkeypatch.setattr(provider, "get_report_announcements", unavailable)
    spec = AnnouncementSyncSpec(start_date=date(2020, 1, 1), end_date=date(2025, 12, 31),
                                symbols=("600519",), report_types=("annual", "interim"))
    first = service.sync_announcements(spec)
    assert len(calls) == attempted
    assert first["status"] == "failed" and first["paused_after_provider_failure"]
    assert first["partitions"] == {"failed": attempted, "planned": 12 - attempted}
    assert "未标记完成" in first["error"]
    with database.connect(read_only=True) as connection:
        assert connection.execute("SELECT COUNT(*) FROM data_coverage WHERE dataset='announcement'").fetchone()[0] == 0
        assert connection.execute("SELECT COUNT(*) FROM published_announcement").fetchone()[0] == 0
        assert connection.execute("SELECT SUM(attempts) FROM research_sync_partition").fetchone()[0] == attempted
        assert connection.execute("SELECT error FROM sync_batch WHERE batch_id=?", [first["batch_id"]]).fetchone()[0] == first["error"]
    monkeypatch.setattr(provider, "get_report_announcements", original)
    second = service.sync_announcements(spec)
    assert second["resumed"] and second["batch_id"] == first["batch_id"]
    assert second["status"] == "published" and second["partitions"] == {"published": 12}
    assert not second["paused_after_provider_failure"] and second["error"] is None


def test_success_resets_outage_streak_and_published_partitions_survive_resume(tmp_path, monkeypatch):
    provider = FakeResearchProvider()
    service, _, database = _services(tmp_path, provider)
    original = provider.get_report_announcements
    calls = []

    def intermittent(*args):
        calls.append(args)
        if len(calls) not in {3, 6}:
            raise AnnouncementResponseError("公告查询", "json", status=200)
        return original(*args)

    monkeypatch.setattr(provider, "get_report_announcements", intermittent)
    spec = AnnouncementSyncSpec(start_date=date(2020, 1, 1), end_date=date(2025, 12, 31),
                                symbols=("600519",), report_types=("annual",))
    first = service.sync_announcements(spec)
    assert len(calls) == 6 and not first["paused_after_provider_failure"]
    assert first["partitions"] == {"published": 2, "failed": 4}
    with database.connect(read_only=True) as connection:
        published = connection.execute("SELECT partition_key, raw_sha256, attempts FROM research_sync_partition WHERE status='published' ORDER BY partition_key").fetchall()
    monkeypatch.setattr(provider, "get_report_announcements", original)
    second = service.sync_announcements(spec)
    assert second["status"] == "published"
    with database.connect(read_only=True) as connection:
        for key, digest, attempts in published:
            assert connection.execute("SELECT raw_sha256, attempts FROM research_sync_partition WHERE partition_key=?", [key]).fetchone() == (digest, attempts)


def test_unclassified_partition_failures_do_not_trigger_provider_outage_breaker(tmp_path, monkeypatch):
    provider = FakeResearchProvider()
    service, _, _ = _services(tmp_path, provider)

    def fail(*args):
        raise ValueError("invalid fixture report period")

    monkeypatch.setattr(provider, "get_report_announcements", fail)
    result = service.sync_announcements(AnnouncementSyncSpec(
        start_date=date(2020, 1, 1), end_date=date(2024, 12, 31),
        symbols=("600519",), report_types=("annual",),
    ))
    assert result["partitions"] == {"failed": 5}
    assert not result["paused_after_provider_failure"]
