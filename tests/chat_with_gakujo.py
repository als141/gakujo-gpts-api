"""GPTsとほぼ同じ体験でAPIをテストする対話型スクリプト。

OpenAI Responses API + function calling で、学務情報システムAPIを
GPTs Custom Actions と同じように呼び出す。

使い方:
  uv run python tests/chat_with_gakujo.py
  uv run python tests/chat_with_gakujo.py --url https://gakujo-gpts-api-xxx.run.app
  uv run python tests/chat_with_gakujo.py --model gpt-5.4

セットアップ:
  1. .env に OPENAI_API_KEY を設定
  2. .env に GAKUJO_USERNAME, GAKUJO_PASSWORD, GAKUJO_TOTP_SECRET を設定
  3. サーバーを起動: uv run uvicorn main:app --host 0.0.0.0 --port 8000
"""

import json
import os
import sys
import urllib.parse

import httpx
from dotenv import load_dotenv
from openai import OpenAI

load_dotenv()

BASE_URL = "http://localhost:8000"
MODEL = "gpt-5.4"

# ─── APIツール定義 (GPTs Custom Actionsと同等) ──────

TOOLS = [
    {
        "type": "function",
        "name": "get_digest",
        "description": "朝のブリーフィング。未提出レポート、休講、出欠リスク、最新通知、修得単位を一括取得。",
        "parameters": {"type": "object", "properties": {}, "required": []},
    },
    {
        "type": "function",
        "name": "get_timetable",
        "description": "時間割を取得。教室名・科目コード・集中講義含む。",
        "parameters": {"type": "object", "properties": {}, "required": []},
    },
    {
        "type": "function",
        "name": "get_grades",
        "description": "成績一覧を取得。得点・合否・担当教員・報告日含む全フィールド。",
        "parameters": {"type": "object", "properties": {}, "required": []},
    },
    {
        "type": "function",
        "name": "get_reports",
        "description": "レポート・小テスト一覧を取得。種別・緊急度・曜日時限付き。",
        "parameters": {"type": "object", "properties": {}, "required": []},
    },
    {
        "type": "function",
        "name": "get_notices",
        "description": "連絡通知一覧を取得。デフォルト20件。",
        "parameters": {
            "type": "object",
            "properties": {
                "limit": {
                    "type": "integer",
                    "description": "最大取得件数 (デフォルト20、0で全件)",
                }
            },
            "required": [],
        },
    },
    {
        "type": "function",
        "name": "get_notice_detail",
        "description": "連絡通知の本文を取得。一覧で取得したdetail_keyを指定。",
        "parameters": {
            "type": "object",
            "properties": {
                "detail_key": {
                    "type": "string",
                    "description": "一覧で取得したdetail_key",
                }
            },
            "required": ["detail_key"],
        },
    },
    {
        "type": "function",
        "name": "get_cancellations",
        "description": "休講・補講情報を取得。",
        "parameters": {"type": "object", "properties": {}, "required": []},
    },
    {
        "type": "function",
        "name": "get_attendance",
        "description": "出欠情報を取得。各回の出欠記録・担当教員・アラート条件付き。",
        "parameters": {"type": "object", "properties": {}, "required": []},
    },
    {
        "type": "function",
        "name": "get_attendance_risk",
        "description": "出欠リスクを分析。欠席率によるdanger/warning/safe判定。",
        "parameters": {"type": "object", "properties": {}, "required": []},
    },
    {
        "type": "function",
        "name": "search_syllabus",
        "description": "シラバスを検索。科目名・教員名・キーワードで検索可能。",
        "parameters": {
            "type": "object",
            "properties": {
                "subject_name": {
                    "type": "string",
                    "description": "科目名 (部分一致)",
                },
                "instructor": {
                    "type": "string",
                    "description": "担当教員名 (部分一致)",
                },
                "keyword": {
                    "type": "string",
                    "description": "キーワード (部分一致)",
                },
            },
            "required": [],
        },
    },
    {
        "type": "function",
        "name": "get_syllabus_detail",
        "description": "シラバス詳細を取得。科目概要・到達目標・授業計画全回分を返す。",
        "parameters": {
            "type": "object",
            "properties": {
                "subject_code": {
                    "type": "string",
                    "description": "開講番号 (例: 250F3823)",
                },
            },
            "required": ["subject_code"],
        },
    },
    {
        "type": "function",
        "name": "export_timetable_ics",
        "description": "時間割をICSカレンダーファイルでエクスポート。",
        "parameters": {"type": "object", "properties": {}, "required": []},
    },
    {
        "type": "function",
        "name": "export_grades_csv",
        "description": "成績をCSVファイルでエクスポート。",
        "parameters": {"type": "object", "properties": {}, "required": []},
    },
    # search_handbook, check_graduation は GPTs Knowledge Files に移行済み
]

# ─── ツール名 → APIパスのマッピング ──────

TOOL_TO_API = {
    "get_digest": "/api/v1/digest",
    "get_timetable": "/api/v1/timetable",
    "get_grades": "/api/v1/grades",
    "get_reports": "/api/v1/reports",
    "get_notices": "/api/v1/notices",
    "get_notice_detail": "/api/v1/notices/detail",
    "get_cancellations": "/api/v1/cancellations",
    "get_attendance": "/api/v1/attendance",
    "get_attendance_risk": "/api/v1/attendance/risk",
    "search_syllabus": "/api/v1/syllabus/search",
    "export_timetable_ics": "/api/v1/timetable/export",
    "export_grades_csv": "/api/v1/grades/export",
    "get_syllabus_detail": "/api/v1/syllabus/detail",
    # search_handbook, check_graduation は GPTs Knowledge Files に移行済み
}

SYSTEM_PROMPT = """あなたは「新大学務AIアシスタント」です。新潟大学の学務情報システム (CampusSquare) と連携して、学生の学務情報を取得・分析・提案する頼れるAIアシスタントです。

- データを単に表示するだけでなく、分析・提案・アドバイスを加えてください
- 親しみやすいが信頼できる「先輩」のようなトーンで応答してください
- 最初の質問にはまずget_digestを使って全体像を把握してください
- 成績は得点(score)・合否(pass_fail)を含む全フィールドが取得できます
- 成績にはcredits_by_category(科目区分別集計)とcredits_by_required_type(必選区分別集計)が含まれます
- 出欠はsession_recordsに各回の出欠記録(出/欠/遅)があります
- 通知の本文を見る場合はget_notice_detailを使ってください
- シラバスはsearch_syllabusで科目名・教員名で検索できます
- 卒業/修了要件の確認にはget_gradesで成績を取得し、credits_by_categoryとcredits_by_required_typeを基に分析してください
- 注: GPTs版ではKnowledge Filesの学生便覧と照合しますが、このテスト環境では便覧データがないため成績集計のみの分析になります"""


# ─── OAuthトークン取得 ──────


def get_api_token(http: httpx.Client) -> str:
    """OAuthフローでAPIトークンを取得。"""
    username = os.getenv("GAKUJO_USERNAME", "")
    password = os.getenv("GAKUJO_PASSWORD", "")
    totp_secret = os.getenv("GAKUJO_TOTP_SECRET", "")

    resp = http.post(
        f"{BASE_URL}/oauth/callback",
        data={
            "username": username,
            "password": password,
            "totp_secret": totp_secret,
            "redirect_uri": f"{BASE_URL}/test",
            "state": "test",
            "scope": "openid",
        },
        follow_redirects=False,
    )
    location = resp.headers.get("location", "")
    code = urllib.parse.parse_qs(urllib.parse.urlparse(location).query).get(
        "code", [""]
    )[0]

    resp = http.post(
        f"{BASE_URL}/oauth/token",
        data={
            "grant_type": "authorization_code",
            "code": code,
            "client_id": os.getenv("OAUTH_CLIENT_ID", "gakujo-gpts"),
            "client_secret": os.getenv("OAUTH_CLIENT_SECRET", "gakujo-gpts-secret"),
        },
    )
    return resp.json()["access_token"]


# ─── ツール実行 ──────


def execute_tool(http: httpx.Client, token: str, tool_name: str, args: dict) -> str:
    """APIエンドポイントを呼び出してJSON文字列を返す。"""
    path = TOOL_TO_API.get(tool_name, "")
    if not path:
        return json.dumps({"error": f"Unknown tool: {tool_name}"})

    resp = http.get(
        f"{BASE_URL}{path}",
        params=args if args else None,
        headers={"Authorization": f"Bearer {token}"},
        timeout=60,
    )
    result = resp.json()

    # レスポンスが大きすぎる場合は要約
    result_str = json.dumps(result, ensure_ascii=False)
    if len(result_str) > 8000:
        # entriesを最大5件に制限
        if isinstance(result, dict):
            for key in ("entries", "urgent_reports", "recent_notices"):
                if (
                    key in result
                    and isinstance(result[key], list)
                    and len(result[key]) > 5
                ):
                    result[key] = result[key][:5]
                    result[f"{key}_truncated"] = True
        result_str = json.dumps(result, ensure_ascii=False)

    return result_str


# ─── 対話ループ ──────


def chat_loop():
    openai_client = OpenAI()
    http = httpx.Client(follow_redirects=False, timeout=60)

    print("🔐 学務情報システムにログイン中...")
    token = get_api_token(http)
    print("✅ ログイン成功\n")

    print(f"🤖 新大学務AIアシスタント (model: {MODEL})")
    print("   質問を入力してください。終了: quit/exit\n")

    conversation = [{"role": "system", "content": SYSTEM_PROMPT}]

    while True:
        try:
            user_input = input("あなた > ").strip()
        except (EOFError, KeyboardInterrupt):
            print("\n👋 終了します")
            break

        if not user_input:
            continue
        if user_input.lower() in ("quit", "exit", "q"):
            print("👋 終了します")
            break

        conversation.append({"role": "user", "content": user_input})

        # Responses API でツール呼び出しループ
        while True:
            response = openai_client.responses.create(
                model=MODEL,
                instructions=SYSTEM_PROMPT,
                input=conversation,
                tools=TOOLS,
            )

            # ツール呼び出しがあるか確認
            tool_calls = [
                item for item in response.output if item.type == "function_call"
            ]

            if not tool_calls:
                # テキスト応答を取得
                text_output = response.output_text
                print(f"\n🎓 アシスタント > {text_output}\n")
                conversation.extend(response.output)
                break

            # ツール呼び出しを実行し、結果をinputに追加
            # Responses API: output全体をinputに追加 → tool結果を追加
            conversation.extend(response.output)

            for tc in tool_calls:
                tool_name = tc.name
                tool_args = json.loads(tc.arguments) if tc.arguments else {}
                print(f"  🔧 {tool_name}({tool_args})")

                result = execute_tool(http, token, tool_name, tool_args)

                conversation.append(
                    {
                        "type": "function_call_output",
                        "call_id": tc.call_id,
                        "output": result,
                    }
                )

    http.close()


def main():
    global BASE_URL, MODEL
    args = sys.argv[1:]

    if "--url" in args:
        idx = args.index("--url")
        BASE_URL = args[idx + 1].rstrip("/")
        args = args[:idx] + args[idx + 2 :]

    if "--model" in args:
        idx = args.index("--model")
        MODEL = args[idx + 1]
        args = args[:idx] + args[idx + 2 :]

    if not os.getenv("OPENAI_API_KEY"):
        print("❌ .env に OPENAI_API_KEY が設定されていません")
        sys.exit(1)

    if not os.getenv("GAKUJO_USERNAME"):
        print("❌ .env に GAKUJO_USERNAME が設定されていません")
        sys.exit(1)

    print(f"🌐 API: {BASE_URL}")
    print(f"🧠 Model: {MODEL}\n")
    chat_loop()


if __name__ == "__main__":
    main()
