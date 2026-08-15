from datetime import date

import pandas as pd

from ai_quant_assistant.providers.akshare_provider import AkShareProvider


class FakeAkShare:
    @staticmethod
    def stock_zh_a_hist(**_: str) -> pd.DataFrame:
        raise ConnectionError("Eastmoney endpoint unavailable")

    @staticmethod
    def stock_zh_a_daily(symbol: str, adjust: str) -> pd.DataFrame:
        assert symbol == "sh600519"
        assert adjust == "hfq"
        return pd.DataFrame(
            {
                "date": ["2024-01-01", "2024-01-02", "2024-01-03", "2024-01-04"],
                "close": [99.0, 100.0, 101.0, 102.0],
            }
        )


def test_stock_provider_falls_back_and_clips_dates(tmp_path, monkeypatch) -> None:
    provider = AkShareProvider(cache_dir=tmp_path, retries=1)
    monkeypatch.setattr(provider, "_akshare", lambda: FakeAkShare())

    result = provider.get_stock_prices(
        "600519", date(2024, 1, 2), date(2024, 1, 3), "hfq"
    )

    assert result["date"].tolist() == [
        pd.Timestamp("2024-01-02"),
        pd.Timestamp("2024-01-03"),
    ]
    assert result["symbol"].unique().tolist() == ["600519"]
    assert result["source"].unique().tolist() == ["akshare:stock_zh_a_daily:hfq"]
