# Gakujo GPTs API

新潟大学の学務情報システム (CampusSquare) をスクレイピングし、GPTs Custom Actions から利用しやすい REST API として公開する FastAPI サーバーです。

## 提供するAPI

### 学務データ
| エンドポイント | 機能 |
| --- | --- |
| `GET /api/v1/timetable` | 時間割 |
| `GET /api/v1/cancellations` | 休講・補講情報 |
| `GET /api/v1/grades` | 成績 |
| `GET /api/v1/reports` | レポート・小テスト一覧 |
| `GET /api/v1/notices` | 連絡通知一覧 |
| `GET /api/v1/notices/detail` | 連絡通知本文 |
| `GET /api/v1/attendance` | 出欠管理 |
| `GET /api/v1/syllabus/search` | シラバス検索 |
| `GET /api/v1/syllabus/detail` | シラバス詳細 |

### 分析
| エンドポイント | 機能 |
| --- | --- |
| `GET /api/v1/attendance/risk` | 出欠リスク分析 |
| `GET /api/v1/digest` | ダイジェスト |

### エクスポート
| エンドポイント | 機能 |
| --- | --- |
| `GET /api/v1/timetable/export` | ICS エクスポート |
| `GET /api/v1/grades/export` | CSV エクスポート |

### その他
| エンドポイント | 機能 |
| --- | --- |
| `GET /privacy` | GPTs 向けプライバシーポリシー |
| `GET /oauth/authorize` | OAuth 認可画面 |
| `POST /oauth/callback` | ログインフォーム送信 |
| `POST /oauth/token` | トークン交換 |

## リポジトリ構成

- `app/`: API 本体
- `tests/`: 単体テストと疎通確認スクリプト
- `deploy/cloudrun-service.yaml`: Cloud Run サービス定義のサンプル
- `gpt-config.md`: GPT Builder 向け設定メモ

次のローカル専用データは Git 追跡対象に含めていません。

- `.env`
- `sample/`
- `knowledge_files/`
- `.playwright-mcp/`

## セットアップ

### 前提

- Python 3.12+
- [uv](https://docs.astral.sh/uv/)

### インストール

```bash
git clone <this-repo>
cd gakujo-gpts-api
uv sync
cp .env.example .env
```

### 最低限の設定

本番では少なくとも次を設定してください。

```env
TOKEN_SECRET=your-strong-secret-key
OAUTH_CLIENT_ID=gakujo-gpts
OAUTH_CLIENT_SECRET=replace-with-a-long-random-secret
SERVER_URL=https://your-cloud-run-url.run.app
ALLOWED_REDIRECT_HOSTS=chat.openai.com,chatgpt.com
ALLOWED_HOSTS=your-cloud-run-url.run.app
RESPONSE_CACHE_TTL_SECONDS=0
CAMPUS_MAX_CONCURRENT_REQUESTS=1
CAMPUS_MIN_REQUEST_INTERVAL_SECONDS=0.5
ALLOW_ENV_CREDENTIALS=false
DEBUG=false
```

補足:

- `DEBUG=false` では `TOKEN_SECRET` 未設定で起動できません。
- `DEBUG=false` では `OAUTH_CLIENT_SECRET=gakujo-gpts-secret` のまま起動できません。
- `RESPONSE_CACHE_TTL_SECONDS=0` が、学務データ本文をサーバー側に残さないデフォルトです。
- `ALLOW_ENV_CREDENTIALS=false` を前提に、Cloud Run へ学生IDやパスワードを置かない構成を推奨します。

### ローカル開発

ローカルでのみ、必要なら以下を `.env` に追加してください。

```env
GAKUJO_USERNAME=
GAKUJO_PASSWORD=
GAKUJO_TOTP_SECRET=
SERVER_URL=http://localhost:8000
ALLOWED_REDIRECT_HOSTS=chat.openai.com,chatgpt.com,localhost,127.0.0.1
ALLOWED_HOSTS=localhost,127.0.0.1
```

ローカルの OAuth 疎通確認では `redirect_uri` に `localhost` を使うため、`ALLOWED_REDIRECT_HOSTS` に `localhost` と `127.0.0.1` を含めてください。

## 起動

```bash
uv run gakujo-api
```

または:

```bash
uv run uvicorn main:app --host 0.0.0.0 --port 8000 --no-access-log
```

Swagger UI: `http://localhost:8000/docs`

## OAuth フロー

この API は GPTs Actions 向けの OAuth Authorization Code Grant を実装しています。

1. GPTs が `GET /oauth/authorize` に遷移する
2. ユーザーがログインフォームに CampusSquare 認証情報を入力する
3. サーバーが短命の認可コードを発行する
4. GPTs が `POST /oauth/token` で `access_token` に交換する
5. 以後は `Authorization: Bearer <access_token>` で API を呼び出す

制約:

- `redirect_uri` は許可ホストに一致しないと拒否されます。
- 認可コードは `client_id` と `redirect_uri` に束縛されます。
- アクセストークンに CampusSquare の認証情報は含めません。
- コールドスタート後やメモリキャッシュ失効後は再ログインが必要です。

## セキュリティ方針

- 学生の認証情報はログイン完了後にメモリ上のクライアントから消去します。
- 学務データのレスポンス本文はデフォルトでサーバー側にキャッシュしません。
- 詳細な内部例外を API レスポンスへ露出しません。
- OAuth フォームとトークン交換にレート制限をかけています。
- `detail_key` などの内部 URL 参照は CampusSquare ドメイン配下に制限しています。
- Trusted Host / CSP / `Cache-Control: no-store` / HSTS を適用しています。
- コンテナは非 root ユーザーで起動します。

注意:

- この設計は「このアプリ自身が永続保存しない」ことを目標にしています。GPTs プラットフォーム側やクラウド基盤側の保存までは制御できません。
- Cloud Run の管理権限を持つ主体は設定やログポリシーを変更できるため、IAM は最小権限で運用してください。

## Cloud Run

Cloud Run のサンプル定義は [deploy/cloudrun-service.yaml](deploy/cloudrun-service.yaml) にあります。利用時は次を必ず置き換えてください。

- `PROJECT_ID`
- `REGION`
- `REPOSITORY`
- `serviceAccountName`
- `SERVER_URL`
- `ALLOWED_HOSTS`

運用チェックリスト:

- Secret Manager で `TOKEN_SECRET` と `OAUTH_CLIENT_SECRET` を注入する
- `containerConcurrency: 1` と低い `maxScale` を維持する
- Cloud Armor などで `/oauth/callback` と `/oauth/token` に追加のレート制限をかける
- Cloud Logging の保持期間と除外フィルタを見直す
- カスタムドメイン利用時は `ALLOWED_HOSTS` にそのホストも追加する
- デプロイ後に `/oauth/authorize` の `redirect_uri` 拒否動作を確認する

## テスト

### 単体テスト

```bash
uv run python -m unittest tests/test_security.py
```

### ローカル疎通確認

アプリを起動した状態で:

```bash
uv run python tests/test_all.py
```

必要な環境変数:

- `GAKUJO_USERNAME`
- `GAKUJO_PASSWORD`
- `GAKUJO_TOTP_SECRET`

### GPT 経由の手動確認スクリプト

`tests/chat_with_gakujo.py` は OpenAI API を使った補助スクリプトです。実行には `OPENAI_API_KEY` が必要です。

## 技術スタック

- FastAPI
- httpx
- BeautifulSoup4 + lxml
- pyotp
- cryptography
- pydantic-settings

## ライセンス

[MIT](LICENSE)
