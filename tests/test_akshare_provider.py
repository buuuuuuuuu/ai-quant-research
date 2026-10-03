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

    @staticmethod
    def stock_zh_a_disclosure_report_cninfo(**kwargs) -> pd.DataFrame:
        quarterly = {
            "一季报": ("第一季度", "2024-04-20 18:30:00"),
            "三季报": ("第三季度", "2024-10-20 19:10:00"),
        }
        if kwargs["category"] in quarterly:
            quarter, published_at = quarterly[kwargs["category"]]
            return pd.DataFrame(
                {
                    "代码": ["600519"],
                    "简称": ["贵州茅台"],
                    "公告标题": [f"贵州茅台2024年{quarter}报告"],
                    "公告时间": [published_at],
                    "公告链接": [f"https://example.test/{quarter}"],
                }
            )
        if kwargs["category"] == "半年报":
            return pd.DataFrame(
                {
                    "代码": ["600519"],
                    "简称": ["贵州茅台"],
                    "公告标题": ["贵州茅台2024年半年度报告"],
                    "公告时间": ["2024-08-15 18:45:00"],
                    "公告链接": ["https://example.test/interim"],
                }
            )
        return pd.DataFrame(
            {
                "代码": ["600519", "600519"],
                "简称": ["贵州茅台", "贵州茅台"],
                "公告标题": ["贵州茅台2024年年度报告", "贵州茅台2024年半年度报告"],
                "公告时间": ["2025-03-10 20:15:00", "2025-03-11 08:00:00"],
                "公告链接": ["https://example.test/annual", "https://example.test/interim"],
            }
        )

    @staticmethod
    def stock_financial_analysis_indicator_em(**kwargs) -> pd.DataFrame:
        assert kwargs == {"symbol": "600519.SH", "indicator": "按报告期"}
        return pd.DataFrame(
            {
                "REPORT_DATE": ["2024-03-31", "2024-06-30"],
                "SECURITY_NAME_ABBR": ["贵州茅台", "贵州茅台"],
                "NOTICE_DATE": ["2024-04-20 18:30:00", "2024-08-15 18:45:00"],
                "TOTALOPERATEREVE": [100.0, 250.0],
                "PARENTNETPROFIT": [30.0, 80.0],
                "EPSJB": [0.3, 0.8],
                "ROEJQ": [12.0, 18.0],
                "JYXJLYYSR": [0.22, 0.31],
            }
        )


class FakeFinancialFallbackAkShare:
    @staticmethod
    def stock_financial_analysis_indicator_em(**kwargs) -> pd.DataFrame:
        assert kwargs["symbol"] == "000001.SZ"
        raise ConnectionError("EastMoney financial endpoint disconnected")

    @staticmethod
    def stock_financial_analysis_indicator(**kwargs) -> pd.DataFrame:
        assert kwargs == {"symbol": "000001", "start_year": "2024"}
        return pd.DataFrame(
            {
                "日期": ["2024-06-30"],
                "营业收入(元)": [900.0],
                "净利润(元)": [120.0],
                "经营现金净流量对销售收入比率(%)": [25.0],
            }
        )


def test_official_suspension_evidence_precedes_third_party(tmp_path, monkeypatch):
    provider = AkShareProvider(tmp_path, retries=1, refresh=True)

    class Official:
        @staticmethod
        def verify(symbol, dates):
            assert symbol == "000333"
            assert dates == (date(2018, 9, 10), date(2018, 10, 30))
            return {
                "verified": [
                    {
                        "date": "2018-09-10",
                        "classification": "verified_suspension",
                        "source": "cninfo:official_announcement_chain",
                    }
                ],
                "errors": [],
                "evidence_chains": [{"suspension_start": "2018-09-10"}],
                "source": "cninfo:official_announcement_chain",
                "candidate_count": 2,
                "parsed_count": 2,
            }

    class ThirdParty:
        calls = []

        @classmethod
        def stock_tfp_em(cls, *, date):
            cls.calls.append(date)
            return pd.DataFrame(
                {
                    "代码": ["000333"],
                    "停牌时间": ["2018-10-30"],
                    "停牌截止时间": ["2018-10-30"],
                    "停牌原因": ["结构化停牌记录"],
                }
            )

    provider._official_suspensions = Official()
    monkeypatch.setattr(provider, "_akshare", lambda: ThirdParty)

    result = provider.get_suspension_evidence(
        "000333", (date(2018, 9, 10), date(2018, 10, 30))
    )

    assert {item["date"] for item in result["verified"]} == {
        "2018-09-10",
        "2018-10-30",
    }
    assert ThirdParty.calls == ["20181030"]
    assert result["evidence_chains"] == [{"suspension_start": "2018-09-10"}]


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

    result = provider.get_benchmark_prices("csi000300", date(2024, 1, 2), date(2024, 1, 3))

    assert result["date"].tolist() == [pd.Timestamp("2024-01-02"), pd.Timestamp("2024-01-03")]
    assert result["symbol"].unique().tolist() == ["csi000300"]
    assert result["source"].unique().tolist() == ["akshare:stock_zh_index_daily"]


def test_stock_provider_falls_back_to_sina_daily(tmp_path, monkeypatch) -> None:
    provider = AkShareProvider(cache_dir=tmp_path, retries=1)
    monkeypatch.setattr(provider, "_akshare", lambda: FakeAkShare())

    result = provider.get_stock_prices("601318", date(2024, 1, 2), date(2024, 1, 3), "hfq")

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
    bars = provider.get_daily_bars("600519", date(2024, 1, 2), date(2024, 1, 3), "none")

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


def test_security_master_falls_back_when_composite_listing_fails(tmp_path, monkeypatch) -> None:
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


def test_report_announcements_preserve_period_and_actual_publication(tmp_path, monkeypatch) -> None:
    provider = AkShareProvider(cache_dir=tmp_path, retries=1)
    monkeypatch.setattr(provider, "_akshare", lambda: FakeSyncAkShare())
    monkeypatch.setattr(provider, "_fetch_cninfo_announcements", FakeSyncAkShare().stock_zh_a_disclosure_report_cninfo)

    interim = provider.get_report_announcements(
        "600519", date(2024, 1, 1), date(2024, 12, 31), "interim"
    )
    annual = provider.get_report_announcements(
        "600519", date(2025, 1, 1), date(2025, 12, 31), "annual"
    )

    assert interim["report_period_end"].tolist() == [pd.Timestamp("2024-06-30")]
    assert interim["published_at"].tolist() == [pd.Timestamp("2024-08-15 18:45:00")]
    assert interim["source"].tolist() == ["akshare:stock_zh_a_disclosure_report_cninfo:interim"]
    assert annual["report_year"].tolist() == [2024]
    assert annual["report_period_end"].tolist() == [pd.Timestamp("2024-12-31")]
    assert annual["published_at"].tolist() == [pd.Timestamp("2025-03-10 20:15:00")]


def test_quarterly_announcements_preserve_report_period_and_publication_time(
    tmp_path, monkeypatch
) -> None:
    provider = AkShareProvider(cache_dir=tmp_path, retries=1)
    monkeypatch.setattr(provider, "_akshare", lambda: FakeSyncAkShare())
    monkeypatch.setattr(provider, "_fetch_cninfo_announcements", FakeSyncAkShare().stock_zh_a_disclosure_report_cninfo)

    q1 = provider.get_report_announcements("600519", date(2024, 1, 1), date(2024, 12, 31), "q1")
    q3 = provider.get_report_announcements("600519", date(2024, 1, 1), date(2024, 12, 31), "q3")

    assert q1["report_period_end"].tolist() == [pd.Timestamp("2024-03-31")]
    assert q1["published_at"].tolist() == [pd.Timestamp("2024-04-20 18:30:00")]
    assert q3["report_period_end"].tolist() == [pd.Timestamp("2024-09-30")]
    assert q3["published_at"].tolist() == [pd.Timestamp("2024-10-20 19:10:00")]


def test_financial_provider_normalizes_quarterly_metrics_and_publication_times(
    tmp_path, monkeypatch
) -> None:
    provider = AkShareProvider(cache_dir=tmp_path, retries=1)
    monkeypatch.setattr(provider, "_akshare", lambda: FakeSyncAkShare())

    result = provider.get_financial_indicators("600519", date(2024, 1, 1), date(2024, 12, 31))

    assert result["report_type"].tolist() == ["q1", "interim"]
    assert result["published_at"].tolist() == [
        pd.Timestamp("2024-04-20 18:30:00"),
        pd.Timestamp("2024-08-15 18:45:00"),
    ]
    assert result["revenue"].tolist() == [100.0, 250.0]
    assert result["operating_cash_flow_to_revenue"].tolist() == [0.22, 0.31]


def test_financial_provider_fallback_converts_percentage_to_ratio(tmp_path, monkeypatch) -> None:
    provider = AkShareProvider(cache_dir=tmp_path, retries=1)
    monkeypatch.setattr(provider, "_akshare", lambda: FakeFinancialFallbackAkShare())

    result = provider.get_financial_indicators("000001", date(2024, 1, 1), date(2024, 12, 31))

    assert result["source"].tolist() == ["akshare:stock_financial_analysis_indicator"]
    assert result["revenue"].tolist() == [900.0]
    assert result["operating_cash_flow_to_revenue"].tolist() == [0.25]
    assert result["published_at"].isna().all()
