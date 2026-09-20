import json
from datetime import date

import pandas as pd
import pytest
from curl_cffi import requests

from ai_quant_assistant.errors import ProviderError
from ai_quant_assistant.providers.akshare_provider import AkShareProvider
from ai_quant_assistant.providers.cninfo import (
    AnnouncementResponseError,
    CninfoAnnouncements,
    announcement_error,
)


class Response:
    def __init__(self, data=None, *, body=None, status=200):
        self.body = body if body is not None else json.dumps(data).encode()
        self.status_code = status
        self.closed = False

    def __enter__(self):
        return self

    def __exit__(self, *args):
        self.closed = True

    def close(self):
        self.closed = True

    def iter_content(self, chunk_size):
        for start in range(0, len(self.body), chunk_size):
            yield self.body[start:start + chunk_size]


class Session:
    def __init__(self, responses):
        self.responses = iter(responses)
        self.headers = {}
        self.calls = []

    def __enter__(self):
        return self

    def __exit__(self, *args):
        pass

    def request(self, method, url, **kwargs):
        self.calls.append((method, url, kwargs))
        response = next(self.responses)
        if isinstance(response, Exception):
            raise response
        return response


def catalog():
    return Response({"stockList": [{"code": "688111", "orgId": "9900038384"}]})


def item(identity=1, published="2024-08-15 18:45:00"):
    return {
        "secCode": "688111", "secName": "测试公司", "announcementTitle": "2024年半年度报告",
        "announcementTime": int(pd.Timestamp(published, tz="Asia/Shanghai").timestamp() * 1000),
        "announcementId": str(identity), "orgId": "9900038384",
    }


def fetch(client):
    return client.fetch(symbol="688111", market="沪深京", category="半年报",
                        start_date="20240101", end_date="20241231")


@pytest.mark.parametrize("published", ["2024-08-15 18:45:00", "2024-08-15 00:00:00"])
def test_https_transport_matches_original_format_and_preserves_identity(monkeypatch, published):
    from akshare.stock_feature.stock_disclosure_cninfo import __format_announcement_df

    raw = [item(published=published)]
    catalog_response = catalog()
    response = Response({"totalAnnouncement": 1, "announcements": raw})
    session = Session([catalog_response, response])
    session_options = {}

    def session_factory(**kwargs):
        session_options.update(kwargs)
        return session

    monkeypatch.setattr(requests, "Session", session_factory)
    result = fetch(CninfoAnnouncements(timeout_seconds=15))
    pd.testing.assert_frame_equal(result, __format_announcement_df(pd.DataFrame(raw)))
    assert catalog_response.closed and response.closed
    assert session_options == {
        "impersonate": "chrome",
        "headers": {"Accept": "application/json", "Referer": "https://www.cninfo.com.cn/"},
    }
    assert len(session.calls) == 2  # No duplicate first-page fetch.
    for _, url, kwargs in session.calls:
        assert url.startswith("https://www.cninfo.com.cn/")
        assert kwargs["timeout"] == (10.0, 15.0)
        assert kwargs["allow_redirects"] is False and kwargs["stream"] is True
    assert session.calls[-1][2]["data"]["category"] == "category_bndbg_szsh"
    assert session.calls[-1][2]["data"]["stock"] == "688111,9900038384"


@pytest.mark.parametrize("items", [None, []])
@pytest.mark.parametrize("total", [0, "0"])
def test_only_verified_empty_response_is_success(monkeypatch, items, total):
    session = Session([catalog(), Response({"totalAnnouncement": total, "announcements": items})])
    monkeypatch.setattr(requests, "Session", lambda **_: session)
    assert fetch(CninfoAnnouncements()).empty


@pytest.mark.parametrize("response,reason", [
    (Response(body=b""), "empty"),
    (Response(body=b"<html>secret upstream token</html>"), "json"),
    (Response([]), "schema"),
    (Response({}), "schema"),
    (Response({"totalAnnouncement": 0}), "schema"),
    (Response({"totalAnnouncement": 1, "announcements": []}), "pagination"),
    (Response({"totalAnnouncement": 1, "announcements": [dict(item(), secCode="000001")]}), "schema"),
    (Response(status=302), "redirect"),
    (Response(status=403), "http"),
    (Response(status=429), "http"),
    (Response(status=503), "http"),
    (requests.exceptions.Timeout("secret upstream token"), "network"),
])
def test_bad_response_is_actionable_failure_not_empty_success(monkeypatch, response, reason):
    session = Session([catalog(), response])
    monkeypatch.setattr(requests, "Session", lambda **_: session)
    with pytest.raises(AnnouncementResponseError) as error:
        fetch(CninfoAnnouncements())
    assert error.value.reason == reason
    assert "secret upstream token" not in str(error.value)
    assert "请检查服务器" in str(error.value)
    assert len(session.calls) == 2
    if not isinstance(response, Exception):
        assert response.closed


def test_size_page_limits_and_later_page_failure_are_not_partial_success(monkeypatch):
    session = Session([catalog(), Response({"totalAnnouncement": 31, "announcements": [item(i) for i in range(30)]}), Response(body=b"not json")])
    monkeypatch.setattr(requests, "Session", lambda **_: session)
    monkeypatch.setattr("ai_quant_assistant.providers.cninfo.time.sleep", lambda _: None)
    with pytest.raises(AnnouncementResponseError, match="JSON"):
        fetch(CninfoAnnouncements())
    assert session.calls[-1][2]["data"]["pageNum"] == "2"
    session = Session([catalog(), Response({"totalAnnouncement": 31, "announcements": [item(i) for i in range(30)]})])
    client = CninfoAnnouncements()
    client.max_pages = 1
    with pytest.raises(AnnouncementResponseError) as error:
        fetch(client)
    assert error.value.reason == "limit"
    client = CninfoAnnouncements()
    client.max_bytes = 10
    session = Session([catalog()])
    with pytest.raises(AnnouncementResponseError) as error:
        fetch(client)
    assert error.value.reason == "limit"


def test_pagination_rejects_duplicate_and_changed_total(monkeypatch):
    monkeypatch.setattr("ai_quant_assistant.providers.cninfo.time.sleep", lambda _: None)
    for second in ({"totalAnnouncement": 31, "announcements": [item(0)]},
                   {"totalAnnouncement": 32, "announcements": [item(30), item(31)]}):
        session = Session([catalog(), Response({"totalAnnouncement": 31, "announcements": [item(i) for i in range(30)]}), Response(second)])
        monkeypatch.setattr(requests, "Session", lambda session=session, **_: session)
        with pytest.raises(AnnouncementResponseError) as error:
            fetch(CninfoAnnouncements())
        assert error.value.reason == "pagination"


def test_rate_limit_not_retried_or_cached_as_empty(tmp_path, monkeypatch):
    session = Session([catalog(), Response(status=429)])
    monkeypatch.setattr(requests, "Session", lambda **_: session)
    monkeypatch.setattr("ai_quant_assistant.providers.cache.time.sleep", lambda _: pytest.fail("429 must not hammer upstream"))
    provider = AkShareProvider(tmp_path, retries=3, refresh=True)
    with pytest.raises(ProviderError, match="after 1 attempts") as error:
        provider.get_report_announcements("688111", date(2024, 1, 1), date(2024, 12, 31), "interim")
    assert announcement_error(error.value).stop_batch
    assert not list(tmp_path.rglob("*.csv.gz"))


def test_provider_preserves_publication_semantics_and_uses_same_transport_for_interim(tmp_path, monkeypatch):
    session = Session([catalog(), Response({"totalAnnouncement": 1, "announcements": [item()]}),
                       Response({"totalAnnouncement": 1, "announcements": [item()]})])
    monkeypatch.setattr(requests, "Session", lambda **_: session)
    provider = AkShareProvider(tmp_path, retries=1)
    reports = provider.get_report_announcements("688111", date(2024, 1, 1), date(2024, 12, 31), "interim")
    interim = provider.get_interim_announcements("688111", date(2024, 1, 1), date(2024, 12, 31))
    assert reports.iloc[0]["published_at"] == pd.Timestamp("2024-08-15 18:45:00")
    assert reports.iloc[0]["report_period_end"] == pd.Timestamp("2024-06-30")
    assert reports.iloc[0]["source"] == "akshare:stock_zh_a_disclosure_report_cninfo:interim"
    assert reports.iloc[0]["url"] == interim.iloc[0]["url"]
