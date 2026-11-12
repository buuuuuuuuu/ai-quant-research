import re
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from test_platform_operations import _settings

from ai_quant_assistant.api.app import create_app
from ai_quant_assistant.api.auth import WebAuthenticator


@pytest.mark.parametrize('status', [200, 401, 429])
def test_all_login_responses_use_content_hash(tmp_path, monkeypatch, status):
    settings = _settings(tmp_path, web_password='test-password', web_session_secret='test-secret')
    if status == 429:
        monkeypatch.setattr(WebAuthenticator, 'allow_attempt', lambda *_: False)
    with TestClient(create_app(settings)) as client:
        page = client.get('/login') if status == 200 else client.post('/login', data={'password': 'wrong'})
        assert page.status_code == status
        hashes = re.findall(r'/assets/[^"?]+\?v=([0-9a-f]{16})"', page.text)
        assert len(hashes) == 2
        assert hashes[0] == hashes[1]
        assert hashes[0] == page.context['asset_version']


def test_css_change_invalidates_login_assets_without_package_version_change(tmp_path, monkeypatch):
    settings = _settings(tmp_path, web_password='test-password', web_session_secret='test-secret')
    with TestClient(create_app(settings)) as client:
        before = client.get('/login').context
    original = Path.read_bytes

    def changed(path):
        data = original(path)
        return data + b'/* updated stylesheet */' if path.name == 'ui-foundation.css' else data

    monkeypatch.setattr(Path, 'read_bytes', changed)
    with TestClient(create_app(settings)) as client:
        after = client.get('/login').context
    assert before['version'] == after['version']
    assert before['asset_version'] != after['asset_version']
