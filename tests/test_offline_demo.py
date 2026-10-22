"""Public demo must be reproducible, isolated and offline."""

import importlib.util
import socket
from pathlib import Path

import pandas as pd
import pytest


def test_public_demo_is_offline_isolated_and_refuses_overwrite(tmp_path, monkeypatch):
    path = Path(__file__).resolve().parents[1] / "examples/offline-demo/demo.py"
    spec = importlib.util.spec_from_file_location("public_demo", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)

    def no_network(*args, **kwargs):
        raise AssertionError("offline example attempted a network connection")

    monkeypatch.setattr(socket.socket, "connect", no_network)
    monkeypatch.setenv("AI_QUANT_DUCKDB_PATH", str(tmp_path / "private.duckdb"))
    monkeypatch.setenv("AI_QUANT_ARTIFACT_DIR", str(tmp_path / "private-artifacts"))
    settings, manifest = module.build_demo(tmp_path / "demo")
    assert not (tmp_path / "private.duckdb").exists()
    assert not (tmp_path / "private-artifacts").exists()
    assert settings.web_password == "" and settings.openai_api_key == ""
    assert manifest["data_kind"] == "synthetic_demo_not_market_data"
    report = Path(manifest["report"])
    assert report.is_file()
    orders = pd.read_csv(report.parent / "orders.csv")
    assert {"buy", "sell"} <= set(orders.loc[orders.status.eq("executed"), "side"])
    assert (report.parent / "account_audit.json").is_file()
    with pytest.raises(FileExistsError):
        module.build_demo(tmp_path / "demo")
    _, second = module.build_demo(tmp_path / "second")
    for name in ("daily_nav.csv", "orders.csv", "signals.csv", "positions.csv"):
        assert (report.parent / name).read_bytes() == (
            Path(second["report"]).parent / name
        ).read_bytes()
