from __future__ import annotations

import json
import time
from datetime import date

import pandas as pd
import pytest
from fastapi.testclient import TestClient
from test_factor_backtest import _database
from test_web_workbench import _settings

from ai_quant_assistant.api.app import create_app
from ai_quant_assistant.api.schemas import DailySignalStrategyRequest
from ai_quant_assistant.domain.models import (
    DailySignalRule,
    DailySignalStrategySpec,
    FactorComponent,
)
from ai_quant_assistant.reports.daily_signal_writer import DailySignalReportWriter
from ai_quant_assistant.research.daily_signal import DailySignalStrategyService


def test_daily_signal_request_has_actionable_chinese_validation():
    base = _spec().to_dict()
    base["buy_rules"] = [
        {
            "kind": "ma_cross",
            "operator": "cross_above",
            "short_window": 20,
            "long_window": 5,
        }
    ]
    with pytest.raises(ValueError, match="MA 短周期当前为 20，长周期为 5"):
        DailySignalStrategyRequest.model_validate(base)
    base = _spec().to_dict()
    base["max_positions"] = 2
    with pytest.raises(ValueError, match="最大持仓数量当前为 2，候选数量为 1"):
        DailySignalStrategyRequest.model_validate(base)


def _spec(**changes):
    values = dict(
        symbols=("600519", "000001"),
        start_date=date(2024, 1, 4),
        end_date=date(2024, 1, 12),
        candidate_factors=(FactorComponent("return_1"),),
        candidate_count=1,
        candidate_refresh="daily",
        buy_rules=(DailySignalRule("close_vs_ma", "above", window=2),),
        sell_rules=(DailySignalRule("close_vs_ma", "below", window=2),),
        max_positions=1,
        initial_capital=100_000,
    )
    values.update(changes)
    return DailySignalStrategySpec(**values)


def _run(tmp_path, spec=None):
    database = _database(tmp_path)
    service = DailySignalStrategyService(database=database, artifact_dir=tmp_path / "artifacts")
    return service.run(spec or _spec()), database


def test_rule_validation_and_cross_equal_boundary():
    with pytest.raises(ValueError):
        DailySignalRule("ma_cross", "cross_above", short_window=20, long_window=5)
    rule = DailySignalRule("ma_cross", "cross_above", short_window=2, long_window=3)
    frame = pd.DataFrame(
        {"ma_2": [2.0, 3.0], "ma_3": [2.0, 2.5], "signal_close": [2, 3]},
        index=pd.to_datetime(["2024-01-02", "2024-01-03"]),
    )
    matched, evidence = DailySignalStrategyService._rule_values(
        rule, frame, pd.Timestamp("2024-01-03")
    )
    assert matched is True
    assert evidence["previous_ma_2"] == evidence["previous_ma_3"]


def test_sma_return_and_deviation_use_complete_trading_windows(tmp_path):
    database = _database(tmp_path)
    service = DailySignalStrategyService(database=database, artifact_dir=tmp_path / "artifacts")
    prices = database.read_daily_signal_prices(("600519",), date(2024, 1, 8), "qfq")
    calendar = pd.DatetimeIndex(pd.bdate_range("2024-01-02", "2024-01-08"))
    frame = service._features(prices, calendar, {2})["600519"]
    day = pd.Timestamp("2024-01-04")
    assert frame.at[day, "ma_2"] == pytest.approx((100.3 + 100.6) / 2)
    assert frame.at[day, "return_2"] == pytest.approx(100.6 / 100.0 - 1)
    assert frame.at[day, "ma_deviation_2"] == pytest.approx(100.6 / ((100.3 + 100.6) / 2) - 1)
    prices = prices.loc[prices.date != pd.Timestamp("2024-01-03")]
    missing = service._features(prices, calendar, {2})["600519"]
    assert pd.isna(missing.at[day, "ma_2"])


def test_close_signal_executes_at_next_open_and_writes_trace(tmp_path):
    result, _ = _run(tmp_path)
    signals = pd.read_csv(result["artifacts"]["signals_csv"], dtype={"symbol": str})
    orders = pd.read_csv(result["artifacts"]["orders_csv"], dtype={"symbol": str})
    first = signals.iloc[0]
    fill = orders.loc[orders.status == "executed"].iloc[0]
    assert first.signal_date == "2024-01-04"
    assert first.planned_execution_date == "2024-01-05"
    assert fill.execution_date == "2024-01-05"
    assert fill.symbol == first.symbol
    assert json.loads(first.rule_evidence)[0]["matched"] is True
    assert result["diagnostics"]["opening_evidence"].startswith("opening price")
    assert result["summary"]["signal_count"] == len(signals)
    assert result["summary"]["order_attempt_count"] == len(orders)
    assert result["summary"]["executed_order_count"] == 1


def test_future_prices_do_not_change_past_signals_or_orders(tmp_path):
    result, database = _run(tmp_path / "original")
    signals = pd.read_csv(result["artifacts"]["signals_csv"])
    orders = pd.read_csv(result["artifacts"]["orders_csv"])
    with database.connect() as connection:
        connection.execute(
            "UPDATE daily_bar SET open=open*1.5,high=high*1.5,low=low*1.5,close=close*1.5 "
            "WHERE trade_date>=DATE '2024-01-10'"
        )
    service = DailySignalStrategyService(
        database=database, artifact_dir=tmp_path / "original" / "second-artifacts"
    )
    second = service.run(_spec())
    signals2 = pd.read_csv(second["artifacts"]["signals_csv"])
    orders2 = pd.read_csv(second["artifacts"]["orders_csv"])
    pd.testing.assert_frame_equal(
        signals.loc[signals.signal_date < "2024-01-10"].reset_index(drop=True),
        signals2.loc[signals2.signal_date < "2024-01-10"].reset_index(drop=True),
    )
    pd.testing.assert_frame_equal(
        orders.loc[orders.execution_date < "2024-01-10"].reset_index(drop=True),
        orders2.loc[orders2.execution_date < "2024-01-10"].reset_index(drop=True),
    )


def test_future_financial_publication_does_not_change_prior_candidates(tmp_path):
    database = _database(tmp_path)
    service = DailySignalStrategyService(database=database, artifact_dir=tmp_path / "first")
    spec = _spec(candidate_factors=(FactorComponent("roe"),))
    first = service.run(spec)
    before = pd.read_csv(first["artifacts"]["candidate_selections_csv"])
    with database.connect() as connection:
        connection.execute(
            """INSERT INTO financial_report (
                report_id,security_id,report_period_end,report_type,published_at,
                availability_source,source,source_batch_id,raw_sha256
            ) VALUES ('future-pingan','SZSE.000001','2023-12-31','annual',
                      '2024-01-10 18:00:00','provider_notice','fixture','future','sha')"""
        )
        connection.execute(
            """INSERT INTO financial_metric (
                report_id,security_id,report_period_end,published_at,metric_code,
                metric_value,unit,source_batch_id
            ) VALUES ('future-pingan','SZSE.000001','2023-12-31','2024-01-10 18:00:00',
                      'roe',1000,'PERCENT','future')"""
        )
    second = DailySignalStrategyService(database=database, artifact_dir=tmp_path / "second").run(
        spec
    )
    after = pd.read_csv(second["artifacts"]["candidate_selections_csv"])
    pd.testing.assert_frame_equal(
        before.loc[before.signal_date < "2024-01-11"].reset_index(drop=True),
        after.loc[after.signal_date < "2024-01-11"].reset_index(drop=True),
    )


def test_opening_decision_does_not_read_same_day_high_low_close_or_volume(tmp_path):
    result, database = _run(tmp_path / "base")
    before = pd.read_csv(result["artifacts"]["orders_csv"])
    with database.connect() as connection:
        connection.execute(
            "UPDATE daily_bar SET high=high*1.4,low=low*0.7,close=close*1.1,volume=0 "
            "WHERE trade_date=DATE '2024-01-05'"
        )
    second = DailySignalStrategyService(
        database=database, artifact_dir=tmp_path / "base" / "changed"
    ).run(_spec(end_date=date(2024, 1, 5)))
    after = pd.read_csv(second["artifacts"]["orders_csv"])
    columns = ["execution_date", "symbol", "side", "status", "shares", "execution_price", "reason"]
    pd.testing.assert_frame_equal(
        before.loc[before.execution_date == "2024-01-05", columns].reset_index(drop=True),
        after[columns].reset_index(drop=True),
    )


def test_missing_raw_bar_is_not_replaced_with_adjusted_open(tmp_path):
    database = _database(tmp_path)
    with database.connect() as connection:
        connection.execute(
            "DELETE FROM daily_bar WHERE security_id='SSE.600519' "
            "AND trade_date=DATE '2024-01-05' AND adjustment='none'"
        )
    result = DailySignalStrategyService(database=database, artifact_dir=tmp_path / "artifacts").run(
        _spec(end_date=date(2024, 1, 5))
    )
    orders = pd.read_csv(result["artifacts"]["orders_csv"])
    order = orders.loc[orders.symbol == 600519].iloc[0]
    assert order.status == "blocked"
    assert order.reason == "missing_raw_open"


def test_opening_gap_can_reduce_buy_to_affordable_round_lot(tmp_path):
    database = _database(tmp_path)
    with database.connect() as connection:
        connection.execute(
            "UPDATE daily_bar SET open=109,high=109.2 WHERE security_id='SSE.600519' "
            "AND trade_date=DATE '2024-01-05'"
        )
    result = DailySignalStrategyService(database=database, artifact_dir=tmp_path / "artifacts").run(
        _spec(commission_bps=100, slippage_bps=100, end_date=date(2024, 1, 5))
    )
    order = pd.read_csv(result["artifacts"]["orders_csv"]).iloc[0]
    assert order.status == "executed"
    assert order.shares == 800
    assert order.reason == "reduced_for_opening_gap_and_cash"
    assert order.shares % 100 == 0


def test_failed_state_buy_expires_without_automatic_daily_retry(tmp_path):
    database = _database(tmp_path)
    with database.connect() as connection:
        connection.execute(
            "UPDATE daily_bar SET open=110.66,high=110.66,low=100.7 "
            "WHERE security_id='SSE.600519' AND trade_date=DATE '2024-01-05'"
        )
    result = DailySignalStrategyService(database=database, artifact_dir=tmp_path / "artifacts").run(
        _spec()
    )
    signals = pd.read_csv(result["artifacts"]["signals_csv"])
    orders = pd.read_csv(result["artifacts"]["orders_csv"])
    assert len(signals.loc[(signals.symbol == 600519) & (signals.action == "buy")]) == 1
    assert len(orders.loc[(orders.symbol == 600519) & (orders.side == "buy")]) == 1
    assert orders.iloc[0].status == "blocked"
    assert orders.iloc[0].reason == "opening_at_upper_price_limit"


def test_persistent_limit_retry_does_not_duplicate_sell_signal(tmp_path):
    database = _database(tmp_path)
    with database.connect() as connection:
        connection.execute(
            "UPDATE daily_bar SET close=99,low=98.8 WHERE security_id='SSE.600519' "
            "AND trade_date=DATE '2024-01-08' AND adjustment='qfq'"
        )
        connection.execute(
            "UPDATE daily_bar SET open=91.08,low=90.9 WHERE security_id='SSE.600519' "
            "AND trade_date=DATE '2024-01-09' AND adjustment='none'"
        )
    result = DailySignalStrategyService(database=database, artifact_dir=tmp_path / "artifacts").run(
        _spec(
            candidate_refresh="monthly",
            sell_rules=(DailySignalRule("close_vs_ma", "below", window=2),),
        )
    )
    signals = pd.read_csv(result["artifacts"]["signals_csv"])
    orders = pd.read_csv(result["artifacts"]["orders_csv"])
    sells = signals.loc[signals.action == "sell"]
    attempts = orders.loc[orders.side == "sell"]
    assert len(sells) == 1
    assert attempts.status.tolist() == ["blocked", "executed"]
    assert attempts.reason.iloc[0] == "opening_at_lower_price_limit"
    assert attempts.signal_date.nunique() == 1
    assert attempts.next_action.tolist() == ["retry_next_trading_day", "closed"]


def test_candidate_refresh_can_create_explicit_drop_exit(tmp_path):
    database = _database(tmp_path)
    with database.connect() as connection:
        connection.execute(
            "UPDATE daily_bar SET close=100,low=99.8 WHERE security_id='SSE.600519' "
            "AND trade_date=DATE '2024-01-08' AND adjustment='qfq'"
        )
        connection.execute(
            "UPDATE daily_bar SET close=60,high=60.2 WHERE security_id='SZSE.000001' "
            "AND trade_date=DATE '2024-01-08' AND adjustment='qfq'"
        )
    spec = _spec(
        exit_on_candidate_drop=True,
        sell_rules=(DailySignalRule("return_threshold", "below", window=1, threshold=-10),),
    )
    result = DailySignalStrategyService(database=database, artifact_dir=tmp_path / "artifacts").run(
        spec
    )
    signals = pd.read_csv(result["artifacts"]["signals_csv"])
    dropped = signals.loc[signals.rule_evidence == "candidate_drop"]
    assert len(dropped) == 1
    assert dropped.iloc[0].symbol == 600519
    assert dropped.iloc[0].planned_execution_date == "2024-01-09"


def test_nav_cash_positions_and_open_end_state_reconcile(tmp_path):
    result, _ = _run(tmp_path, _spec(end_date=date(2024, 1, 5)))
    nav = pd.read_csv(result["artifacts"]["daily_nav_csv"])
    positions = pd.read_csv(result["artifacts"]["positions_csv"])
    final = nav.iloc[-1]
    final_positions = positions.loc[positions.date == final.date]
    assert final.portfolio_value == pytest.approx(final.cash + final_positions.market_value.sum())
    assert result["summary"]["open_position_count"] == 1
    assert result["diagnostics"]["open_positions"] == ["600519"]


def test_existing_factor_backtest_artifacts_are_not_reused(tmp_path):
    result, _ = _run(tmp_path)
    assert "/daily-signals/" in result["artifacts"]["run_manifest"]
    assert set((tmp_path / "artifacts" / "backtests").glob("*")) == set()


def test_daily_signal_html_escapes_unknown_display_codes(tmp_path):
    spec = _spec().to_dict()
    spec["candidate_factors"][0]["code"] = '<img src=x onerror="boom()">'
    path = DailySignalReportWriter().write(
        tmp_path,
        spec=spec,
        metrics={},
        nav=pd.DataFrame(columns=["date", "portfolio_nav"]),
        signals=pd.DataFrame(
            [
                {
                    "signal_date": "2024-01-04",
                    "planned_execution_date": "2024-01-05",
                    "symbol": "600519",
                    "action": "buy",
                    "status": "generated",
                    "signal_price": 10,
                    "rule_evidence": json.dumps(
                        [
                            {
                                "rule": {
                                    "kind": "ma_cross",
                                    "operator": "cross_above",
                                    "short_window": 5,
                                    "long_window": 20,
                                },
                                "matched": True,
                            }
                        ]
                    ),
                }
            ]
        ),
        orders=pd.DataFrame(
            [
                {
                    "signal_date": "2024-01-04",
                    "planned_execution_date": "2024-01-05",
                    "execution_date": "2024-01-05",
                    "symbol": "600519",
                    "side": "buy",
                    "status": "blocked",
                    "reason": "insufficient_cash_or_round_lot",
                    "next_action": "expired",
                }
            ]
        ),
        positions=pd.DataFrame(),
        candidates=pd.DataFrame(),
        diagnostics={},
    )["html_report"]
    html = path.read_text(encoding="utf-8")
    assert "&lt;img" in html
    assert "<img src" not in html
    assert "信号 → 指令 → 成交或未成交" in html
    assert "资金不足或不足一手（insufficient_cash_or_round_lot）" in html
    assert "MA(5) 上穿 MA(20)" in html


def test_api_history_copy_report_and_read_only_configuration(tmp_path):
    database = _database(tmp_path)
    payload = _spec().to_dict()
    with database.connect(read_only=True) as connection:
        before = connection.execute("SELECT COUNT(*) FROM operation_task").fetchone()[0]
    with TestClient(create_app(_settings(tmp_path))) as client:
        page = client.get("/backtests/daily-signals")
        config = client.get("/api/v1/run-center/configuration?kind=daily_signal")
        assert page.status_code == 200 and "日线信号策略" in page.text
        assert config.status_code == 200
        assert config.json()["params"]["symbols"] == []
        with database.connect(read_only=True) as connection:
            assert connection.execute("SELECT COUNT(*) FROM operation_task").fetchone()[0] == before

        created = client.post("/api/v1/daily-signal-strategies", json=payload)
        assert created.status_code == 202
        task_id = created.json()["task_id"]
        for _ in range(200):
            task = client.get(f"/api/v1/daily-signal-strategies/{task_id}").json()
            if task["status"] in {"succeeded", "failed"}:
                break
            time.sleep(0.025)
        assert task["status"] == "succeeded", task
        run_id = task["run_id"]

        detail = client.get(f"/api/v1/daily-signal-runs/{run_id}")
        report = client.get(f"/reports/daily-signals/{run_id}")
        history = client.get("/api/v1/run-center?kind=daily_signal&status=succeeded")
        copied = client.get("/api/v1/run-center/copy", params={"source": "index", "record": run_id})
        assert detail.status_code == report.status_code == copied.status_code == 200
        assert detail.json()["spec"]["candidate_factors"][0]["code"] == "return_1"
        assert detail.json()["signals"][0]["planned_execution_date"] == "2024-01-05"
        assert "1 日涨跌幅（return_1）" in report.text
        assert history.json()["total"] == 1
        copied_params = copied.json()["params"]
        assert copied_params["candidate_factors"] == payload["candidate_factors"]
        assert copied_params["buy_rules"] == payload["buy_rules"]
        assert copied_params["symbols"] == payload["symbols"]
        assert copied.json()["source_run_id"] == run_id
        assert (
            client.get(f"/api/v1/daily-signal-runs/{run_id}/downloads/signals.csv").status_code
            == 200
        )
        assert (
            client.get(f"/api/v1/daily-signal-runs/{run_id}/downloads/../../secret").status_code
            == 404
        )
