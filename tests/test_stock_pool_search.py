from fastapi.testclient import TestClient
from test_factor_backtest import _database
from test_web_workbench import _settings

from ai_quant_assistant.api.app import create_app


def test_security_search_exact_partial_name_and_missing(tmp_path):
    database = _database(tmp_path)
    with database.connect() as connection:
        connection.execute("UPDATE security_master SET name = '贵州茅台' WHERE symbol = '600519'")
        connection.execute(
            "UPDATE security_master SET name = '600519关联样本' WHERE symbol = '000001'"
        )
    with TestClient(create_app(_settings(tmp_path))) as client:
        exact = client.get("/api/v1/stock-pools/securities/search", params={"query": "600519"})
        assert exact.status_code == 200, exact.text
        assert [item["symbol"] for item in exact.json()] == ["600519", "000001"]
        assert exact.json()[0]["coverage"]["rows"] > 0
        assert (
            client.get("/api/v1/stock-pools/securities/search", params={"query": "贵州"}).json()[0][
                "name"
            ]
            == "贵州茅台"
        )
        assert (
            client.get("/api/v1/stock-pools/securities/search", params={"query": "600022"}).json()
            == []
        )
        assert (
            len(
                client.get(
                    "/api/v1/stock-pools/securities/search", params={"query": "600519", "limit": 1}
                ).json()
            )
            == 1
        )
        assert (
            client.get(
                "/api/v1/stock-pools/securities/search", params={"query": "' OR 1=1 --"}
            ).json()
            == []
        )
        assert (
            client.get(
                "/api/v1/stock-pools/securities/search", params={"query": "600", "limit": 51}
            ).status_code
            == 422
        )
