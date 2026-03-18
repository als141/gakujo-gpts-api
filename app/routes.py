"""GPTs向けAPIエンドポイント。

OAuth認証方式: Authorization: Bearer {access_token} ヘッダーで認証。
access_token は OAuth フロー (/oauth/authorize → /oauth/token) で取得。
"""

import logging
from typing import Annotated

import httpx
from fastapi import APIRouter, HTTPException, Query, Request

from app.models import (
    AttendanceResponse,
    AttendanceRiskResponse,
    CancellationListResponse,
    DigestResponse,
    ErrorResponse,
    FileExportResponse,
    GradeResponse,
    NoticeDetailResponse,
    NoticeListResponse,
    ReportListResponse,
    SyllabusDetailResponse,
    SyllabusSearchResponse,
    TimetableResponse,
)
from app.scraper import CampusSquareScraper

logger = logging.getLogger(__name__)

router = APIRouter()


def _raise_backend_error(operation: str, exc: Exception) -> None:
    if isinstance(exc, HTTPException):
        raise exc
    if isinstance(exc, ValueError):
        logger.warning("%s: invalid request (%s)", operation, type(exc).__name__)
        raise HTTPException(status_code=400, detail="リクエストが不正です。")
    if isinstance(exc, httpx.TimeoutException):
        logger.warning("%s: timeout", operation)
        raise HTTPException(
            status_code=504,
            detail="学務情報システムの応答がタイムアウトしました。時間を置いて再試行してください。",
        )

    logger.error("%s failed (%s)", operation, type(exc).__name__)
    raise HTTPException(
        status_code=502,
        detail="学務情報システムからのデータ取得に失敗しました。再ログインして再試行してください。",
    )


async def _resolve_scraper(request: Request) -> CampusSquareScraper:
    """Authorization: Bearer ヘッダーからスクレイパーを解決。

    ウォームコンテナ: メモリキャッシュから即座に返す
    コールドスタート/キャッシュ失効後: 401 を返して再ログインを要求
    """
    from app.oauth import get_or_create_session

    auth = request.headers.get("authorization", "")
    if not auth.startswith("Bearer "):
        raise HTTPException(
            status_code=401,
            detail="Authorization: Bearer ヘッダーが必要です。先にOAuthログインしてください。",
        )
    token = auth[7:]
    sess = await get_or_create_session(token)
    return sess["scraper"]


# ─── データ取得エンドポイント ──────────────────────────


@router.get(
    "/timetable",
    response_model=TimetableResponse,
    responses={401: {"model": ErrorResponse}, 500: {"model": ErrorResponse}},
    summary="時間割を取得",
    description=(
        "現在の履修時間割を取得します。"
        "曜日・時限ごとの科目名、担当教員、教室情報を返します。"
    ),
    tags=["学務データ"],
)
async def get_timetable(
    request: Request,
    year: Annotated[int, Query(description="年度 (例: 2025)")] = 2025,
    semester: Annotated[str, Query(description="学期 (前期/後期)")] = "前期",
) -> TimetableResponse:
    scraper = await _resolve_scraper(request)
    try:
        await scraper.client.ensure_logged_in()
        return await scraper.get_timetable(year=year, semester=semester)
    except Exception as e:
        _raise_backend_error("時間割取得", e)


@router.get(
    "/cancellations",
    response_model=CancellationListResponse,
    responses={500: {"model": ErrorResponse}},
    summary="休講・補講情報を取得",
    description=(
        "休講・補講の一覧を取得します。"
        "日付、時限、科目名、種別（休講/補講/変更）、備考を返します。"
    ),
    tags=["学務データ"],
)
async def get_cancellations(request: Request) -> CancellationListResponse:
    scraper = await _resolve_scraper(request)
    try:
        await scraper.client.ensure_logged_in()
        return await scraper.get_cancellations()
    except Exception as e:
        _raise_backend_error("休講情報取得", e)


@router.get(
    "/grades",
    response_model=GradeResponse,
    responses={500: {"model": ErrorResponse}},
    summary="成績情報を取得",
    description=(
        "成績一覧を取得します。"
        "科目名、単位数、評価（秀/優/良/可/不可）、GP値、GPA、修得単位合計を返します。"
    ),
    tags=["学務データ"],
)
async def get_grades(request: Request) -> GradeResponse:
    scraper = await _resolve_scraper(request)
    try:
        await scraper.client.ensure_logged_in()
        return await scraper.get_grades()
    except Exception as e:
        _raise_backend_error("成績取得", e)


@router.get(
    "/reports",
    response_model=ReportListResponse,
    responses={500: {"model": ErrorResponse}},
    summary="レポート・小テスト一覧を取得",
    description=(
        "レポート・小テスト・アンケートの一覧を取得します。"
        "タイトル、科目名、提出状態、提出期限を返します。"
        "未提出のレポートの確認に便利です。"
    ),
    tags=["学務データ"],
)
async def get_reports(request: Request) -> ReportListResponse:
    scraper = await _resolve_scraper(request)
    try:
        await scraper.client.ensure_logged_in()
        return await scraper.get_reports()
    except Exception as e:
        _raise_backend_error("レポート取得", e)


@router.get(
    "/notices",
    response_model=NoticeListResponse,
    responses={500: {"model": ErrorResponse}},
    summary="連絡通知一覧を取得",
    description=(
        "連絡通知（お知らせ・メッセージ）の一覧を取得します。"
        "タイトル、送信者、日付を返します。"
        "デフォルトで最新20件を返します。全件取得するにはlimit=0を指定してください。"
    ),
    tags=["学務データ"],
)
async def get_notices(
    request: Request,
    limit: Annotated[
        int, Query(description="最大取得件数 (デフォルト20、0で全件)")
    ] = 20,
) -> NoticeListResponse:
    scraper = await _resolve_scraper(request)
    try:
        await scraper.client.ensure_logged_in()
        return await scraper.get_notices(limit=limit)
    except Exception as e:
        _raise_backend_error("連絡通知取得", e)


@router.get(
    "/notices/detail",
    response_model=NoticeDetailResponse,
    responses={500: {"model": ErrorResponse}},
    summary="連絡通知の本文を取得",
    description=(
        "連絡通知の本文（詳細）を取得します。"
        "一覧取得時に返される detail_key を指定してください。"
    ),
    tags=["学務データ"],
)
async def get_notice_detail(
    request: Request,
    detail_key: Annotated[str, Query(description="一覧で取得した detail_key")],
) -> NoticeDetailResponse:
    scraper = await _resolve_scraper(request)
    try:
        await scraper.client.ensure_logged_in()
        return await scraper.get_notice_detail(detail_key=detail_key)
    except Exception as e:
        _raise_backend_error("連絡通知詳細取得", e)


@router.get(
    "/attendance",
    response_model=AttendanceResponse,
    responses={500: {"model": ErrorResponse}},
    summary="出欠情報を取得",
    description=(
        "出欠管理情報を取得します。"
        "科目ごとの出席回数、欠席回数、遅刻回数、授業実施回数を返します。"
    ),
    tags=["学務データ"],
)
async def get_attendance(request: Request) -> AttendanceResponse:
    scraper = await _resolve_scraper(request)
    try:
        await scraper.client.ensure_logged_in()
        return await scraper.get_attendance()
    except Exception as e:
        _raise_backend_error("出欠取得", e)


@router.get(
    "/attendance/risk",
    response_model=AttendanceRiskResponse,
    responses={500: {"model": ErrorResponse}},
    summary="出欠リスクを分析",
    description=(
        "科目ごとの欠席率を計算し、単位取得リスクを判定します。"
        "欠席率1/3超: danger (単位取得危険)、1/4超: warning、それ以下: safe。"
    ),
    tags=["分析"],
)
async def get_attendance_risk(request: Request) -> AttendanceRiskResponse:
    scraper = await _resolve_scraper(request)
    try:
        await scraper.client.ensure_logged_in()
        return await scraper.get_attendance_risk()
    except Exception as e:
        _raise_backend_error("出欠リスク分析", e)


@router.get(
    "/digest",
    response_model=DigestResponse,
    responses={500: {"model": ErrorResponse}},
    summary="朝のブリーフィング / 週間ダイジェスト",
    description=(
        "全データソースを統合したダイジェストを取得します。"
        "未提出レポート、休講情報、出欠リスク、最新通知、修得単位を一括で返します。"
        "毎朝の確認や、今週のやるべきことの把握に最適です。"
    ),
    tags=["分析"],
)
async def get_digest(request: Request) -> DigestResponse:
    scraper = await _resolve_scraper(request)
    try:
        await scraper.client.ensure_logged_in()
        return await scraper.get_digest()
    except Exception as e:
        _raise_backend_error("ダイジェスト取得", e)


@router.get(
    "/timetable/export",
    response_model=FileExportResponse,
    responses={500: {"model": ErrorResponse}},
    summary="時間割をICSカレンダーファイルでエクスポート",
    description=(
        "時間割と休講情報を統合した .ics カレンダーファイルを返します。"
        "Google Calendar や Apple Calendar にインポートできます。"
        "openaiFileResponse 形式で返却されるため、ChatGPT上でファイルダウンロードが可能です。"
    ),
    tags=["エクスポート"],
)
async def export_timetable(
    request: Request,
    year: Annotated[int, Query(description="年度 (例: 2025)")] = 2025,
    semester: Annotated[str, Query(description="学期 (前期/後期)")] = "前期",
) -> FileExportResponse:
    scraper = await _resolve_scraper(request)
    try:
        await scraper.client.ensure_logged_in()
        return await scraper.export_timetable_ics(year=year, semester=semester)
    except Exception as e:
        _raise_backend_error("ICSエクスポート", e)


@router.get(
    "/grades/export",
    response_model=FileExportResponse,
    responses={500: {"model": ErrorResponse}},
    summary="成績をCSVファイルでエクスポート",
    description=(
        "成績一覧を .csv ファイルとして返します。"
        "Excel や Google Sheets で開けます。"
        "openaiFileResponse 形式で返却されます。"
    ),
    tags=["エクスポート"],
)
async def export_grades(request: Request) -> FileExportResponse:
    scraper = await _resolve_scraper(request)
    try:
        await scraper.client.ensure_logged_in()
        return await scraper.export_grades_csv()
    except Exception as e:
        _raise_backend_error("CSV成績エクスポート", e)


@router.get(
    "/syllabus/search",
    response_model=SyllabusSearchResponse,
    responses={500: {"model": ErrorResponse}},
    summary="シラバスを検索",
    description=(
        "シラバス（授業概要）を科目名、担当教員名、キーワードで検索します。"
        "少なくとも1つの検索条件を指定してください。"
    ),
    tags=["学務データ"],
)
async def search_syllabus(
    request: Request,
    subject_name: Annotated[str, Query(description="科目名 (部分一致)")] = "",
    instructor: Annotated[str, Query(description="担当教員名 (部分一致)")] = "",
    keyword: Annotated[str, Query(description="キーワード (部分一致)")] = "",
    year: Annotated[str, Query(description="年度 (例: 2025)")] = "2025",
) -> SyllabusSearchResponse:
    scraper = await _resolve_scraper(request)
    try:
        await scraper.client.ensure_logged_in()
        return await scraper.search_syllabus(
            subject_name=subject_name,
            instructor=instructor,
            keyword=keyword,
            year=year,
        )
    except Exception as e:
        _raise_backend_error("シラバス検索", e)


@router.get(
    "/syllabus/detail",
    response_model=SyllabusDetailResponse,
    responses={500: {"model": ErrorResponse}},
    summary="シラバス詳細を取得",
    description=(
        "開講番号を指定してシラバスの詳細情報を取得します。"
        "科目概要、到達目標、授業計画（全回分）、担当教員、教室等を返します。"
    ),
    tags=["学務データ"],
)
async def get_syllabus_detail(
    request: Request,
    subject_code: Annotated[str, Query(description="開講番号 (例: 250F3823)")],
    year: Annotated[str, Query(description="年度 (例: 2025)")] = "2025",
) -> SyllabusDetailResponse:
    scraper = await _resolve_scraper(request)
    try:
        await scraper.client.ensure_logged_in()
        return await scraper.get_syllabus_detail(subject_code=subject_code, year=year)
    except Exception as e:
        _raise_backend_error("シラバス詳細取得", e)

    # 学生便覧検索・修了要件チェックは GPTs Knowledge Files に移行済み
    # GPTs が Knowledge Files + grades API の集計データを使って自力で修了判定する
