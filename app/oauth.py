"""OAuth 2.0 Authorization Server for GPTs Custom Actions.

GPTs の OAuth Authorization Code Grant フローに対応した認可サーバー。
ユーザーの CampusSquare 認証情報はサーバーの .env に保存せず、
OAuth ログインフォームで都度入力させる。

Cloud Run 対応:
  - access_token / refresh_token は Fernet で暗号化
  - トークンに認証情報は含めない
  - 認可リクエストは短命のサーバー側状態に束縛
  - ウォームコンテナでは JSESSIONID のみメモリ保持
"""

import base64
import hashlib
import json
import logging
import secrets
import time
from html import escape
from typing import Annotated
from urllib.parse import urlencode

from cryptography.fernet import Fernet, InvalidToken
from fastapi import APIRouter, Form, HTTPException, Query, Request
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse

from app.client import CampusSquareClient
from app.config import settings
from app.scraper import CampusSquareScraper
from app.security import InMemoryRateLimiter, extract_client_ip, validate_redirect_uri

logger = logging.getLogger(__name__)

oauth_router = APIRouter(tags=["OAuth"])

# ─── 暗号化 ──────────────────────────────────────────

_fernet: Fernet | None = None


def _get_fernet() -> Fernet:
    """Fernet インスタンスを取得 (遅延初期化)。"""
    global _fernet
    if _fernet is None:
        key = settings.token_secret
        if not key:
            # 未設定の場合は自動生成 (コンテナ再起動で変わるが、リフレッシュで再発行)
            key = Fernet.generate_key().decode()
            logger.warning(
                "TOKEN_SECRET 未設定。自動生成しました (再起動で無効化されます)"
            )
        else:
            # 任意の文字列を Fernet キーに変換 (32バイト base64url)
            raw = hashlib.sha256(key.encode()).digest()
            key = base64.urlsafe_b64encode(raw).decode()
        _fernet = Fernet(key)
    return _fernet


def _encrypt_token(payload: dict) -> str:
    """ペイロードを暗号化してトークン文字列を返す。"""
    data = json.dumps(payload).encode()
    return _get_fernet().encrypt(data).decode()


def _decrypt_token(token: str) -> dict | None:
    """トークンを復号してペイロードを返す。失敗時は None。"""
    try:
        data = _get_fernet().decrypt(token.encode())
        return json.loads(data)
    except (InvalidToken, json.JSONDecodeError):
        return None


# ─── セッションキャッシュ (ウォームコンテナ用) ──────────

SESSION_EXPIRY = 3600  # access_token 有効期限: 1時間
AUTH_CODE_EXPIRY = 300  # 認可コード有効期限: 5分
AUTH_REQUEST_EXPIRY = 600  # 認可リクエスト有効期限: 10分
CACHE_MAX_AGE = 1200  # キャッシュ最大保持: 20分

# 認可リクエスト (短命)
_auth_requests: dict[str, dict] = {}

# 認可コード (短命)
_auth_codes: dict[str, dict] = {}

# メモリキャッシュ: トークンハッシュ → {client, scraper, last_used}
_session_cache: dict[str, dict] = {}

_oauth_form_limiter = InMemoryRateLimiter(
    settings.oauth_form_rate_limit_max_attempts,
    settings.oauth_form_rate_limit_window_seconds,
)
_token_limiter = InMemoryRateLimiter(
    settings.token_rate_limit_max_attempts,
    settings.token_rate_limit_window_seconds,
)


def _cache_key(token: str) -> str:
    return hashlib.sha256(token.encode()).hexdigest()[:16]


async def _close_client_safely(client: CampusSquareClient | None) -> None:
    if client is None:
        return
    try:
        await client.close()
    except Exception:
        pass


async def _cleanup_expired_state() -> None:
    """古い認可状態とセッションキャッシュを削除。"""
    now = time.time()
    expired_sessions = [
        k for k, v in _session_cache.items() if now - v["last_used"] > CACHE_MAX_AGE
    ]
    for k in expired_sessions:
        sess = _session_cache.pop(k, None)
        if sess:
            await _close_client_safely(sess.get("client"))

    expired_codes = [c for c, d in _auth_codes.items() if d["expires_at"] < now]
    for c in expired_codes:
        code_data = _auth_codes.pop(c, None)
        if code_data:
            await _close_client_safely(code_data.get("client"))

    expired_requests = [
        request_id for request_id, data in _auth_requests.items() if data["expires_at"] < now
    ]
    for request_id in expired_requests:
        _auth_requests.pop(request_id, None)


async def _enforce_state_limits() -> None:
    while len(_auth_requests) > settings.max_auth_state_entries:
        oldest_request_id = min(
            _auth_requests,
            key=lambda key: _auth_requests[key]["expires_at"],
        )
        _auth_requests.pop(oldest_request_id, None)

    while len(_auth_codes) > settings.max_auth_state_entries:
        oldest_code = min(
            _auth_codes,
            key=lambda key: _auth_codes[key]["expires_at"],
        )
        code_data = _auth_codes.pop(oldest_code, None)
        if code_data:
            await _close_client_safely(code_data.get("client"))

    while len(_session_cache) > settings.max_session_cache_entries:
        oldest_session_key = min(
            _session_cache,
            key=lambda key: _session_cache[key]["last_used"],
        )
        session_data = _session_cache.pop(oldest_session_key, None)
        if session_data:
            await _close_client_safely(session_data.get("client"))


async def get_or_create_session(access_token: str) -> dict:
    """access_token からセッションを取得。

    トークンには認証情報を埋め込まないため、ウォームコンテナ上の
    メモリキャッシュに存在するアクティブセッションのみ利用できる。
    コールドスタート後やキャッシュ失効後は再ログインを要求する。
    """
    await _cleanup_expired_state()
    key = _cache_key(access_token)

    # 1. メモリキャッシュチェック
    if key in _session_cache:
        sess = _session_cache[key]
        sess["last_used"] = time.time()
        return sess

    # 2. トークン復号
    payload = _decrypt_token(access_token)
    if payload is None:
        raise HTTPException(
            status_code=401, detail="無効なトークンです。再ログインしてください。"
        )

    if payload.get("type") != "access":
        raise HTTPException(status_code=401, detail="無効なトークンタイプです。")

    exp = payload.get("exp", 0)
    if time.time() > exp:
        raise HTTPException(status_code=401, detail="トークンの有効期限が切れました。")

    # 3. セッションキャッシュに存在しない = コールドスタートまたはセッション切れ
    # セキュリティ: トークンに認証情報を含めないため、再ログインを要求
    raise HTTPException(
        status_code=401,
        detail="セッションが切れました。再ログインしてください。",
    )


def _invalid_oauth_request_page(message: str, status_code: int = 400) -> HTMLResponse:
    safe_message = escape(message)
    return HTMLResponse(
        f"<h1>OAuth Error</h1><p>{safe_message}</p>",
        status_code=status_code,
    )


def _normalize_login_error(exc: Exception) -> str:
    if isinstance(exc, RuntimeError):
        return str(exc)
    if isinstance(exc, ValueError):
        return "入力内容が不正です。確認して再試行してください。"
    return "ログインに失敗しました。時間を置いて再試行してください。"


# ─── 認可エンドポイント ──────────────────────────────


@oauth_router.get("/oauth/authorize", response_class=HTMLResponse)
async def authorize(
    response_type: Annotated[str, Query()] = "code",
    client_id: Annotated[str, Query()] = "",
    redirect_uri: Annotated[str, Query()] = "",
    scope: Annotated[str, Query()] = "",
    state: Annotated[str, Query()] = "",
):
    """OAuth認可エンドポイント。ログインフォームを表示する。"""
    await _cleanup_expired_state()
    if response_type != "code":
        return _invalid_oauth_request_page("response_type must be 'code'")
    if client_id != settings.oauth_client_id:
        return _invalid_oauth_request_page("invalid client_id")
    if scope and "openid" not in scope.split():
        return _invalid_oauth_request_page("unsupported scope")

    try:
        validated_redirect_uri = validate_redirect_uri(
            redirect_uri, settings.allowed_redirect_hosts
        )
    except ValueError as exc:
        return _invalid_oauth_request_page(str(exc))

    auth_request_id = secrets.token_urlsafe(24)
    _auth_requests[auth_request_id] = {
        "client_id": client_id,
        "redirect_uri": validated_redirect_uri,
        "state": state,
        "scope": scope,
        "expires_at": time.time() + AUTH_REQUEST_EXPIRY,
    }
    await _enforce_state_limits()
    return HTMLResponse(_render_login_form(auth_request_id, error=""))


@oauth_router.post("/oauth/callback")
async def oauth_callback(
    request: Request,
    auth_request_id: Annotated[str, Form()],
    username: Annotated[str, Form()],
    password: Annotated[str, Form()],
    totp_code: Annotated[str, Form()] = "",
    totp_secret: Annotated[str, Form()] = "",
):
    """ログインフォームの送信を処理し、ChatGPTにリダイレクト。"""
    await _cleanup_expired_state()
    auth_request = _auth_requests.get(auth_request_id)
    if not auth_request:
        return _invalid_oauth_request_page(
            "ログイン画面の有効期限が切れました。最初からやり直してください。"
        )

    client_ip = extract_client_ip(request)
    if not _oauth_form_limiter.allow(f"oauth-form:{client_ip}"):
        return HTMLResponse(
            _render_login_form(
                auth_request_id,
                error="試行回数が多すぎます。数分後に再試行してください。",
            ),
            status_code=429,
        )

    effective_totp_secret = totp_secret.strip()

    client = CampusSquareClient(
        username=username.strip(),
        password=password,
        totp_secret=effective_totp_secret,
    )

    try:
        if not effective_totp_secret and totp_code.strip():
            await _login_with_direct_totp(client, totp_code.strip())
        else:
            await client.login()
    except Exception as e:
        logger.warning("OAuthログイン失敗 (%s)", type(e).__name__)
        await client.close()
        return HTMLResponse(
            _render_login_form(auth_request_id, error=_normalize_login_error(e)),
            status_code=200,
        )

    # ログイン成功 → 認証情報を即座に消去し、認可コード発行
    client.wipe_credentials()  # ID/PW/TOTP をメモリから消去
    _auth_requests.pop(auth_request_id, None)

    code = secrets.token_urlsafe(32)
    _auth_codes[code] = {
        "client_id": auth_request["client_id"],
        "redirect_uri": auth_request["redirect_uri"],
        "state": auth_request["state"],
        "client": client,  # JSESSIONIDのみ保持 (認証情報は消去済み)
        "expires_at": time.time() + AUTH_CODE_EXPIRY,
    }
    await _cleanup_expired_state()
    await _enforce_state_limits()

    redirect_uri = auth_request["redirect_uri"]
    query = urlencode({"code": code, "state": auth_request["state"]})
    separator = "&" if "?" in redirect_uri else "?"
    return RedirectResponse(
        f"{redirect_uri}{separator}{query}", status_code=302
    )


@oauth_router.post("/oauth/token")
async def token_exchange(request: Request):
    """トークンエンドポイント。認可コード or リフレッシュトークンを処理。"""
    await _cleanup_expired_state()
    client_ip = extract_client_ip(request)
    if not _token_limiter.allow(f"oauth-token:{client_ip}"):
        raise HTTPException(status_code=429, detail="Too many token requests")

    form = await request.form()
    grant_type = form.get("grant_type", "")
    client_id = form.get("client_id", "")
    client_secret = form.get("client_secret", "")

    # Basic 認証ヘッダー対応
    if not client_id:
        auth_header = request.headers.get("authorization", "")
        if auth_header.startswith("Basic "):
            try:
                decoded = base64.b64decode(auth_header[6:]).decode()
            except Exception as exc:
                raise HTTPException(
                    status_code=401, detail="Invalid basic authorization header"
                ) from exc
            if ":" in decoded:
                client_id, client_secret = decoded.split(":", 1)

    if not (
        secrets.compare_digest(client_id, settings.oauth_client_id)
        and secrets.compare_digest(client_secret, settings.oauth_client_secret)
    ):
        raise HTTPException(status_code=401, detail="Invalid client credentials")

    if grant_type == "authorization_code":
        return await _handle_authorization_code(form, client_id)
    elif grant_type == "refresh_token":
        return await _handle_refresh_token(form)
    else:
        raise HTTPException(
            status_code=400, detail=f"Unsupported grant_type: {grant_type}"
        )


# ─── トークン生成・交換 ──────────────────────────────


def _create_tokens() -> dict:
    """暗号化された access_token と refresh_token を生成。

    セキュリティ: 認証情報(ID/PW/TOTP)は一切含めない。
    セッションIDのみを暗号化して埋め込む。
    """
    now = time.time()
    session_id = secrets.token_hex(16)

    access_token = _encrypt_token(
        {
            "type": "access",
            "session_id": session_id,
            "exp": now + SESSION_EXPIRY,
            "iat": now,
        }
    )

    refresh_token = _encrypt_token(
        {
            "type": "refresh",
            "session_id": session_id,
            "iat": now,
        }
    )

    return {
        "access_token": access_token,
        "token_type": "bearer",
        "refresh_token": refresh_token,
        "expires_in": SESSION_EXPIRY,
    }


async def _handle_authorization_code(form, client_id: str) -> JSONResponse:
    """認可コード → access_token 交換。"""
    code = form.get("code", "")
    redirect_uri = form.get("redirect_uri", "")

    code_data = _auth_codes.pop(code, None)
    if not code_data:
        raise HTTPException(
            status_code=400, detail="Invalid or expired authorization code"
        )
    if code_data["expires_at"] < time.time():
        await _close_client_safely(code_data.get("client"))
        raise HTTPException(status_code=400, detail="Authorization code expired")
    if not redirect_uri:
        await _close_client_safely(code_data.get("client"))
        raise HTTPException(status_code=400, detail="redirect_uri is required")
    if client_id != code_data["client_id"]:
        await _close_client_safely(code_data.get("client"))
        raise HTTPException(status_code=401, detail="client_id mismatch")
    if redirect_uri != code_data["redirect_uri"]:
        await _close_client_safely(code_data.get("client"))
        raise HTTPException(status_code=400, detail="redirect_uri mismatch")

    # 認可コードに紐づいた CampusSquare クライアントをキャッシュ
    # セキュリティ: 認証情報は既にclient.wipe_credentials()で消去済み
    tokens = _create_tokens()

    # ウォームキャッシュに保存 (ログイン済みクライアント=JSESSIONIDのみ)
    client = code_data["client"]
    scraper = CampusSquareScraper(client)
    key = _cache_key(tokens["access_token"])
    _session_cache[key] = {
        "client": client,
        "scraper": scraper,
        "last_used": time.time(),
    }
    await _enforce_state_limits()

    logger.debug("access_token 発行")
    return JSONResponse(tokens)


async def _handle_refresh_token(form) -> JSONResponse:
    """リフレッシュトークン → 新しい access_token 発行。"""
    refresh_token = form.get("refresh_token", "")

    payload = _decrypt_token(refresh_token)
    if payload is None or payload.get("type") != "refresh":
        raise HTTPException(status_code=401, detail="Invalid refresh token")

    # セキュリティ: トークンに認証情報を含めないため、リフレッシュ時は再ログインを要求
    # ChatGPTは401を受けるとトークンを削除し、ユーザーに再認証を促す
    raise HTTPException(
        status_code=401,
        detail="セッションが切れました。再ログインしてください。",
    )


# ─── TOTP 直接入力対応 ──────────────────────────────


async def _login_with_direct_totp(client: CampusSquareClient, totp_code: str) -> None:
    """6桁TOTP コードを直接入力してログイン (TOTP secret なし)。"""
    from bs4 import BeautifulSoup

    # Step 1: 初期ページ
    resp = await client._request("GET", client.portal_url, params={"locale": "ja_JP"})
    resp.raise_for_status()
    client._rwf_hash = client._extract_rwf_hash(resp.text)
    if not client._rwf_hash:
        raise RuntimeError("rwfHash の抽出に失敗")

    # Step 2: ログインPOST
    resp = await client._request(
        "POST",
        client.portal_url,
        data={
            "userName": client.username,
            "password": client.password,
            "wfId": "nwf_PTW0000002_login",
            "locale": "ja_JP",
            "action": "rwf",
            "tabId": "home",
            "page": "",
            "rwfHash": client._rwf_hash,
        },
        headers={
            "Content-Type": "application/x-www-form-urlencoded",
            "X-Requested-With": "XMLHttpRequest",
            "Referer": f"{client.portal_url}?locale=ja_JP",
        },
    )
    resp.raise_for_status()

    # Step 3: page=main で TOTP ページ取得
    resp = await client._request("GET", client.portal_url, params={"page": "main"})
    resp.raise_for_status()
    html = resp.text

    if not client._has_totp_form(html):
        new_hash = client._extract_rwf_hash(html)
        if new_hash:
            client._rwf_hash = new_hash
        client._logged_in = True
        client._update_activity()
        return

    # Step 4: TOTP コード直接送信
    soup = BeautifulSoup(html, "lxml")
    form = soup.find("form", attrs={"name": "form"})
    hidden_fields: dict[str, str] = {}
    if form:
        for inp in form.find_all("input", {"type": "hidden"}):
            name = inp.get("name", "")
            if name:
                hidden_fields[name] = inp.get("value", "")

    action_url = client.portal_url
    if form and form.get("action"):
        action_url = client._normalize_internal_url(form["action"])

    resp = await client._request(
        "POST",
        action_url,
        data={**hidden_fields, "ninshoCode": totp_code},
        headers={
            "Content-Type": "application/x-www-form-urlencoded",
            "Referer": client.portal_url,
        },
    )
    resp.raise_for_status()

    if client._has_totp_form(resp.text):
        raise RuntimeError("TOTP認証に失敗しました。コードを確認してください。")

    # Step 5: メインページ取得
    resp = await client._request("GET", client.portal_url, params={"page": "main"})
    resp.raise_for_status()
    new_hash = client._extract_rwf_hash(resp.text)
    if new_hash:
        client._rwf_hash = new_hash
    client._logged_in = True
    client._update_activity()


# ─── ログインフォーム HTML ──────────────────────────


def _render_login_form(auth_request_id: str, error: str) -> str:
    safe_auth_request_id = escape(auth_request_id, quote=True)
    error_html = ""
    if error:
        error_html = f'<div class="error">{escape(error)}</div>'

    return f"""<!DOCTYPE html>
<html lang="ja">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>新潟大学 学務情報システム - ログイン</title>
<style>
  * {{ margin: 0; padding: 0; box-sizing: border-box; }}
  body {{
    font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', 'Hiragino Sans',
                 'Noto Sans JP', sans-serif;
    background: #f5f6f8;
    min-height: 100vh;
    display: flex;
    flex-direction: column;
    align-items: center;
    justify-content: center;
    padding: 20px;
  }}
  .container {{
    background: #fff;
    border-radius: 6px;
    border: 1px solid #e0e2e6;
    padding: 36px 32px 32px;
    max-width: 400px;
    width: 100%;
    box-shadow: 0 1px 3px rgba(0,0,0,0.06), 0 4px 12px rgba(0,0,0,0.04);
  }}
  .header {{
    text-align: center;
    margin-bottom: 28px;
    padding-bottom: 20px;
    border-bottom: 1px solid #eee;
  }}
  .header h1 {{
    font-size: 17px;
    font-weight: 700;
    color: #1a1a1a;
    letter-spacing: 0.02em;
    margin-bottom: 6px;
  }}
  .header .subtitle {{
    font-size: 13px;
    color: #6b7280;
    font-weight: 400;
  }}
  .error {{
    background: #fef2f2;
    border: 1px solid #fecaca;
    color: #b91c1c;
    padding: 10px 14px;
    border-radius: 5px;
    font-size: 13px;
    margin-bottom: 20px;
    line-height: 1.5;
  }}
  .field {{
    margin-bottom: 18px;
  }}
  label {{
    display: block;
    font-size: 13px;
    font-weight: 600;
    color: #374151;
    margin-bottom: 5px;
  }}
  input[type="text"], input[type="password"] {{
    width: 100%;
    padding: 9px 12px;
    border: 1px solid #d1d5db;
    border-radius: 5px;
    font-size: 14px;
    color: #1a1a1a;
    background: #fff;
    transition: border-color 0.15s, box-shadow 0.15s;
  }}
  input:focus {{
    outline: none;
    border-color: #2563eb;
    box-shadow: 0 0 0 3px rgba(37, 99, 235, 0.1);
  }}
  input::placeholder {{
    color: #9ca3af;
  }}
  .section-label {{
    font-size: 12px;
    font-weight: 600;
    color: #6b7280;
    text-transform: uppercase;
    letter-spacing: 0.05em;
    margin-bottom: 12px;
    margin-top: 24px;
  }}
  .totp-section {{
    background: #f9fafb;
    border: 1px solid #e5e7eb;
    border-radius: 5px;
    padding: 16px;
    margin-bottom: 24px;
  }}
  .totp-section .hint {{
    font-size: 12px;
    color: #6b7280;
    margin-bottom: 12px;
    line-height: 1.5;
  }}
  .totp-section .field {{
    margin-bottom: 14px;
  }}
  .totp-section .field:last-child {{
    margin-bottom: 0;
  }}
  button {{
    width: 100%;
    padding: 10px 16px;
    background: #1d4ed8;
    color: #fff;
    border: none;
    border-radius: 5px;
    font-size: 14px;
    font-weight: 600;
    cursor: pointer;
    transition: background-color 0.15s;
  }}
  button:hover {{
    background: #1e40af;
  }}
  button:active {{
    background: #1e3a8a;
  }}
  .footer {{
    font-size: 11px;
    color: #9ca3af;
    text-align: center;
    margin-top: 16px;
    line-height: 1.6;
  }}
  .footer svg {{
    vertical-align: -2px;
    margin-right: 2px;
  }}
</style>
</head>
<body>
<div class="container">
  <div class="header">
    <h1>新潟大学 学務情報システム</h1>
    <p class="subtitle">ChatGPT連携のためにログインしてください</p>
  </div>
  {error_html}
  <form method="post" action="/oauth/callback">
    <input type="hidden" name="auth_request_id" value="{safe_auth_request_id}">

    <div class="field">
      <label for="username">ユーザーID</label>
      <input type="text" id="username" name="username"
             placeholder="f00x000x" required autocomplete="username">
    </div>

    <div class="field">
      <label for="password">パスワード</label>
      <input type="password" id="password" name="password"
             required autocomplete="current-password">
    </div>

    <p class="section-label">二段階認証 (学外アクセス時)</p>

    <div class="totp-section">
      <p class="hint">学外からのアクセスには認証アプリの確認コードが必要です。</p>

      <div class="field">
        <label for="totp_code">6桁の確認コード</label>
        <input type="text" id="totp_code" name="totp_code"
               placeholder="000000" maxlength="6" inputmode="numeric"
               pattern="[0-9]*" autocomplete="one-time-code">
      </div>
    </div>

    <button type="submit">ログイン</button>
  </form>
  <p class="footer">
    <svg width="12" height="12" viewBox="0 0 24 24" fill="none" stroke="currentColor"
         stroke-width="2" stroke-linecap="round" stroke-linejoin="round">
      <rect x="3" y="11" width="18" height="11" rx="2" ry="2"/>
      <path d="M7 11V7a5 5 0 0 1 10 0v4"/>
    </svg>
    認証情報はログイン処理のみに使用され、サーバーに保存されません
  </p>
</div>
</body>
</html>"""
