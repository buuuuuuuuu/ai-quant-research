from __future__ import annotations

import copy
import hashlib
import json
import re

import pandas as pd
from fastapi.testclient import TestClient
from test_data_explorer import _explorer_database
from test_web_workbench import _settings

from ai_quant_assistant.api.app import create_app
from ai_quant_assistant.api.schemas import FactorLabRequest
from ai_quant_assistant.providers.normalization import FINANCIAL_METRICS
from ai_quant_assistant.reports.factor_lab_writer import FactorLabReportWriter
from ai_quant_assistant.reports.presentation import (
    enum_label,
    factor_label,
    presentation_catalog,
    strategy_label,
)


def test_catalog_uses_existing_definitions_and_unknown_codes_are_not_replaced():
    assert presentation_catalog()["factors"] == {
        k: v["label"] for k, v in FINANCIAL_METRICS.items()
    }
    assert factor_label("roe") == "加权净资产收益率（roe）"
    assert factor_label("not-a-factor") == "未知因子（not-a-factor）"
    assert enum_label("asc") == "低值优先（asc）"
    assert "资产负债率" in strategy_label(
        "composite:roe+debt_asset_ratio|top=3|monthly|commission=5"
    )
    assert strategy_label("legacy-name") == "未识别策略（legacy-name）"


def report_fixture():
    spec = FactorLabRequest().model_dump(mode="json")
    spec.update(symbols=["600519", "000001"], start_date="2024-01-01", end_date="2024-12-31")
    strategies = pd.DataFrame(
        [
            {
                "strategy": "roe|top=1|monthly|commission=5",
                "factor": "roe",
                "top_n": 1,
                "rebalance": "monthly",
                "commission_bps": 5,
                "total_return": 0.1,
                "annualized_return": 0.1,
                "excess_return": 0.03,
                "max_drawdown": -0.05,
                "sharpe_ratio": None,
                "selected_by_validation": True,
            }
        ]
    )
    return {
        "spec": spec,
        "strategies": strategies,
        "ic_summary": pd.DataFrame(
            [
                {
                    "factor": "roe",
                    "periods": 4,
                    "mean_ic": 0.2,
                    "mean_rank_ic": 0.25,
                    "ic_ir": None,
                    "positive_ic_ratio": 0.75,
                }
            ]
        ),
        "segments": pd.DataFrame(
            [
                {
                    "strategy": strategies.iloc[0]["strategy"],
                    "segment": "test",
                    "start_date": "2024-10-01",
                    "end_date": "2024-12-31",
                    "total_return": 0.02,
                    "excess_return": 0.01,
                    "sharpe_ratio": None,
                    "selected_by_validation": True,
                }
            ]
        ),
        "yearly": pd.DataFrame(),
        "walk_forward": pd.DataFrame(),
        "diagnostics": {
            "universe_mode": "explicit",
            "industry_neutralization": "none",
            "max_industry_weight": 1,
            "survivorship_bias_warning": "显式范围存在幸存者偏差。",
        },
    }


def test_chinese_report_keeps_raw_frames_specs_and_exports(tmp_path):
    fixture = report_fixture()
    original = copy.deepcopy(fixture)
    paths = FactorLabReportWriter().write(tmp_path, **fixture)
    html = paths["html_report"].read_text()
    assert "加权净资产收益率（roe）" in html
    assert "高值优先（desc）" in html and "低值优先（asc）" in html
    assert "样本外测试集（test）" in html and "佣金 5 bp" in html
    assert "未记录" in html and "Pearson" in html
    assert "table-scroll" in html and "@media" in html
    assert fixture["spec"] == original["spec"]
    for name, frame in original.items():
        if isinstance(frame, pd.DataFrame):
            pd.testing.assert_frame_equal(fixture[name], frame)
    csv = paths["strategies_csv"].read_text()
    assert "roe|top=1|monthly|commission=5" in csv
    assert "加权净资产收益率" not in csv
    assert json.loads(paths["diagnostics_json"].read_text()) == fixture["diagnostics"]
    # Rendering another new report never walks or mutates existing report directories.
    before = hashlib.sha256(paths["html_report"].read_bytes()).hexdigest()
    another = tmp_path / "another"
    another.mkdir()
    FactorLabReportWriter().write(another, **fixture)
    assert hashlib.sha256(paths["html_report"].read_bytes()).hexdigest() == before


def test_report_html_escapes_unknown_factor_and_strategy(tmp_path):
    fixture = report_fixture()
    fixture["spec"]["factors"][0]["code"] = '<img src=x onerror="boom()">'
    fixture["strategies"].loc[0, "strategy"] = "<script>alert(1)</script>"
    html = FactorLabReportWriter().write(tmp_path, **fixture)["html_report"].read_text()
    assert "&lt;img" in html and "&lt;script&gt;" in html
    assert "<script>alert" not in html and "<img src" not in html


def test_independent_configuration_routes_and_catalog_are_read_only(tmp_path):
    database = _explorer_database(tmp_path)
    with TestClient(create_app(_settings(tmp_path))) as client:
        for route in ["/backtests", "/backtests/history", "/backtests/new", "/backtests/factors"]:
            page = client.get(route)
            assert page.status_code == 200
            assert 'id="center-catalog"' in page.text
            catalog = json.loads(
                re.search(r'<script id="center-catalog"[^>]*>(.*?)</script>', page.text).group(1)
            )
            assert catalog["factors"]["roe"] == "加权净资产收益率"
            assert 'id="backtest-form"' not in page.text
        for kind in ["factor_backtest", "factor_lab"]:
            config = client.get("/api/v1/run-center/configuration", params={"kind": kind}).json()
            assert config["source_run_id"] is None and config["params"]["symbols"] == []
            assert (
                "factors" in config["params"]
                if kind == "factor_lab"
                else "factor" in config["params"]
            )
        assert client.get("/api/v1/run-center/configuration?kind=unknown").status_code == 422
    with database.connect(read_only=True) as c:
        assert c.execute("SELECT COUNT(*) FROM operation_task").fetchone()[0] == 0
        assert c.execute("SELECT COUNT(*) FROM stock_research_run").fetchone()[0] == 0
