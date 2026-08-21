from __future__ import annotations

import base64
import hashlib
import hmac
import json
import time
from collections import defaultdict
from threading import Lock
from typing import Any

from ai_quant_assistant.config import Settings

SESSION_COOKIE = "ai_quant_session"


class WebAuthenticator:
    def __init__(self, settings: Settings) -> None:
        self.settings = settings
        self.enabled = bool(settings.web_password)
        if self.enabled and not settings.web_session_secret:
            raise ValueError("AI_QUANT_WEB_SESSION_SECRET is required when Web login is enabled")
        if settings.web_session_ttl_seconds < 60:
            raise ValueError("web session lifetime must be at least 60 seconds")
        self.attempts: dict[str, list[float]] = defaultdict(list)
        self.lock = Lock()

    def verify_credentials(self, username: str, password: str) -> bool:
        valid_user = hmac.compare_digest(username.encode(), self.settings.web_username.encode())
        valid_password = hmac.compare_digest(password.encode(), self.settings.web_password.encode())
        return valid_user and valid_password

    def allow_attempt(self, remote_host: str) -> bool:
        now = time.monotonic()
        with self.lock:
            recent = [item for item in self.attempts[remote_host] if now - item < 300]
            self.attempts[remote_host] = recent
            if len(recent) >= 5:
                return False
            recent.append(now)
            return True

    def clear_attempts(self, remote_host: str) -> None:
        with self.lock:
            self.attempts.pop(remote_host, None)

    def create_session(self) -> str:
        payload = {
            "username": self.settings.web_username,
            "issued_at": int(time.time()),
        }
        message = base64.urlsafe_b64encode(
            json.dumps(payload, sort_keys=True, separators=(",", ":")).encode()
        ).decode().rstrip("=")
        signature = self._sign(message)
        return f"{message}.{signature}"

    def valid_session(self, token: str | None) -> bool:
        if not self.enabled:
            return True
        if not token or token.count(".") != 1:
            return False
        message, signature = token.split(".", maxsplit=1)
        if not hmac.compare_digest(signature, self._sign(message)):
            return False
        try:
            padding = "=" * (-len(message) % 4)
            payload: dict[str, Any] = json.loads(base64.urlsafe_b64decode(message + padding))
            issued_at = int(payload["issued_at"])
        except (KeyError, TypeError, ValueError, json.JSONDecodeError):
            return False
        if not hmac.compare_digest(str(payload.get("username", "")), self.settings.web_username):
            return False
        age = time.time() - issued_at
        return 0 <= age <= self.settings.web_session_ttl_seconds

    def _sign(self, message: str) -> str:
        return hmac.new(
            self.settings.web_session_secret.encode(),
            message.encode(),
            hashlib.sha256,
        ).hexdigest()


def safe_next_path(value: str | None) -> str:
    if value and value.startswith("/") and not value.startswith("//") and "\\" not in value:
        return value
    return "/"
