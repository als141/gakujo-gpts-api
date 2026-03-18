"""新潟大学 学務情報システム GPTs API サーバー。

CampusSquare (学務情報システム) をスクレイピングし、
GPTs Custom Actions 向けの構造化 JSON API を提供する。

認証: OAuth 2.0 Authorization Code Grant
"""

import asyncio
import logging
from contextlib import asynccontextmanager

import uvicorn
from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from starlette.middleware.trustedhost import TrustedHostMiddleware

from app.config import settings
from app.load_control import get_http_request_semaphore
from app.oauth import _session_cache, oauth_router
from app.routes import router

# ロギング設定
logging.basicConfig(
    level=logging.DEBUG if settings.debug else logging.WARNING,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
)


@asynccontextmanager
async def lifespan(app: FastAPI):
    """アプリケーションのライフサイクル管理。"""
    yield
    # シャットダウン時にキャッシュ済みセッションをクリーンアップ
    for key, sess in list(_session_cache.items()):
        try:
            await sess["client"].close()
        except Exception:
            pass
    _session_cache.clear()


app = FastAPI(
    title="新潟大学 学務情報システム API",
    description=(
        "新潟大学の学務情報システム (CampusSquare) から"
        "時間割、休講情報、成績、レポート、連絡通知などを取得する"
        "GPTs Custom Actions 向け REST API です。\n\n"
        "## 認証\n"
        "OAuth 2.0 Authorization Code Grant を使用します。\n"
        "1. GPTs が `/oauth/authorize` にリダイレクト → ログインフォーム表示\n"
        "2. ユーザーがCampusSquareの認証情報を入力\n"
        "3. 認可コード → アクセストークンに交換\n"
        "4. 以降のAPIコールは `Authorization: Bearer` ヘッダーで認証\n\n"
        "## データ取得\n"
        "各エンドポイントを呼び出すだけで学務データを取得できます。"
    ),
    version="1.0.0",
    lifespan=lifespan,
    servers=(
        [{"url": settings.server_url, "description": "API サーバー"}]
        if settings.server_url
        else []
    ),
)

if settings.allowed_hosts:
    app.add_middleware(TrustedHostMiddleware, allowed_hosts=settings.allowed_hosts)

if settings.cors_allow_origins:
    app.add_middleware(
        CORSMiddleware,
        allow_origins=settings.cors_allow_origins,
        allow_methods=["GET", "POST", "OPTIONS"],
        allow_headers=["Authorization", "Content-Type"],
    )

# OAuthエンドポイント (トップレベル、OpenAPIスキーマには含めない)
app.include_router(oauth_router, include_in_schema=False)

# データ取得エンドポイント
app.include_router(router, prefix="/api/v1")


@app.middleware("http")
async def apply_security_headers(request, call_next):
    semaphore = None
    acquired = False
    if request.url.path.startswith(("/api/v1", "/oauth/")):
        semaphore = get_http_request_semaphore()
        try:
            await asyncio.wait_for(
                semaphore.acquire(),
                timeout=settings.active_http_request_acquire_timeout_seconds,
            )
            acquired = True
        except TimeoutError:
            return JSONResponse(
                status_code=503,
                content={
                    "error": "server_busy",
                    "detail": "リクエストが集中しています。少し待って再試行してください。",
                },
                headers={"Retry-After": "10"},
            )

    try:
        response = await call_next(request)

        response.headers.setdefault("Cache-Control", "no-store")
        response.headers.setdefault("Pragma", "no-cache")
        response.headers.setdefault("Referrer-Policy", "no-referrer")
        response.headers.setdefault("X-Content-Type-Options", "nosniff")
        response.headers.setdefault("X-Frame-Options", "DENY")
        response.headers.setdefault(
            "Permissions-Policy", "camera=(), microphone=(), geolocation=()"
        )

        content_type = response.headers.get("content-type", "").lower()
        if "text/html" in content_type:
            response.headers.setdefault(
                "Content-Security-Policy",
                "default-src 'self'; style-src 'self' 'unsafe-inline'; "
                "img-src 'self' data:; form-action 'self'; base-uri 'none'; "
                "frame-ancestors 'none'; object-src 'none'",
            )
        else:
            response.headers.setdefault(
                "Content-Security-Policy",
                "default-src 'none'; frame-ancestors 'none'; base-uri 'none'; "
                "form-action 'none'",
            )

        forwarded_proto = request.headers.get("x-forwarded-proto", request.url.scheme)
        if forwarded_proto == "https":
            response.headers.setdefault(
                "Strict-Transport-Security", "max-age=31536000; includeSubDomains"
            )

        return response
    finally:
        if acquired and semaphore is not None:
            semaphore.release()


# プライバシーポリシー (GPTs Custom Actions 必須)
@app.get("/privacy", include_in_schema=False)
async def privacy_policy():
    from fastapi.responses import HTMLResponse

    return HTMLResponse("""<!DOCTYPE html>
<html lang="ja"><head><meta charset="UTF-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>プライバシーポリシー - 新大学務AIアシスタント</title>
<style>body{font-family:sans-serif;max-width:700px;margin:40px auto;padding:0 20px;line-height:1.8;color:#333}
h1{font-size:22px}h2{font-size:17px;margin-top:28px}p{margin:8px 0}</style></head><body>
<h1>プライバシーポリシー</h1>
<p>最終更新日: 2026年3月17日</p>

<h2>1. サービス概要</h2>
<p>本サービス「新大学務AIアシスタント」は、新潟大学の学務情報システム（CampusSquare）と連携し、
学生の学務情報を取得・分析するGPTs Custom Actionsです。</p>

<h2>2. 収集する情報</h2>
<p>本サービスはOAuth認証を通じて、ユーザーがログインフォームに入力した学務情報システムの認証情報
（ユーザーID、パスワード、TOTPコード/シークレット）を一時的に処理します。</p>

<h2>3. 情報の利用目的</h2>
<p>収集した認証情報は、学務情報システムへのログインおよびデータ取得のためにのみ使用します。</p>

<h2>4. 情報の保存</h2>
<ul>
<li>認証情報はログイン処理完了後、即座にサーバーメモリから消去されます</li>
<li>認証情報はデータベース・ファイルシステム・トークンに保存されません</li>
<li>学務データのレスポンス本文はデフォルトでサーバー側にキャッシュ保存しません</li>
<li>ログイン後はCampusSquareのセッションCookie（JSESSIONID）のみを短時間メモリ保持し、セッション終了後またはサーバー再起動時に削除されます</li>
</ul>

<h2>5. 第三者提供</h2>
<p>ユーザーの認証情報および学務データを第三者に提供することはありません。
データはCampusSquareとの通信、およびユーザーが利用するGPTsプラットフォームへの応答にのみ使用されます。</p>

<h2>6. セキュリティ</h2>
<p>通信は全てHTTPS（TLS 1.2以上）で暗号化されています。
トークンはFernet対称暗号で保護され、アプリケーションは no-store ヘッダ・Host制限・SSRF対策・詳細エラーマスクを適用しています。</p>

<h2>7. お問い合わせ</h2>
<p>本サービスに関するお問い合わせは、GitHubリポジトリのIssuesをご利用ください。</p>
</body></html>""")


# OpenAPI スキーマに OAuth securitySchemes を追加
_base_url = settings.server_url or "https://your-server.example.com"


def custom_openapi():
    if app.openapi_schema:
        return app.openapi_schema
    from fastapi.openapi.utils import get_openapi

    schema = get_openapi(
        title=app.title,
        version=app.version,
        description=app.description,
        routes=app.routes,
        servers=list(app.servers) if app.servers else None,
    )
    schema["components"] = schema.get("components", {})
    schema["components"]["securitySchemes"] = {
        "oauth": {
            "type": "oauth2",
            "flows": {
                "authorizationCode": {
                    "authorizationUrl": f"{_base_url}/oauth/authorize",
                    "tokenUrl": f"{_base_url}/oauth/token",
                    "scopes": {"openid": "学務情報へのアクセス"},
                }
            },
        }
    }
    schema["security"] = [{"oauth": ["openid"]}]
    app.openapi_schema = schema
    return schema


app.openapi = custom_openapi


def start():
    """uvicorn起動エントリポイント。"""
    uvicorn.run(
        "main:app",
        host="0.0.0.0",
        port=8000,
        access_log=False,
        reload=settings.debug,
    )


if __name__ == "__main__":
    start()
