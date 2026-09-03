from __future__ import annotations

import json
from datetime import date
from http.client import RemoteDisconnected
from io import BytesIO
from types import SimpleNamespace
from urllib.parse import parse_qs, urlparse

import pandas as pd
import pytest

from ai_quant_assistant.errors import ProviderError
from ai_quant_assistant.providers import listing_dates
from ai_quant_assistant.providers.akshare_provider import AkShareProvider
from ai_quant_assistant.providers.listing_dates import (
    SSE_LISTING_SOURCE,
    explicit_listing_date,
    sse_listing_frame,
    validate_listing_frame,
)


def evidence(symbol="688111", listed="20191118"):
    return pd.DataFrame({"item": ["股票代码", "上市时间"], "value": [symbol, listed]})


@pytest.fixture
def endpoints(tmp_path, monkeypatch):
    calls = []

    def primary(**kwargs):
        calls.append(("em", kwargs))
        raise RemoteDisconnected("controlled disconnect; never use real internet")

    def secondary(request, timeout):
        calls.append(("sse", request, timeout))
        return BytesIO(json.dumps({"result": [{
            "A_STOCK_CODE": "688111", "LIST_DATE": "2019-11-18",
        }]}).encode())

    ak = SimpleNamespace(stock_individual_info_em=primary)
    opener = SimpleNamespace(open=secondary)
    monkeypatch.setattr(AkShareProvider, "_akshare", staticmethod(lambda: ak))
    monkeypatch.setattr(listing_dates, "build_opener", lambda *args: opener)
    monkeypatch.setattr("ai_quant_assistant.providers.cache.time.sleep", lambda _: None)
    return AkShareProvider(tmp_path, retries=3, timeout_seconds=2, refresh=True), ak, opener, calls


def test_disconnected_primary_uses_bounded_single_stock_exchange_evidence(endpoints):
    provider, _, _, calls = endpoints
    assert provider.get_listing_date("688111") == (date(2019, 11, 18), SSE_LISTING_SOURCE)
    assert [c[0] for c in calls] == ["em", "em", "em", "sse"]
    request, timeout = calls[-1][1:]
    query = parse_qs(urlparse(request.full_url).query)
    assert urlparse(request.full_url).netloc == "query.sse.com.cn"
    assert query["STOCK_CODE"] == ["688111"]
    assert query["STOCK_TYPE"] == ["8"]
    assert query["pageHelp.pageSize"] == ["2"]
    assert timeout == 2
    assert all(call[1]["timeout"] == 2 for call in calls[:3])


def test_primary_success_does_not_call_secondary_and_cache_keeps_leading_zero(endpoints):
    provider, ak, _, calls = endpoints
    provider.refresh = False
    ak.stock_individual_info_em = lambda **kwargs: evidence("003816", "20190826")
    first = provider.get_listing_date("003816")
    assert first[0] == date(2019, 8, 26)
    ak.stock_individual_info_em = lambda **kwargs: pytest.fail("use validated cache")
    assert provider.get_listing_date("003816") == first
    assert calls == []


@pytest.mark.parametrize("frame", [
    evidence("600519"),
    pd.DataFrame({"item": ["上市时间"], "value": ["20191118"]}),
    pd.DataFrame({"item": ["股票代码", "上市时间", "上市日期"],
                  "value": ["688111", "20191118", "20200101"]}),
    evidence(listed="20990101"),
    evidence(listed="invalid"),
    evidence(listed="20190230"),
])
def test_invalid_primary_is_not_cached_or_accepted_and_can_use_independent_source(endpoints, frame):
    provider, ak, _, _ = endpoints
    ak.stock_individual_info_em = lambda **kwargs: frame
    assert provider.get_listing_date("688111")[1] == SSE_LISTING_SOURCE
    assert provider.cache.get("listing_date_v2", {"symbol": "688111"}) is None


@pytest.mark.parametrize("payload", [
    {"result": []},
    {"result": [{"A_STOCK_CODE": "600519", "LIST_DATE": "2019-11-18"}]},
    {"result": [{"B_STOCK_CODE": "688111", "LIST_DATE": "2019-11-18"}]},
    {"result": [{"A_STOCK_CODE": "688111", "LIST_DATE": "2099-01-01"}]},
    {"result": [{"A_STOCK_CODE": "688111", "LIST_DATE": None}]},
    {"result": [{"A_STOCK_CODE": "688111", "LIST_DATE": "2019-11-18"}] * 2},
    {"result": "invalid"},
    [],
])
def test_exchange_mismatches_duplicates_and_missing_dates_fail_closed(endpoints, payload):
    provider, _, opener, _ = endpoints
    opener.open = lambda *args, **kwargs: BytesIO(json.dumps(payload).encode())
    with pytest.raises(ProviderError, match="来源均不可用或证据无效"):
        provider.get_listing_date("688111")
    assert provider.cache.get("listing_date_sse_v1", {"symbol": "688111"}) is None


def test_all_sources_fail_with_bounded_retries_and_safe_diagnostics(endpoints):
    provider, _, opener, calls = endpoints

    def fail(*args, **kwargs):
        calls.append(("sse_failure",))
        raise TimeoutError("https://user:secret@example.invalid/?token=private")

    opener.open = fail
    with pytest.raises(ProviderError) as raised:
        provider.get_listing_date("688111")
    message = str(raised.value)
    assert "RemoteDisconnected" in message and "TimeoutError" in message
    assert "stock_individual_info_em" in message and SSE_LISTING_SOURCE in message
    assert "secret" not in message and "token" not in message and "private" not in message
    assert [c[0] for c in calls].count("em") == 3
    assert [c[0] for c in calls].count("sse_failure") == 3


def test_non_shanghai_never_uses_wrong_exchange_fallback(endpoints):
    provider, _, _, calls = endpoints
    with pytest.raises(ProviderError):
        provider.get_listing_date("003816")
    assert {call[0] for call in calls} == {"em"}


def test_refresh_does_not_trust_stale_listing_cache(endpoints):
    provider, _, opener, _ = endpoints
    provider.cache.put("listing_date_v2", {"symbol": "688111"}, pd.DataFrame({
        "symbol": ["688111"], "listing_date": ["2019-11-18"],
    }))
    opener.open = lambda *args, **kwargs: BytesIO(b'{"result": []}')
    with pytest.raises(ProviderError):
        provider.get_listing_date("688111")


def test_response_size_and_redirects_are_bounded(endpoints):
    _, _, opener, _ = endpoints
    opener.open = lambda *args, **kwargs: BytesIO(b"x" * (128 * 1024 + 1))
    with pytest.raises(ProviderError, match="size limit"):
        sse_listing_frame("688111", 2)
    with pytest.raises(ProviderError, match="redirected"):
        listing_dates._NoRedirect().redirect_request(None, None, 302, "", {}, "http://127.0.0.1")


@pytest.mark.parametrize("value", ["20191118", "20191118.0", "2019-11-18", date(2019, 11, 18)])
def test_explicit_date_formats(value):
    assert explicit_listing_date(value) == date(2019, 11, 18)


def test_missing_identity_never_inferred_from_requested_symbol():
    with pytest.raises(ProviderError, match="identity"):
        validate_listing_frame(pd.DataFrame({"item": ["上市日期"], "value": ["2019-11-18"]}), "688111")
