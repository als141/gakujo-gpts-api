from typing import Annotated
from urllib.parse import urlparse

from pydantic import field_validator, model_validator
from pydantic_settings import BaseSettings, NoDecode


class Settings(BaseSettings):
    """アプリケーション設定。環境変数または .env ファイルから読み込む。"""

    gakujo_username: str = ""
    gakujo_password: str = ""
    gakujo_totp_secret: str = ""

    # CampusSquare URLs
    base_url: str = "https://gakujo.iess.niigata-u.ac.jp/campusweb"
    portal_url: str = "https://gakujo.iess.niigata-u.ac.jp/campusweb/campusportal.do"
    web_url: str = "https://gakujo.iess.niigata-u.ac.jp/campusweb/campussquare.do"

    # API設定
    api_key: str = ""
    server_url: str = ""  # OpenAPIスキーマのservers URL (ngrok URLなど)
    token_secret: str = ""  # Fernet暗号化キー (未設定時は起動時に自動生成)
    oauth_client_id: str = "gakujo-gpts"
    oauth_client_secret: str = "gakujo-gpts-secret"
    allowed_redirect_hosts: Annotated[list[str], NoDecode] = [
        "chat.openai.com",
        "chatgpt.com",
        "localhost",
        "127.0.0.1",
    ]
    allowed_hosts: Annotated[list[str], NoDecode] = []
    cors_allow_origins: Annotated[list[str], NoDecode] = []
    allow_env_credentials: bool = False
    response_cache_ttl_seconds: int = 0
    max_active_http_requests: int = 8
    active_http_request_acquire_timeout_seconds: float = 15.0
    campus_max_concurrent_requests: int = 1
    campus_min_request_interval_seconds: float = 0.5
    max_session_cache_entries: int = 128
    max_auth_state_entries: int = 256
    oauth_form_rate_limit_max_attempts: int = 10
    oauth_form_rate_limit_window_seconds: int = 300
    token_rate_limit_max_attempts: int = 30
    token_rate_limit_window_seconds: int = 300
    debug: bool = False

    @field_validator(
        "allowed_redirect_hosts",
        "allowed_hosts",
        "cors_allow_origins",
        mode="before",
    )
    @classmethod
    def _parse_csv_list(cls, value):
        if value is None:
            return []
        if isinstance(value, str):
            return [item.strip() for item in value.split(",") if item.strip()]
        return value

    @model_validator(mode="after")
    def _validate_security_settings(self):
        if self.server_url:
            host = urlparse(self.server_url).hostname
            if host:
                merged_hosts = {*(self.allowed_hosts or []), host}
                self.allowed_hosts = sorted(merged_hosts)

        if not self.debug:
            if not self.token_secret:
                raise ValueError("TOKEN_SECRET must be set when DEBUG=false")
            if not self.oauth_client_id:
                raise ValueError("OAUTH_CLIENT_ID must be set when DEBUG=false")
            if not self.oauth_client_secret:
                raise ValueError("OAUTH_CLIENT_SECRET must be set when DEBUG=false")
            if self.oauth_client_secret == "gakujo-gpts-secret":
                raise ValueError(
                    "OAUTH_CLIENT_SECRET must not use the default placeholder when DEBUG=false"
                )

        return self

    model_config = {
        "env_file": ".env",
        "env_file_encoding": "utf-8",
        "extra": "ignore",
    }


settings = Settings()
