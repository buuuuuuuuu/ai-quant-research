from datetime import date
from decimal import Decimal

import pytest
from fastapi.testclient import TestClient

from ai_quant_assistant.api.app import create_app
from ai_quant_assistant.config import Settings
from ai_quant_assistant.research.national_team import DEFAULT_FUNDS, calculate, slots
from ai_quant_assistant.storage.national_team import Scope, validate_rows

FUNDS = DEFAULT_FUNDS[:2]
DAYS = ["2025-03-03", "2025-03-04", "2025-03-05"]


def rows():
    result = []
    for fund in FUNDS:
        for day, shares in zip(DAYS, (100, 120, 110), strict=True):
            result.extend([
                {"kind": "shares", "code": fund["code"], "stamp": day, "shares": str(shares)},
                {"kind": "nav", "code": fund["code"], "stamp": day, "nav": "2.5"},
                {"kind": "daily", "code": fund["code"], "stamp": day, "amount": "100", "close": "2.5"},
            ])
    return result


def scope():
    return Scope(funds=FUNDS, start=date(2025, 3, 4), end=date(2025, 3, 5), threshold=Decimal(90))


def test_flows_are_not_turnover_and_missing_is_not_zero():
    result = calculate(FUNDS, DAYS, rows(), DAYS[1], DAYS[2], threshold="90")
    assert [p["flow"] for p in result["daily"]] == ["100.0", "-50.0"]
    assert result["daily"][0]["amount"] == "200"
    assert result["sample_days"] == [DAYS[1]]
    incomplete = [r for r in rows() if not (r["kind"] == "nav" and r["stamp"] == DAYS[1] and r["code"] == FUNDS[0]["code"])]
    point = calculate(FUNDS, DAYS, incomplete, DAYS[1], DAYS[2])["daily"][0]
    assert point["flow"] is None and point["covered"] == 1
    assert point["groups"]["沪深300"]["flow"] is None
    assert point["groups"]["科创50"]["flow"] is None


def test_split_and_exclusions_never_become_buy_signals():
    values = rows()
    values[3]["corporate_action"] = True
    result = calculate(FUNDS, DAYS, values, DAYS[1], DAYS[2], threshold="90")
    assert all(x["flow"] is None for x in result["daily"])
    result = calculate(FUNDS, DAYS, rows(), DAYS[1], DAYS[2], threshold="90", excluded=[DAYS[1]])
    assert result["sample_days"] == []


def test_minute_gap_breaks_cumulative_and_twenty_day_mean():
    minutes = []
    for day in DAYS:
        for fund in FUNDS:
            for clock in slots():
                if day == DAYS[-1] and clock == "09:32" and fund == FUNDS[0]:
                    continue
                minutes.append({"kind": "minute", "code": fund["code"], "stamp": day+" "+clock, "amount": "10", "close": "2"})
    minute = calculate(FUNDS, DAYS, minutes, DAYS[0], DAYS[-1])["minute"]
    assert minute[0]["cumulative"] == "20"
    assert minute[1]["cumulative"] is None and minute[2]["cumulative"] is None
    assert minute[2]["amount"] == "20"
    assert minute[0]["mean20"] is None and minute[0]["history_count"] == 2
    assert minute[0]["previous"] == "20"
    assert slots()[119:121] == ["11:30", "13:01"]


@pytest.mark.parametrize("value", ["NaN", "Infinity", "-1", "bad"])
def test_input_rejects_invalid_amounts(value):
    with pytest.raises(ValueError):
        validate_rows([{"kind": "minute", "code": FUNDS[0]["code"], "stamp": "2025-03-03 09:31", "amount": value, "close": "3"}])


@pytest.fixture
def app(tmp_path):
    return create_app(Settings(data_dir=tmp_path / "data", artifact_dir=tmp_path / "artifacts", cache_dir=tmp_path / "cache", duckdb_path=tmp_path / "data/catalog.duckdb"))


def test_page_queries_and_demo_do_not_collect(app):
    class Forbidden:
        def __getattr__(self, name):
            raise AssertionError("page/query must not use provider")
    app.state.national_team.provider = Forbidden()
    with TestClient(app) as client:
        assert client.get("/market/national-team").status_code == 200
        assert len(client.get("/api/v1/national-team/catalog").json()["funds"]) == 20
        response = client.post("/api/v1/national-team/query", json=scope().model_dump(mode="json"))
        assert response.status_code == 200 and response.json()["daily"] == []
        demo = client.get("/api/v1/national-team/demo").json()
        assert demo["demo"] and demo["minute"][0]["history_count"] == 20
        assert demo["minute"][0]["mean20"] is not None
        bad = client.post("/api/v1/national-team/sync", json={**scope().model_dump(mode="json"), "confirm": False})
        assert bad.status_code == 422


def test_import_audit_revision_and_missing_values(app):
    service = app.state.national_team
    payload = {"scope": scope().model_dump(mode="json"), "source": "test fixture", "confirm": True,
               "rows": rows()+[{"kind": "calendar", "code": "INDEX.000001", "stamp": d} for d in DAYS]}
    with TestClient(app) as client:
        first = client.post("/api/v1/national-team/import", json=payload)
        assert first.status_code == 201
        result = service.read(scope())
        assert result["daily"][0]["flow"] == "100.0" and result["batches"][0]["source"] == "user-import:test fixture"
        old_hash = result["input_sha256"]
        # Identical refresh creates no duplicated observations.
        assert client.post("/api/v1/national-team/import", json=payload).status_code == 201
        with service.database.connect(read_only=True) as c:
            assert c.execute("SELECT count(*) FROM national_team_observation").fetchone()[0] == len(payload["rows"])
        payload["rows"][3]["shares"] = "130"
        assert client.post("/api/v1/national-team/import", json=payload).status_code == 201
        result = service.read(scope())
        assert result["daily"][0]["flow"] == "125.0" and result["input_sha256"] != old_hash
        payload["rows"].append(dict(payload["rows"][0]))
        assert client.post("/api/v1/national-team/import", json=payload).status_code == 422
        export = client.post("/api/v1/national-team/export", json=scope().model_dump(mode="json"))
        assert export.status_code == 200 and export.json()["daily"][0]["flow"] == "125.0"


def test_sync_failure_keeps_prior_data_and_task_ownership(app):
    service = app.state.national_team
    service.database.initialize()
    with service.database.connect() as c:
        for n, day in enumerate(DAYS):
            c.execute("INSERT INTO trading_calendar(exchange,trade_date,is_open,source,source_batch_id,session_no) VALUES ('SSE',?,TRUE,'fixture','fixture',?)", [day, n])
    service.save({}, rows(), "fixture")
    class Provider:
        def bars(self, *args):
            raise ValueError("failed")
        def fetch_nav(self, *args):
            raise ValueError("failed")
        def fetch_shares(self, *args):
            raise ValueError("failed")
    service.provider = Provider()
    task = service.request(**scope().model_dump(), confirm=True)
    assert service.request(**scope().model_dump(), confirm=True)["task_id"] == task["task_id"]
    service.run(task["task_id"])
    assert service.tasks.get(task["task_id"])["status"] == "failed"
    assert service.read(scope())["daily"][0]["flow"] == "100.0"


def test_schedule_requires_explicit_confirmation(app):
    with TestClient(app) as client:
        assert client.get("/api/v1/national-team/schedule").json()["enabled"] is False
        body = {"scope": scope().model_dump(mode="json"), "enabled": True, "confirm": False}
        assert client.post("/api/v1/national-team/schedule", json=body).status_code == 422
        body["confirm"] = True
        assert client.post("/api/v1/national-team/schedule", json=body).json()["enabled"] is True
        body["enabled"] = False
        assert client.post("/api/v1/national-team/schedule", json=body).json()["enabled"] is False


def test_public_provider_pagination_and_share_unit(monkeypatch):
    from ai_quant_assistant.providers.national_team import NationalTeamProvider

    class Response:
        def __init__(self, payload):
            self.payload = payload
        def raise_for_status(self):
            pass
        def json(self):
            return self.payload
    def get(url, **kwargs):
        assert kwargs["timeout"] == 20
        if "sse.com.cn" in url:
            return Response({"result": [{"STAT_DATE": "2025-03-04", "SEC_CODE": "510300", "TOT_VOL": "12.34"}]})
        page = kwargs["params"]["pageIndex"]
        return Response({"TotalCount": 2, "Data": {"LSJZList": [{"FSRQ": f"2025-03-0{page + 2}", "DWJZ": "2.5"}]}})
    monkeypatch.setattr("ai_quant_assistant.providers.national_team.requests.get", get)
    provider = NationalTeamProvider()
    assert provider.fetch_shares("SSE", "2025-03-04", ["SSE.510300"])[0]["shares"] == "123400.00"
    assert len(provider.fetch_nav("SSE.510300", "2025-03-03", "2025-03-05")) == 2
    monkeypatch.setattr("ai_quant_assistant.providers.national_team.requests.get", lambda *a, **kw: Response({"TotalCount": 1, "Data": {"LSJZList": []}}))
    with pytest.raises(ValueError, match="分页不完整"):
        provider.fetch_nav("SSE.510300", "2025-03-03", "2025-03-05")


def test_successful_collection_releases_claim_and_publishes(app):
    service = app.state.national_team
    service.database.initialize()
    with service.database.connect() as c:
        for n, day in enumerate(DAYS):
            c.execute("INSERT INTO trading_calendar(exchange,trade_date,is_open,source,source_batch_id,session_no) VALUES ('SSE',?,TRUE,'fixture','fixture',?)", [day, n])
    class Provider:
        def bars(self, code, start, end, minute=False):
            return [r for r in rows() if r["kind"] == "daily" and r["code"] == code]
        def fetch_nav(self, code, start, end):
            return [r for r in rows() if r["kind"] == "nav" and r["code"] == code]
        def fetch_shares(self, market, day, codes):
            return [r for r in rows() if r["kind"] == "shares" and r["stamp"] == day]
    service.provider = Provider()
    task = service.request(**scope().model_dump(), confirm=True)
    service.run(task["task_id"])
    assert service.tasks.get(task["task_id"])["status"] == "succeeded"
    assert service.read(scope())["daily"][0]["flow"] == "100.0"
    assert service.request(**scope().model_dump(), confirm=True)["task_id"] != task["task_id"]


def test_scheduler_submits_once_and_stops_when_disabled(app, monkeypatch):
    from datetime import datetime

    class Clock(datetime):
        @classmethod
        def now(cls, tz=None):
            return cls(2025, 3, 5, 10, 0, tzinfo=tz)
    monkeypatch.setattr("ai_quant_assistant.storage.national_team.datetime", Clock)
    service = app.state.national_team
    service.database.initialize()
    with service.database.connect() as c:
        for n, day in enumerate(["2025-02-20", *DAYS]):
            c.execute("INSERT INTO trading_calendar(exchange,trade_date,is_open,source,source_batch_id,session_no) VALUES ('SSE',?,TRUE,'fixture','fixture',?)", [day, n])
    service.set_schedule(scope(), True)
    service.tick()
    service.tick()
    with service.database.connect(read_only=True) as c:
        assert c.execute("SELECT count(*) FROM operation_task WHERE json_extract_string(spec_json,'$.dataset')='national_team'").fetchone()[0] == 1
    service.set_schedule(scope(), False)
    service.tick()
    with service.database.connect(read_only=True) as c:
        assert c.execute("SELECT count(*) FROM operation_task").fetchone()[0] == 1


@pytest.mark.parametrize("mode", ["daily", "minute"])
def test_confirmed_sync_api_dispatches_and_completes(app, mode):
    import time

    service = app.state.national_team
    service.database.initialize()
    with service.database.connect() as c:
        for n, day in enumerate(DAYS):
            c.execute("INSERT INTO trading_calendar(exchange,trade_date,is_open,source,source_batch_id,session_no) VALUES ('SSE',?,TRUE,'fixture','fixture',?)", [day, n])

    class Provider:
        def bars(self, code, start, end, minute=False):
            if minute:
                return [{"kind": "minute", "code": code, "stamp": DAYS[-1] + " 09:31", "amount": "10", "close": "2.5"}]
            return [r for r in rows() if r["kind"] == "daily" and r["code"] == code]

        def fetch_nav(self, code, start, end):
            return [r for r in rows() if r["kind"] == "nav" and r["code"] == code]

        def fetch_shares(self, market, day, codes):
            return [r for r in rows() if r["kind"] == "shares" and r["stamp"] == day]

    service.provider = Provider()
    with TestClient(app) as client:
        response = client.post("/api/v1/national-team/sync", json={**scope().model_dump(mode="json"), "mode": mode, "confirm": True})
        assert response.status_code == 202, response.text
        task_id = response.json()["task_id"]
        deadline = time.monotonic() + 10
        while time.monotonic() < deadline:
            task = client.get(f"/api/v1/national-team/tasks/{task_id}").json()
            if task["status"] not in {"queued", "running"}:
                break
            time.sleep(0.02)
        assert task["status"] == "succeeded", task
        result = client.post("/api/v1/national-team/query", json=scope().model_dump(mode="json")).json()
        if mode == "daily":
            assert result["daily"][0]["flow"] == "100.0"
        else:
            assert result["minute"][0]["amount"] == "20"


def test_nav_pagination_continues_past_twenty_pages(monkeypatch):
    from datetime import timedelta

    from ai_quant_assistant.providers.national_team import NationalTeamProvider

    pages = []
    class Response:
        def __init__(self, page):
            self.page = page
        def raise_for_status(self):
            pass
        def json(self):
            day = (date(2025, 3, 1) + timedelta(days=self.page)).isoformat()
            return {"TotalCount": 21, "Data": {"LSJZList": [{"FSRQ": day, "DWJZ": "2.5"}]}}
    def get(url, **kwargs):
        assert kwargs["params"]["pageSize"] == 20
        pages.append(kwargs["params"]["pageIndex"])
        return Response(pages[-1])
    monkeypatch.setattr("ai_quant_assistant.providers.national_team.requests.get", get)
    assert len(NationalTeamProvider().fetch_nav("SSE.510300", "2025-03-01", "2025-03-31")) == 21
    assert pages[-1] == 21


def test_nav_failure_does_not_prevent_share_collection(app):
    service = app.state.national_team
    service.database.initialize()
    with service.database.connect() as c:
        for n, day in enumerate(DAYS):
            c.execute("INSERT INTO trading_calendar(exchange,trade_date,is_open,source,source_batch_id,session_no) VALUES ('SSE',?,TRUE,'fixture','fixture',?)", [day, n])
    calls = []
    class Provider:
        def bars(self, code, start, end, minute=False):
            return [r for r in rows() if r["kind"] == "daily" and r["code"] == code]
        def fetch_nav(self, code, start, end):
            calls.append("nav")
            raise ValueError("净值分页不完整，未发布此基金净值")
        def fetch_shares(self, market, day, codes):
            calls.append("shares:"+market)
            return [r for r in rows() if r["kind"] == "shares" and r["stamp"] == day and r["code"].startswith(market)]
    service.provider = Provider()
    request = scope().model_dump()
    request["funds"] = DEFAULT_FUNDS[:6]
    task = service.request(**request, confirm=True)
    service.run(task["task_id"])
    assert calls.count("nav") == 5
    assert "shares:SSE" in calls and "shares:SZSE" in calls
    result = service.read(scope())
    assert result["daily"][0]["details"][0]["shares"] == "120"
    assert result["daily"][0]["details"][0]["missing"] == ["当日单位净值"]
    assert result["daily"][0]["flow"] is None
    assert any(e.get("reason") == "净值分页不完整，未发布此基金净值" for e in result["batches"][0]["errors"])


def test_empty_daily_inputs_are_visible_and_received_counts_survive_dedup(app):
    service = app.state.national_team
    service.database.initialize()
    with service.database.connect() as c:
        for n, day in enumerate(DAYS):
            c.execute("INSERT INTO trading_calendar(exchange,trade_date,is_open,source,source_batch_id,session_no) VALUES ('SSE',?,TRUE,'fixture','fixture',?)", [day, n])
    class Provider:
        def bars(self, code, start, end, minute=False):
            return [r for r in rows() if r["kind"] == "daily" and r["code"] == code]
        def fetch_nav(self, *args):
            return []
        def fetch_shares(self, *args):
            return []
    service.provider = Provider()
    for run in range(2):
        task = service.request(**scope().model_dump(), confirm=True)
        service.run(task["task_id"])
        finished = service.tasks.get(task["task_id"])
        assert finished["result"]["received_counts"] == {"daily": 6}
        assert any(e["partition"].startswith("nav:") and e["error"] == "EmptyResponse" for e in finished["result"]["errors"])
        assert any(e["partition"].startswith("shares:") and e["error"] == "EmptyResponse" for e in finished["result"]["errors"])
        if run:
            assert finished["result"]["records"] == 0
        latest = service.read(scope())["latest_daily"]
        assert latest["result"] == finished["result"]
        assert latest["start"] == "2025-03-04"
