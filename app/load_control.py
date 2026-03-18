"""アプリケーション全体の負荷制御。"""

import asyncio

from app.config import settings

_http_request_semaphore: asyncio.Semaphore | None = None


def get_http_request_semaphore() -> asyncio.Semaphore:
    global _http_request_semaphore
    if _http_request_semaphore is None:
        _http_request_semaphore = asyncio.Semaphore(
            max(1, settings.max_active_http_requests)
        )
    return _http_request_semaphore
