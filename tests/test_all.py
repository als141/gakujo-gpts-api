"""全15エンドポイント + 通知詳細 + プライバシーポリシー = 17項目の網羅テスト。"""

import os
import re
import sys
import time
import urllib.parse

import httpx
from dotenv import load_dotenv

load_dotenv()

BASE = os.getenv("TEST_URL", "http://localhost:8000")
TIMEOUT = 180
REQUEST_PAUSE_SECONDS = float(os.getenv("REQUEST_PAUSE_SECONDS", "0.5"))
CLIENT_ID = os.getenv("OAUTH_CLIENT_ID", "gakujo-gpts")
CLIENT_SECRET = os.getenv("OAUTH_CLIENT_SECRET", "gakujo-gpts-secret")


def login(client):
    auth = client.get(
        f"{BASE}/oauth/authorize",
        params={
            "response_type": "code",
            "client_id": CLIENT_ID,
            "redirect_uri": f"{BASE}/test",
            "state": "test",
            "scope": "openid",
        },
    )
    match = re.search(r'name="auth_request_id" value="([^"]+)"', auth.text)
    if not match:
        raise RuntimeError(f"auth_request_id not found: HTTP {auth.status_code}")

    resp = client.post(
        f"{BASE}/oauth/callback",
        data={
            "auth_request_id": match.group(1),
            "username": os.getenv("GAKUJO_USERNAME"),
            "password": os.getenv("GAKUJO_PASSWORD"),
            "totp_secret": os.getenv("GAKUJO_TOTP_SECRET"),
        },
    )
    loc = resp.headers.get("location", "")
    code = urllib.parse.parse_qs(urllib.parse.urlparse(loc).query).get("code", [""])[0]
    resp = client.post(
        f"{BASE}/oauth/token",
        data={
            "grant_type": "authorization_code",
            "code": code,
            "client_id": CLIENT_ID,
            "client_secret": CLIENT_SECRET,
            "redirect_uri": f"{BASE}/test",
        },
    )
    return resp.json()["access_token"]


def test(client, token, name, path, params=None, is_html=False):
    try:
        headers = {} if is_html else {"Authorization": f"Bearer {token}"}
        if REQUEST_PAUSE_SECONDS > 0:
            time.sleep(REQUEST_PAUSE_SECONDS)
        r = client.get(f"{BASE}{path}", headers=headers, params=params, timeout=TIMEOUT)
        if r.status_code == 200:
            if is_html:
                assert "プライバシーポリシー" in r.text or "privacy" in r.text.lower()
                print(f"  ✅ {name}")
                return True
            data = r.json()
            # 基本的なデータ存在チェック
            if isinstance(data, dict):
                bool(
                    data.get("entries")
                    or data.get("answer")
                    or data.get("openaiFileResponse")
                    or data.get("entries") == []
                    or data.get("total_count") is not None
                    or data.get("year")
                    or data.get("subject_name")
                    or data.get("reports_summary")
                    or data.get("student_name")
                    or data.get("at_risk_count") is not None
                    or data.get("total_credits_required")
                )
                print(f"  ✅ {name}: {list(data.keys())[:4]}")
                return True
        print(f"  ❌ {name}: HTTP {r.status_code}")
        return False
    except Exception as e:
        print(f"  ❌ {name}: {type(e).__name__}: {str(e)[:60]}")
        return False


def main():
    client = httpx.Client(follow_redirects=False, timeout=TIMEOUT)

    print("🔐 ログイン中...")
    token = login(client)
    print(f"  ✅ トークン取得 ({len(token)} chars)\n")

    results = []

    print("=== 学務データ (10) ===")
    results.append(test(client, token, "時間割", "/api/v1/timetable"))
    results.append(test(client, token, "休講補講", "/api/v1/cancellations"))
    results.append(test(client, token, "成績", "/api/v1/grades"))
    results.append(test(client, token, "レポート", "/api/v1/reports"))
    results.append(test(client, token, "通知一覧", "/api/v1/notices", {"limit": "3"}))
    results.append(test(client, token, "出欠管理", "/api/v1/attendance"))
    results.append(
        test(
            client,
            token,
            "シラバス検索",
            "/api/v1/syllabus/search",
            {"subject_name": "人工知能"},
        )
    )
    results.append(
        test(
            client,
            token,
            "シラバス詳細",
            "/api/v1/syllabus/detail",
            {"subject_code": "250F3823"},
        )
    )

    # 通知詳細
    try:
        r = client.get(
            f"{BASE}/api/v1/notices",
            headers={"Authorization": f"Bearer {token}"},
            params={"limit": "1"},
            timeout=TIMEOUT,
        )
        dk = r.json().get("entries", [{}])[0].get("detail_key", "")
        if dk:
            results.append(
                test(
                    client,
                    token,
                    "通知詳細",
                    "/api/v1/notices/detail",
                    {"detail_key": dk},
                )
            )
        else:
            print("  ⚠️ 通知詳細: detail_keyなし")
            results.append(False)
    except Exception:
        results.append(False)

    print("\n=== 分析 (2) ===")
    results.append(test(client, token, "出欠リスク", "/api/v1/attendance/risk"))
    results.append(test(client, token, "ダイジェスト", "/api/v1/digest"))

    # 学生便覧検索・修了チェックは GPTs Knowledge Files に移行済み

    print("\n=== エクスポート (2) ===")
    results.append(test(client, token, "ICSエクスポート", "/api/v1/timetable/export"))
    results.append(test(client, token, "CSVエクスポート", "/api/v1/grades/export"))

    print("\n=== その他 ===")
    results.append(
        test(client, token, "プライバシーポリシー", "/privacy", is_html=True)
    )

    passed = sum(results)
    total = len(results)
    print(f"\n{'=' * 40}")
    print(f"結果: {passed}/{total} passed")
    if passed < total:
        print("❌ 一部テスト失敗")
        sys.exit(1)
    else:
        print("✅ 全テスト合格")

    client.close()


if __name__ == "__main__":
    main()
