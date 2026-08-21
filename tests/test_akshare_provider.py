from datetime import date

import pandas as pd

from ai_quant_assistant.providers.akshare_provider import AkShareProvider


class FakeAkShare:
    @staticmethod
    def stock_zh_index_daily_em(**_: str) -> pd.DataFrame:
        raise ValueError("primary endpoint unavailable")

    @staticmethod
    def stock_zh_index_daily(symbol: str) -> pd.DataFrame:
        assert symbol == "sh000300"
        return pd.DataFrame(
            {
                "date": ["2023-12-29", "2024-01-02", "2024-01-03", "2024-01-04"],
                "close": [99.0, 100.0, 101.0, 102.0],
            }
        )

    @staticmethod
    def stock_zh_a_hist(**_: str) -> pd.DataFrame:
        raise ConnectionError("EastMoney disconnected")

    @staticmethod
    def stock_zh_a_daily(symbol: str, adjust: str) -> pd.DataFrame:
        assert symbol == "sh601318"
        assert adjust == "hfq"
        return pd.DataFrame(
            {
                "date": ["2024-01-01", "2024-01-02", "2024-01-03", "2024-01-04"],
                "close": [99.0, 100.0, 101.0, 102.0],
            }
        )


class FakeSyncAkShare:
    @staticmethod
    def stock_info_a_code_name() -> pd.DataFrame:
        return pd.DataFrame(
            {"code": ["600519", "000001", "430047"], "name": ["贵州茅台", "平安银行", "诺思兰德"]}
        )

    @staticmethod
    def tool_trade_date_hist_sina() -> pd.DataFrame:
        return pd.DataFrame({"trade_date": ["2024-01-02", "2024-01-03"]})

    @staticmethod
    def stock_zh_a_hist(**kwargs) -> pd.DataFrame:
        assert kwargs["symbol"] == "600519"
        assert kwargs["adjust"] == ""
        return pd.DataFrame(
            {
                "日期": ["2024-01-02", "2024-01-03"],
                "开盘": [10.0, 10.2],
                "最高": [10.5, 10.6],
                "最低": [9.9, 10.1],
                "收盘": [10.3, 10.4],
                "成交量": [1000, 1100],
                "成交额": [10300.0, 11440.0],
            }
        )


class FakeFallbackMasterAkShare:
    @staticmethod
    def stock_info_a_code_name() -> pd.DataFrame:
        raise ValueError("BSE response was not valid JSON")

    @staticmethod
    def stock_zh_a_spot_em() -> pd.DataFrame:
        return pd.DataFrame(
            {
                "代码": ["600519", "000001", "430047"],
                "名称": ["贵州茅台", "平安银行", "诺思兰德"],
            }
        )


def test_benchmark_provider_falls_back_and_clips_dates(tmp_path, monkeypatch) -> None:
    provider = AkShareProvider(cache_dir=tmp_path, retries=1)
    monkeypatch.setattr(provider, "_akshare", lambda: FakeAkShare())

    result = provider.get_benchmark_prices(
        "csi000300", date(2024, 1, 2), date(2024, 1, 3)
    )

    assert result["date"].tolist() == [pd.Timestamp("2024-01-02"), pd.Timestamp("2024-01-03")]
    assert result["symbol"].unique().tolist() == ["csi000300"]
    assert result["source"].unique().tolist() == ["akshare:stock_zh_index_daily"]


def test_stock_provider_falls_back_to_sina_daily(tmp_path, monkeypatch) -> None:
    provider = AkShareProvider(cache_dir=tmp_path, retries=1)
    monkeypatch.setattr(provider, "_akshare", lambda: FakeAkShare())

    result = provider.get_stock_prices(
        "601318", date(2024, 1, 2), date(2024, 1, 3), "hfq"
    )

    assert result["date"].tolist() == [pd.Timestamp("2024-01-02"), pd.Timestamp("2024-01-03")]
    assert result["symbol"].unique().tolist() == ["601318"]
    assert result["source"].unique().tolist() == ["akshare:stock_zh_a_daily:hfq"]


def test_sync_provider_normalizes_master_calendar_and_full_daily_bars(
    tmp_path, monkeypatch
) -> None:
    provider = AkShareProvider(cache_dir=tmp_path, retries=1)
    monkeypatch.setattr(provider, "_akshare", lambda: FakeSyncAkShare())

    master = provider.get_security_master()
    cached_master = provider.get_security_master()
    calendar = provider.get_trading_calendar()
    bars = provider.get_daily_bars(
        "600519", date(2024, 1, 2), date(2024, 1, 3), "none"
    )

    assert master["security_id"].tolist() == [
        "SSE.600519",
        "SZSE.000001",
        "BSE.430047",
    ]
    assert cached_master["symbol"].tolist() == ["600519", "000001", "430047"]
    assert len(calendar) == 6
    assert calendar["exchange"].unique().tolist() == ["SSE", "SZSE", "BSE"]
    assert bars.columns.tolist() == [
        "trade_date",
        "open",
        "high",
        "low",
        "close",
        "volume",
        "amount",
        "amount_missing",
        "symbol",
        "source",
    ]
    assert bars["amount_missing"].tolist() == [False, False]


def test_security_master_falls_back_when_composite_listing_fails(
    tmp_path, monkeypatch
) -> None:
    provider = AkShareProvider(cache_dir=tmp_path, retries=1)
    monkeypatch.setattr(provider, "_akshare", lambda: FakeFallbackMasterAkShare())

    master = provider.get_security_master()

    assert master["security_id"].tolist() == [
        "SSE.600519",
        "SZSE.000001",
        "BSE.430047",
    ]
    assert master["symbol"].tolist() == ["600519", "000001", "430047"]
    assert master["source"].unique().tolist() == ["akshare:stock_zh_a_spot_em"]
