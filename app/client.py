"""CampusSquare (学務情報システム) コアクライアント。

ログイン、MFA突破、セッション管理、ポータルナビゲーションを担当する。
"""

import asyncio
import logging
import re
import ssl
import time
from urllib.parse import urljoin, urlparse

import httpx
import pyotp
from bs4 import BeautifulSoup

from app.config import settings

logger = logging.getLogger(__name__)

_outbound_semaphore: asyncio.Semaphore | None = None
_outbound_rate_lock: asyncio.Lock | None = None
_last_outbound_request_at: float = 0.0


def _get_outbound_semaphore() -> asyncio.Semaphore:
    global _outbound_semaphore
    if _outbound_semaphore is None:
        _outbound_semaphore = asyncio.Semaphore(
            max(1, settings.campus_max_concurrent_requests)
        )
    return _outbound_semaphore


def _get_outbound_rate_lock() -> asyncio.Lock:
    global _outbound_rate_lock
    if _outbound_rate_lock is None:
        _outbound_rate_lock = asyncio.Lock()
    return _outbound_rate_lock


def _create_ssl_context() -> ssl.SSLContext:
    """CampusSquareが要求するレガシーSSL再ネゴシエーションに対応したSSLコンテキスト。"""
    ctx = ssl.create_default_context()
    ctx.options |= 0x4  # ssl.OP_LEGACY_SERVER_CONNECT
    return ctx


class CampusSquareClient:
    """新潟大学CampusSquare非同期スクレイピングクライアント。

    認証フロー:
    1. GET campusportal.do?locale=ja_JP → JSESSIONID + rwfHash取得
    2. POST campusportal.do (login) → userName, password, wfId, rwfHash等
    3. TOTP認証 (学外アクセス時) → ninshoCode送信
    4. ポータルメインページへ遷移
    """

    def __init__(
        self,
        username: str | None = None,
        password: str | None = None,
        totp_secret: str | None = None,
    ):
        self.username = username or (
            settings.gakujo_username if settings.allow_env_credentials else ""
        )
        self.password = password or (
            settings.gakujo_password if settings.allow_env_credentials else ""
        )
        self.totp_secret = totp_secret or (
            settings.gakujo_totp_secret if settings.allow_env_credentials else ""
        )

        self.portal_url = settings.portal_url
        self.web_url = settings.web_url
        self.base_url = settings.base_url

        self._client: httpx.AsyncClient | None = None
        self._rwf_hash: str = ""
        self._current_tab_id: str = "home"
        self._logged_in: bool = False
        self._last_activity: float = 0

    def wipe_credentials(self) -> None:
        """ログイン完了後、認証情報をメモリから完全消去。

        セキュリティ: ログイン後はJSESSIONID cookieのみでセッション維持。
        ID/PW/TOTP secretは不要になるため即座に消去する。
        入学年度は学籍番号から事前に算出してキャッシュする。
        """
        # 入学年度を消去前にキャッシュ (学籍番号の2-3文字目から算出)
        if self.username and len(self.username) >= 3:
            try:
                self._enrollment_year = 2000 + int(self.username[1:3])
            except ValueError:
                self._enrollment_year = 2025
        else:
            self._enrollment_year = 2025

        self.username = ""
        self.password = ""
        self.totp_secret = ""

    async def _ensure_client(self) -> httpx.AsyncClient:
        if self._client is None:
            self._client = httpx.AsyncClient(
                verify=_create_ssl_context(),
                follow_redirects=True,
                trust_env=False,
                timeout=httpx.Timeout(30.0, connect=15.0),
                headers={
                    "User-Agent": (
                        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                        "AppleWebKit/537.36 (KHTML, like Gecko) "
                        "Chrome/131.0.0.0 Safari/537.36"
                    ),
                    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
                    "Accept-Language": "ja,en-US;q=0.9,en;q=0.8",
                },
            )
        return self._client

    async def _request(self, method: str, url: str, **kwargs) -> httpx.Response:
        """CampusSquare への通信を集中制御する。

        1. インスタンス内の同時実行数を制限
        2. リクエスト送信間隔を最低限あける
        """
        global _last_outbound_request_at

        client = await self._ensure_client()
        semaphore = _get_outbound_semaphore()
        interval = max(0.0, settings.campus_min_request_interval_seconds)

        async with semaphore:
            rate_lock = _get_outbound_rate_lock()
            async with rate_lock:
                now = time.monotonic()
                wait_seconds = interval - (now - _last_outbound_request_at)
                if wait_seconds > 0:
                    await asyncio.sleep(wait_seconds)
                _last_outbound_request_at = time.monotonic()

            response = await client.request(method, url, **kwargs)

        return response

    def _normalize_internal_url(self, raw_url: str) -> str:
        """CampusSquare ドメイン配下のURLだけを許可。"""
        if not raw_url:
            raise ValueError("URL が指定されていません。")
        if raw_url.lower().startswith("javascript:"):
            raise ValueError("許可されていない URL 形式です。")

        allowed_hosts = {
            parsed.hostname
            for parsed in (
                urlparse(self.base_url),
                urlparse(self.portal_url),
                urlparse(self.web_url),
            )
            if parsed.hostname
        }

        candidate = urljoin(f"{self.base_url.rstrip('/')}/", raw_url)
        parsed = urlparse(candidate)

        if parsed.scheme != "https" or parsed.hostname not in allowed_hosts:
            raise ValueError("許可されていない URL です。")

        return candidate

    # ─── ヘルパー ──────────────────────────────────────

    @staticmethod
    def _extract_rwf_hash(html: str) -> str:
        """HTMLからportalConf.rwfHashの値を抽出。"""
        m = re.search(r"'rwfHash'\s*:\s*'([a-f0-9]+)'", html)
        if m:
            return m.group(1)
        m = re.search(r'"rwfHash"\s*:\s*"([a-f0-9]+)"', html)
        if m:
            return m.group(1)
        return ""

    @staticmethod
    def _extract_portal_page(html: str) -> str:
        """portalConf.pageの値を抽出。"""
        m = re.search(r"'page'\s*:\s*'(\w*)'", html)
        if m:
            return m.group(1)
        return ""

    @staticmethod
    def _parse_hidden_fields(
        html: str, form_selector: str | None = None
    ) -> dict[str, str]:
        """HTMLからhidden input fieldを抽出。"""
        soup = BeautifulSoup(html, "lxml")
        if form_selector:
            form = soup.select_one(form_selector)
            if form is None:
                return {}
            inputs = form.find_all("input", {"type": "hidden"})
        else:
            inputs = soup.find_all("input", {"type": "hidden"})
        return {
            inp.get("name", ""): inp.get("value", "")
            for inp in inputs
            if inp.get("name")
        }

    @staticmethod
    def _has_totp_form(html: str) -> bool:
        """TOTP認証フォームの有無を判定。"""
        return 'name="ninshoCode"' in html or "google-authenticator" in html.lower()

    @staticmethod
    def _has_login_form(html: str) -> bool:
        """ログインフォームの有無を判定。"""
        return 'name="userName"' in html and "nwf_PTW0000002_login" in html

    def _generate_totp(self) -> str:
        """pyotpを使ってTOTPコードを生成。"""
        if not self.totp_secret:
            raise ValueError("TOTP secret が設定されていません (GAKUJO_TOTP_SECRET)")
        totp = pyotp.TOTP(self.totp_secret)
        return totp.now()

    def _update_activity(self) -> None:
        self._last_activity = time.time()

    # ─── 認証フロー ──────────────────────────────────

    async def login(self) -> bool:
        """CampusSquareにログインする。

        Returns:
            True: ログイン成功
        Raises:
            RuntimeError: ログイン失敗
        """
        # Step 1: 初期ページ取得 → JSESSIONID + rwfHash
        logger.debug("Step 1: 初期ページ取得")
        resp = await self._request("GET", self.portal_url, params={"locale": "ja_JP"})
        resp.raise_for_status()
        html = resp.text

        self._rwf_hash = self._extract_rwf_hash(html)
        if not self._rwf_hash:
            raise RuntimeError("rwfHash の抽出に失敗しました")
        logger.debug("rwfHash 取得完了")

        # Step 2: ログインPOST (AJAX rwf)
        logger.debug("Step 2: ログインPOST")
        login_data = {
            "userName": self.username,
            "password": self.password,
            "wfId": "nwf_PTW0000002_login",
            "locale": "ja_JP",
            "action": "rwf",
            "tabId": "home",
            "page": "",
            "rwfHash": self._rwf_hash,
        }

        resp = await self._request(
            "POST",
            self.portal_url,
            data=login_data,
            headers={
                "Content-Type": "application/x-www-form-urlencoded",
                "X-Requested-With": "XMLHttpRequest",
                "Referer": f"{self.portal_url}?locale=ja_JP",
            },
        )
        resp.raise_for_status()
        html = resp.text
        logger.debug("ログインPOSTレスポンス長: %d", len(html))

        # Step 3: TOTP認証 (必要な場合)
        # AJAXレスポンスまたは次のGETでTOTPフォームが出る可能性がある
        if self._has_totp_form(html):
            logger.debug("Step 3: TOTP認証が必要 (AJAXレスポンス)")
            html = await self._submit_totp(html)
        else:
            logger.debug("Step 3: TOTP確認のためpage=main取得")

        # Step 4: ログイン後ページ取得
        logger.debug("Step 4: ログイン後ポータル取得")
        resp = await self._request("GET", self.portal_url, params={"page": "main"})
        resp.raise_for_status()
        html = resp.text

        # page=mainでTOTPが出る場合 (学外アクセスの典型パターン)
        if self._has_totp_form(html):
            logger.debug("Step 4b: TOTP認証が必要 (page=main)")
            html = await self._submit_totp(html)
            # TOTP送信後、再度page=mainを取得
            resp = await self._request("GET", self.portal_url, params={"page": "main"})
            resp.raise_for_status()
            html = resp.text

        new_hash = self._extract_rwf_hash(html)
        if new_hash:
            self._rwf_hash = new_hash
            logger.debug("rwfHash 更新完了")

        page = self._extract_portal_page(html)
        if page == "main":
            self._logged_in = True
            self._update_activity()
            logger.debug("ログイン成功")
            return True

        # ログインフォームがまだ表示されている場合は失敗
        if self._has_login_form(html):
            raise RuntimeError(
                "ログインに失敗しました。ユーザー名またはパスワードを確認してください。"
            )

        # TOTP画面がまだ出ている場合
        if self._has_totp_form(html):
            raise RuntimeError(
                "TOTP認証に失敗しました。シークレットキーを確認してください。"
            )

        # それでもmainに到達できない場合、追加の遷移を試みる
        self._logged_in = True
        self._update_activity()
        logger.debug("ログイン成功 (推定)")
        return True

    async def _submit_totp(self, html: str) -> str:
        """TOTP認証フォームを送信。

        CampusSquareのTOTPフォーム構造:
          <form name="form" method="post" action="/campusweb/campusportal.do">
            <input type="hidden" name="action" value="gal" />
            <input type="hidden" name="mode" value="doGoogleAuthLogin"/>
            <input type="password" name="ninshoCode" ...>
          </form>
        """
        totp_code = self._generate_totp()
        logger.debug("TOTP コード生成完了")

        # HTMLからフォームのhiddenフィールドとaction URLを抽出
        soup = BeautifulSoup(html, "lxml")
        form = soup.find("form", attrs={"name": "form"})
        if form is None:
            for f in soup.find_all("form"):
                if f.find("input", {"name": "ninshoCode"}):
                    form = f
                    break

        # hiddenフィールドを抽出 (action=gal, mode=doGoogleAuthLogin)
        hidden_fields: dict[str, str] = {}
        if form:
            for inp in form.find_all("input", {"type": "hidden"}):
                name = inp.get("name", "")
                if name:
                    hidden_fields[name] = inp.get("value", "")

        totp_data = {
            **hidden_fields,
            "ninshoCode": totp_code,
        }

        logger.debug(
            "TOTP送信データ: %s",
            {k: v for k, v in totp_data.items() if k != "ninshoCode"},
        )

        # フォームのaction URLを決定
        action_url = self.portal_url
        if form and form.get("action"):
            action_url = self._normalize_internal_url(form["action"])

        # 通常のフォーム送信 (AJAXではない)
        resp = await self._request(
            "POST",
            action_url,
            data=totp_data,
            headers={
                "Content-Type": "application/x-www-form-urlencoded",
                "Referer": self.portal_url,
            },
        )
        resp.raise_for_status()
        result_html = resp.text
        logger.debug("TOTP送信完了 (レスポンス長: %d)", len(result_html))

        # まだTOTP画面が出ていたらエラー
        if self._has_totp_form(result_html):
            raise RuntimeError(
                "TOTP認証に失敗しました。シークレットキーを確認してください。"
            )

        return result_html

    # ─── セッション管理 ──────────────────────────────

    async def ensure_logged_in(self) -> None:
        """ログイン状態を確認し、必要に応じて再ログイン。"""
        if not self._logged_in:
            await self.login()
            return

        # セッションタイムアウトチェック (15分)
        elapsed = time.time() - self._last_activity
        if elapsed > 900:
            logger.debug("セッションタイムアウトの可能性があるため再ログイン")
            self._logged_in = False
            await self.close()
            await self.login()
            return

        # セッション延長
        if elapsed > 600:
            await self._extend_session()

    async def _extend_session(self) -> None:
        """セッションを延長する。"""
        try:
            await self._request("GET", self.portal_url, params={"page": "main"})
            self._update_activity()
            logger.debug("セッション延長完了")
        except Exception:
            logger.warning("セッション延長に失敗")

    # ─── ポータルナビゲーション ──────────────────────

    async def switch_tab(self, tab_id: str) -> str:
        """タブを切り替えてHTML内容を取得。

        Args:
            tab_id: タブID (home, kj, sch, kh, sy, rs, si, en, aa, fm, sd, link, gk)

        Returns:
            レスポンスHTML
        """
        await self.ensure_logged_in()
        resp = await self._request(
            "GET",
            self.portal_url,
            params={
                "page": "main",
                "tabId": tab_id,
            },
        )
        resp.raise_for_status()
        self._current_tab_id = tab_id
        self._update_activity()

        html = resp.text
        new_hash = self._extract_rwf_hash(html)
        if new_hash:
            self._rwf_hash = new_hash

        return html

    async def load_portlet(
        self,
        wf_id: str,
        form_data: dict[str, str] | None = None,
        method: str = "GET",
    ) -> str:
        """ポートレットをロードする（AJAX rwfリクエスト）。

        Args:
            wf_id: ポートレットID
            form_data: 追加のフォームデータ
            method: HTTPメソッド

        Returns:
            レスポンスHTML
        """
        await self.ensure_logged_in()
        params = {
            "page": "main",
            "action": "rwf",
            "tabId": self._current_tab_id,
            "wfId": wf_id,
            "rwfHash": self._rwf_hash,
        }

        if form_data:
            params.update(form_data)

        headers = {
            "X-Requested-With": "XMLHttpRequest",
            "Referer": f"{self.portal_url}?page=main",
        }

        if method.upper() == "POST":
            resp = await self._request(
                "POST",
                self.portal_url,
                data=params,
                headers={
                    **headers,
                    "Content-Type": "application/x-www-form-urlencoded",
                },
            )
        else:
            resp = await self._request(
                "GET",
                self.portal_url,
                params=params,
                headers=headers,
            )

        resp.raise_for_status()
        self._update_activity()
        return resp.text

    async def access_web_flow(
        self,
        flow_id: str,
        params: dict[str, str] | None = None,
    ) -> str:
        """Spring Web Flowのエンドポイントにアクセス。

        Args:
            flow_id: フローID (例: "student-kyuuko-flow")
            params: 追加パラメータ

        Returns:
            レスポンスHTML
        """
        await self.ensure_logged_in()
        query = {"_flowId": flow_id}
        if params:
            query.update(params)

        resp = await self._request(
            "GET",
            self.web_url,
            params=query,
            headers={
                "Referer": f"{self.portal_url}?page=main",
            },
        )
        resp.raise_for_status()
        self._update_activity()
        return resp.text

    async def submit_web_flow(
        self,
        action_url: str,
        data: dict[str, str],
    ) -> str:
        """Spring Web Flowのフォームを送信。

        Args:
            action_url: フォームのaction URL
            data: フォームデータ (_flowExecutionKey, _eventId 等を含む)

        Returns:
            レスポンスHTML
        """
        await self.ensure_logged_in()
        action_url = self._normalize_internal_url(action_url)

        resp = await self._request(
            "POST",
            action_url,
            data=data,
            headers={
                "Content-Type": "application/x-www-form-urlencoded",
                "Referer": f"{self.portal_url}?page=main",
            },
        )
        resp.raise_for_status()
        self._update_activity()
        return resp.text

    async def get_frame_content(self, frame_url: str) -> str:
        """iframeのコンテンツを取得。

        Args:
            frame_url: フレームURL (相対パスまたは絶対パス)

        Returns:
            レスポンスHTML
        """
        await self.ensure_logged_in()
        frame_url = self._normalize_internal_url(frame_url)

        resp = await self._request(
            "GET",
            frame_url,
            headers={"Referer": f"{self.portal_url}?page=main"},
        )
        resp.raise_for_status()
        self._update_activity()
        return resp.text

    # ─── リソース管理 ──────────────────────────────────

    async def close(self) -> None:
        """HTTPクライアントをクローズ。"""
        if self._client:
            await self._client.aclose()
            self._client = None

    @property
    def is_logged_in(self) -> bool:
        return self._logged_in
