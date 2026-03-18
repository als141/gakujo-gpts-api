"""アプリケーション共通のセキュリティヘルパー。"""

import threading
import time
from collections import defaultdict, deque
from urllib.parse import urlparse

from fastapi import Request


def extract_client_ip(request: Request) -> str:
    """Cloud Run の X-Forwarded-For を優先してクライアントIPを取得。"""
    forwarded_for = request.headers.get("x-forwarded-for", "")
    if forwarded_for:
        return forwarded_for.split(",")[0].strip()
    if request.client and request.client.host:
        return request.client.host
    return "unknown"


def validate_redirect_uri(redirect_uri: str, allowed_hosts: list[str]) -> str:
    """OAuth redirect_uri を検証。"""
    if not redirect_uri:
        raise ValueError("redirect_uri が指定されていません。")

    parsed = urlparse(redirect_uri)
    host = (parsed.hostname or "").lower()
    allowed = {item.lower() for item in allowed_hosts}

    if not parsed.scheme or not host:
        raise ValueError("redirect_uri は絶対URLで指定してください。")
    if parsed.username or parsed.password:
        raise ValueError("redirect_uri に認証情報を含めることはできません。")
    if parsed.fragment:
        raise ValueError("redirect_uri にフラグメントは使用できません。")
    if host not in allowed:
        raise ValueError("許可されていない redirect_uri です。")

    if host in {"localhost", "127.0.0.1"}:
        if parsed.scheme not in {"http", "https"}:
            raise ValueError("localhost の redirect_uri は http/https のみ許可されます。")
    elif parsed.scheme != "https":
        raise ValueError("redirect_uri には HTTPS が必要です。")

    return redirect_uri


class InMemoryRateLimiter:
    """簡易なインメモリ・レートリミッタ。

    Cloud Run の複数インスタンス間では共有されないため、強固な防御は Cloud Armor 側で行う。
    それでも単一インスタンス上でのブルートフォース抑止には有効。
    """

    def __init__(self, max_attempts: int, window_seconds: int):
        self.max_attempts = max_attempts
        self.window_seconds = window_seconds
        self._events: dict[str, deque[float]] = defaultdict(deque)
        self._lock = threading.Lock()

    def allow(self, key: str) -> bool:
        if self.max_attempts <= 0 or self.window_seconds <= 0:
            return True

        now = time.time()
        with self._lock:
            events = self._events[key]
            while events and now - events[0] > self.window_seconds:
                events.popleft()
            if len(events) >= self.max_attempts:
                return False
            events.append(now)
            return True
