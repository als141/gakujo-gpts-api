"""CampusSquare HTMLパーサー / スクレイピングロジック。

Playwright DOM探索 (2026-03-15) に基づき、CampusSquareの全フィールドを正確にパース。

funcIdマップ:
  RSW0001000 - 履修登録・登録状況照会 (★時間割の最適ソース)
  KHW0001100 - 休講補講参照
  SIW0001300 - 単位修得状況照会 (成績)
  ENW3411100 - レポート・小テスト・アンケート提出
  KJW0001100 - 連絡通知
  AAW3411000 - 出欠管理
  SYW0001000 - シラバス参照
  DPW3412000 - ディプロマサプリメント参照
"""

import logging
import re
import time

from bs4 import BeautifulSoup, Tag

from app.client import CampusSquareClient
from app.config import settings
from app.models import (
    AttendanceEntry,
    AttendanceResponse,
    AttendanceRiskEntry,
    AttendanceRiskResponse,
    CancellationEntry,
    CancellationListResponse,
    CreditSummaryItem,
    DigestResponse,
    FileExportResponse,
    FileItem,
    GradeEntry,
    GradeResponse,
    NoticeDetailResponse,
    NoticeEntry,
    NoticeListResponse,
    ReportEntry,
    ReportListResponse,
    SyllabusDetailResponse,
    SyllabusSearchResponse,
    SyllabusSearchResult,
    TimetableEntry,
    TimetableIntensiveEntry,
    TimetableResponse,
)

logger = logging.getLogger(__name__)

def _text(tag: Tag | None) -> str:
    if tag is None:
        return ""
    return tag.get_text(strip=True)


def _extract_flow_execution_key(soup: BeautifulSoup) -> str:
    inp = soup.find("input", {"name": "_flowExecutionKey"})
    return inp["value"] if inp else ""


def _find_data_table(soup: BeautifulSoup, header_keywords: list[str]) -> Tag | None:
    """ヘッダーキーワードに合致するテーブルを探す。"""
    for table in soup.find_all("table"):
        first_row = table.find("tr")
        if not first_row:
            continue
        cells = first_row.find_all(["th", "td"])
        header_text = " ".join(_text(c) for c in cells)
        if all(kw in header_text for kw in header_keywords):
            return table
    return None


def _safe_int(text: str) -> int:
    match = re.search(r"(\d+)", text)
    return int(match.group(1)) if match else 0


def _compute_urgency(deadline_end: str, status: str) -> tuple[int | None, str]:
    """提出期限と状態から緊急度を計算。"""
    if status == "提出済":
        return None, "submitted"
    if not deadline_end:
        return None, ""
    from datetime import datetime

    try:
        for fmt in ("%Y/%m/%d %H:%M", "%Y/%m/%d %H", "%Y/%m/%d"):
            try:
                dt = datetime.strptime(deadline_end.strip(), fmt)
                break
            except ValueError:
                continue
        else:
            return None, ""
        delta = (dt - datetime.now()).days
        if delta < 0:
            return delta, "overdue"
        elif delta <= 3:
            return delta, "critical"
        elif delta <= 7:
            return delta, "warning"
        else:
            return delta, "safe"
    except Exception:
        return None, ""


class CampusSquareScraper:
    """CampusSquareのデータスクレイピングロジック。

    レスポンスキャッシュは `RESPONSE_CACHE_TTL_SECONDS` が正のときのみ有効化する。
    デフォルトは 0 秒で、学生データをアプリ側に保持しない。
    """

    def __init__(self, client: CampusSquareClient):
        self.client = client
        self._cache: dict[str, tuple[float, object]] = {}

    def _get_cache(self, key: str) -> object | None:
        ttl = settings.response_cache_ttl_seconds
        if ttl <= 0:
            return None
        if key in self._cache:
            ts, data = self._cache[key]
            if time.time() - ts < ttl:
                logger.debug("キャッシュヒット: %s", key)
                return data
            del self._cache[key]
        return None

    def _set_cache(self, key: str, data: object) -> None:
        if settings.response_cache_ttl_seconds <= 0:
            return
        self._cache[key] = (time.time(), data)

    # ─── 時間割 (履修登録ページから取得) ──────────────

    async def get_timetable(
        self, year: int = 2025, semester: str = "第2学期"
    ) -> TimetableResponse:
        """履修登録ページから時間割を取得。教室名・科目コード付き。"""
        cached = self._get_cache("timetable")
        if cached:
            return cached

        html = await self.client.access_web_flow("RSW0001000-flow")
        soup = BeautifulSoup(html, "lxml")

        # 学生情報テーブルから年度・学期・件数を抽出
        info_table = _find_data_table(soup, ["氏名", "在籍番号"])
        year_str = ""
        sem_str = ""
        count = 0
        if info_table:
            for row in info_table.find_all("tr"):
                cells = row.find_all(["th", "td"])
                for i, cell in enumerate(cells):
                    t = _text(cell)
                    if "年度・学期" in t and i + 1 < len(cells):
                        val = _text(cells[i + 1])
                        parts = val.split()
                        if parts:
                            year_str = parts[0]
                            sem_str = " ".join(parts[1:])
                    if "件数" in t and i + 1 < len(cells):
                        count = _safe_int(_text(cells[i + 1]))

        # 時間割グリッドをパース
        # RSW0001000 のグリッドは深くネストされたtable構造
        # 正しいアプローチ: 科目コードリンク (href="#") を探してその親セルから情報抽出
        entries: list[TimetableEntry] = []
        day_short = ["月", "火", "水", "木", "金", "土"]

        grid_table = _find_data_table(soup, ["月曜日", "火曜日"])
        if grid_table:
            # グリッドの直接の子rowだけ取得 (外側のテーブル)
            tbody = grid_table.find("tbody") or grid_table
            outer_rows = tbody.find_all("tr", recursive=False)

            for row in outer_rows:
                outer_cells = row.find_all("td", recursive=False)
                if len(outer_cells) < 7:
                    continue

                # 最初のセルから時限を取得
                # ネストされたテーブル内に「X 限」がある場合もあるので全テキストで検索
                first_full = outer_cells[0].get_text()
                period_match = re.search(r"(\d+)\s*限", first_full)
                if not period_match:
                    # 数字だけの場合 (「1」のみで「限」がない)
                    first_text = _text(outer_cells[0])
                    if first_text.isdigit():
                        period = int(first_text)
                    else:
                        continue
                else:
                    period = int(period_match.group(1))

                # 曜日列 (1-6: 月-土)
                for col_idx in range(1, min(len(outer_cells), 7)):
                    cell = outer_cells[col_idx]
                    # 科目コードリンクを探す (href="#")
                    code_link = cell.find("a", href="#")
                    if not code_link:
                        continue
                    code = _text(code_link)
                    if not code:
                        continue

                    # セル内の全テキストから科目名・教室・単位数を抽出
                    # \xa0 (NBSP) で区切られている場合があるので正規表現で分割
                    full = cell.get_text(separator="\n", strip=True)
                    lines = [
                        ln.strip()
                        for ln in re.split(r"[\n\xa0]+", full)
                        if ln.strip()
                        and ln.strip() not in ("追加", "削除", "シラバス", code)
                    ]
                    name = ""
                    room = ""
                    credits = ""
                    for ln in lines:
                        if "単位" in ln:
                            credits = ln.replace("単位", "").strip()
                        elif not name:
                            name = ln
                        elif not room:
                            room = ln

                    if name and col_idx - 1 < len(day_short):
                        entries.append(
                            TimetableEntry(
                                day_of_week=day_short[col_idx - 1],
                                period=period,
                                subject_name=name,
                                subject_code=code,
                                room=room if room != "別途お知らせ" else "",
                                credits=credits,
                            )
                        )

        # 集中講義をパース (「集中講義など」セクション内の開講番号リンクを探す)
        intensive: list[TimetableIntensiveEntry] = []
        intensive_header = soup.find("th", string=re.compile("集中講義"))
        if intensive_header:
            intensive_section = intensive_header.find_parent("table")
            if intensive_section:
                for row in intensive_section.find_all("tr"):
                    cells = row.find_all("td")
                    if len(cells) < 5:
                        continue
                    code_text = _text(cells[0])
                    if not code_text or code_text in (
                        "開講番号",
                        "登録されていません",
                    ):
                        continue
                    # 科目コードっぽいかチェック (数字+英字)
                    if not re.match(r"\d+\w+", code_text):
                        continue
                    intensive.append(
                        TimetableIntensiveEntry(
                            subject_code=code_text,
                            subject_name=_text(cells[1]),
                            room=_text(cells[2]),
                            period_type=_text(cells[3]),
                            credits=_text(cells[4]).replace("単位", ""),
                            note=_text(cells[5]) if len(cells) > 5 else "",
                        )
                    )

        result = TimetableResponse(
            year=year_str,
            semester=sem_str,
            course_count=count,
            entries=entries,
            intensive_courses=intensive,
        )
        self._set_cache("timetable", result)
        return result

    # ─── 休講・補講情報 ──────────────────────────────

    async def get_cancellations(self) -> CancellationListResponse:
        """休講・補講情報を一覧形式で取得。"""
        cached = self._get_cache("cancellations")
        if cached:
            return cached

        html = await self.client.access_web_flow("KHW0001100-flow")
        soup = BeautifulSoup(html, "lxml")
        fek = _extract_flow_execution_key(soup)

        html = await self.client.submit_web_flow(
            "campussquare.do",
            {
                "_flowExecutionKey": fek,
                "_eventId_search": "",
                "dispType": "list",
                "dispData": "all",
            },
        )
        soup = BeautifulSoup(html, "lxml")

        table = _find_data_table(soup, ["日付", "科目"])
        if table is None:
            result = CancellationListResponse(entries=[], total_count=0)
            self._set_cache("cancellations", result)
            return result

        rows = table.find_all("tr")
        if len(rows) < 2:
            result = CancellationListResponse(entries=[], total_count=0)
            self._set_cache("cancellations", result)
            return result

        header_cells = rows[0].find_all(["th", "td"])
        headers = [_text(c) for c in header_cells]

        entries: list[CancellationEntry] = []
        for row in rows[1:]:
            cells = row.find_all("td")
            if not cells:
                continue
            cell_texts = [_text(c) for c in cells]
            if len(cell_texts) == 1 and "該当" in cell_texts[0]:
                continue
            if len(cell_texts) < len(headers):
                continue
            col = {
                h: cell_texts[i] for i, h in enumerate(headers) if i < len(cell_texts)
            }
            entries.append(
                CancellationEntry(
                    date=col.get("日付", ""),
                    period=col.get("時限", ""),
                    subject_name=col.get("科目", ""),
                    subject_code=col.get("開講番号", ""),
                    instructor=col.get("教員名", ""),
                    cancel_type=col.get("変更内容", col.get("区分", "")),
                    room=col.get("講義室", ""),
                )
            )

        result = CancellationListResponse(entries=entries, total_count=len(entries))
        self._set_cache("cancellations", result)
        return result

    # ─── 成績情報 (全フィールド対応) ──────────────────

    async def get_grades(self) -> GradeResponse:
        """成績一覧を全フィールドで取得。得点・合否・報告日含む。"""
        cached = self._get_cache("grades")
        if cached:
            return cached

        html = await self.client.access_web_flow("SIW0001300-flow")
        soup = BeautifulSoup(html, "lxml")

        # 学生情報抽出
        student_name = ""
        student_id = ""
        department = ""
        grade_year = ""
        total_credits = ""

        info_table = _find_data_table(soup, ["氏名", "在籍番号"])
        if info_table:
            for row in info_table.find_all("tr"):
                cells = row.find_all(["th", "td"])
                for i, cell in enumerate(cells):
                    t = _text(cell)
                    if t == "氏名" and i + 1 < len(cells):
                        student_name = _text(cells[i + 1])
                    elif "在籍番号" in t and i + 1 < len(cells):
                        student_id = _text(cells[i + 1])
                    elif t == "所属" and i + 1 < len(cells):
                        department = _text(cells[i + 1])
                    elif t == "学年" and i + 1 < len(cells):
                        grade_year = _text(cells[i + 1])
                    elif "修得単位数" in t and i + 1 < len(cells):
                        m = re.search(r"([0-9.]+)", _text(cells[i + 1]))
                        if m:
                            total_credits = m.group(1)

        # 成績テーブル (columnheaderで判定)
        table = _find_data_table(soup, ["No.", "科目", "単位数"])
        entries: list[GradeEntry] = []
        if table:
            rows = table.find_all("tr")
            header_cells = rows[0].find_all(["th", "td"])
            headers = [_text(c) for c in header_cells]

            for row in rows[1:]:
                cells = row.find_all("td")
                if not cells or len(cells) < 5:
                    continue
                cell_texts = [_text(c) for c in cells]
                col = {
                    h: cell_texts[i]
                    for i, h in enumerate(headers)
                    if i < len(cell_texts)
                }
                subject = col.get("科目", "")
                if not subject:
                    continue
                entries.append(
                    GradeEntry(
                        no=col.get("No.", ""),
                        subject_name=subject,
                        subject_code=col.get("開講番号", ""),
                        instructor=col.get("担当教員", ""),
                        category=col.get("科目区分", ""),
                        required_type=col.get("必選区分", ""),
                        credits=col.get("単位数", ""),
                        score=col.get("得点", ""),
                        grade=col.get("評語", ""),
                        pass_fail=col.get("合否", ""),
                        gp=col.get("GP", ""),
                        year=col.get("修得年度", ""),
                        semester=col.get("修得学期", ""),
                        report_date=col.get("報告日", ""),
                        exam_type=col.get("試験種別", ""),
                        field=col.get("分野", ""),
                        level=col.get("水準", ""),
                    )
                )

        # GPA/成績平均をディプロマサプリメントから取得試行
        gpa = ""
        average_score = ""
        full_text = soup.get_text()
        m = re.search(r"GPA[：:\s]*([0-9.]+)", full_text)
        if m:
            gpa = m.group(1)

        # 科目区分別・必選区分別の集計を算出 (合格分のみ)
        cat_credits: dict[str, float] = {}
        cat_counts: dict[str, int] = {}
        req_credits: dict[str, float] = {}
        req_counts: dict[str, int] = {}
        passed_count = 0
        failed_count = 0

        for e in entries:
            try:
                c = float(e.credits)
            except (ValueError, TypeError):
                c = 0.0

            if e.pass_fail == "合":
                passed_count += 1
                # 科目区分別
                cat = e.category or "不明"
                cat_credits[cat] = cat_credits.get(cat, 0.0) + c
                cat_counts[cat] = cat_counts.get(cat, 0) + 1
                # 必選区分別
                req = e.required_type or "不明"
                req_credits[req] = req_credits.get(req, 0.0) + c
                req_counts[req] = req_counts.get(req, 0) + 1
            elif e.pass_fail == "否":
                failed_count += 1

        credits_by_category = [
            CreditSummaryItem(
                category=cat,
                earned_credits=cat_credits[cat],
                subject_count=cat_counts[cat],
            )
            for cat in sorted(cat_credits.keys())
        ]
        credits_by_required_type = [
            CreditSummaryItem(
                category=req,
                earned_credits=req_credits[req],
                subject_count=req_counts[req],
            )
            for req in sorted(req_credits.keys())
        ]

        result = GradeResponse(
            student_name=student_name,
            student_id=student_id,
            department=department,
            grade_year=grade_year,
            entries=entries,
            total_credits=total_credits,
            gpa=gpa,
            average_score=average_score,
            credits_by_category=credits_by_category,
            credits_by_required_type=credits_by_required_type,
            passed_count=passed_count,
            failed_count=failed_count,
        )
        self._set_cache("grades", result)
        return result

    # ─── レポート・小テスト (全フィールド対応) ────────

    async def get_reports(self) -> ReportListResponse:
        """レポート・小テスト一覧を全フィールドで取得。"""
        cached = self._get_cache("reports")
        if cached:
            return cached

        html = await self.client.access_web_flow("ENW3411100-flow")
        soup = BeautifulSoup(html, "lxml")

        table = _find_data_table(soup, ["タイトル", "状態"])
        if table is None:
            result = ReportListResponse(entries=[], total_count=0)
            self._set_cache("reports", result)
            return result

        rows = table.find_all("tr")
        if len(rows) < 2:
            result = ReportListResponse(entries=[], total_count=0)
            self._set_cache("reports", result)
            return result

        header_cells = rows[0].find_all(["th", "td"])
        headers = [_text(c) for c in header_cells]

        entries: list[ReportEntry] = []
        for row in rows[1:]:
            cells = row.find_all("td")
            if not cells or len(cells) < 5:
                continue
            cell_texts = [_text(c) for c in cells]
            col = {
                h: cell_texts[i] for i, h in enumerate(headers) if i < len(cell_texts)
            }
            title = col.get("タイトル", "")
            if not title:
                continue

            period = col.get("提出期間", "")
            deadline_start = ""
            deadline_end = period
            if "～" in period or "〜" in period:
                parts = re.split(r"[～〜]", period, maxsplit=1)
                deadline_start = parts[0].strip()
                deadline_end = parts[1].strip() if len(parts) > 1 else ""

            status = col.get("状態", "")
            days_until, urgency = _compute_urgency(deadline_end, status)

            entries.append(
                ReportEntry(
                    report_type=col.get("種別", ""),
                    title=title,
                    subject_name=col.get("科目名", ""),
                    subject_code=col.get("開講番号", ""),
                    status=status,
                    semester=col.get("開講", ""),
                    day_period=col.get("曜日・時限", ""),
                    deadline_start=deadline_start,
                    deadline_end=deadline_end,
                    days_until_deadline=days_until,
                    urgency=urgency,
                )
            )

        unsubmitted = [e for e in entries if e.status != "提出済"]
        overdue = [e for e in entries if e.urgency == "overdue"]
        result = ReportListResponse(
            entries=entries,
            total_count=len(entries),
            unsubmitted_count=len(unsubmitted),
            overdue_count=len(overdue),
        )
        self._set_cache("reports", result)
        return result

    # ─── 連絡通知 ──────────────────────────────────────

    async def get_notices(self, limit: int = 20) -> NoticeListResponse:
        """連絡通知一覧を取得。"""
        cache_key = f"notices_{limit}"
        cached = self._get_cache(cache_key)
        if cached:
            return cached

        html = await self.client.access_web_flow("KJW0001100-flow")
        soup = BeautifulSoup(html, "lxml")

        table = _find_data_table(soup, ["表題"])
        if table is None:
            table = _find_data_table(soup, ["掲載日時"])
        if table is None:
            result = NoticeListResponse(entries=[], total_count=0)
            self._set_cache(cache_key, result)
            return result

        rows = table.find_all("tr")
        if len(rows) < 2:
            result = NoticeListResponse(entries=[], total_count=0)
            self._set_cache(cache_key, result)
            return result

        header_cells = rows[0].find_all(["th", "td"])
        headers = [_text(c) for c in header_cells]

        entries: list[NoticeEntry] = []
        for row in rows[1:]:
            cells = row.find_all("td")
            if not cells or len(cells) < 2:
                continue
            cell_texts = [_text(c) for c in cells]
            col = {
                h: cell_texts[i] for i, h in enumerate(headers) if i < len(cell_texts)
            }
            title = col.get("表題", col.get("タイトル", ""))
            if not title:
                continue

            detail_key = ""
            link = row.find("a", href=re.compile(r"displayMidoku|seqNo"))
            if link and link.get("href"):
                detail_key = link["href"]

            entries.append(
                NoticeEntry(
                    title=title,
                    sender=col.get("氏名", col.get("送信者", "")),
                    date=col.get("掲載日時", col.get("日付", "")),
                    genre=col.get("ジャンル", ""),
                    content="",
                    is_read=col.get("返信未読", "") == "-",
                    detail_key=detail_key,
                )
            )

        if limit > 0:
            entries = entries[:limit]

        result = NoticeListResponse(entries=entries, total_count=len(entries))
        self._set_cache(cache_key, result)
        return result

    async def get_notice_detail(self, detail_key: str) -> NoticeDetailResponse:
        """連絡通知の本文を取得。"""
        cached = self._get_cache(f"notice_detail_{detail_key}")
        if cached:
            return cached

        if not detail_key:
            return NoticeDetailResponse(
                title="", content="detail_key が指定されていません"
            )

        html = await self.client.get_frame_content(detail_key)
        soup = BeautifulSoup(html, "lxml")

        body = soup.find("body")
        full_text = body.get_text(separator="\n", strip=True) if body else ""

        title = ""
        sender = ""
        period = ""
        target = ""
        url = ""

        tables = soup.find_all("table")
        for table in tables:
            for row in table.find_all("tr"):
                cells = row.find_all(["th", "td"])
                if len(cells) == 1:
                    text = _text(cells[0])
                    if not title and len(text) > 5:
                        title = text
                elif len(cells) >= 2:
                    label = _text(cells[0])
                    value = _text(cells[1])
                    if "連絡通知元" in label:
                        sender = value
                    elif "連絡通知期間" in label:
                        period = value
                    elif "URL" in label:
                        link = cells[1].find("a")
                        url = link["href"] if link else value
                    elif "対象" in label:
                        target = value

        content = full_text
        if content.startswith("連絡通知\n"):
            content = content[len("連絡通知\n") :]
        for marker in ["連絡通知元\n", "連絡通知元 "]:
            idx = content.find(marker)
            if idx > 0:
                content = content[:idx].strip()
                break

        result = NoticeDetailResponse(
            title=title,
            sender=sender,
            period=period,
            target=target,
            url=url,
            content=content,
        )
        self._set_cache(f"notice_detail_{detail_key}", result)
        return result

    # ─── 出欠管理 (全フィールド対応) ──────────────────

    async def get_attendance(self) -> AttendanceResponse:
        """出欠情報を全フィールドで取得。各回の出欠記録含む。"""
        cached = self._get_cache("attendance")
        if cached:
            return cached

        html = await self.client.access_web_flow("AAW3411000-flow")
        soup = BeautifulSoup(html, "lxml")

        # ヘッダー: No, 開講番号, 科目名, 曜日・時限, 開講区分, 担当教員,
        #           アラート条件, 出席回数, 欠席回数, 遅刻回数, 早退回数, 無効回数, 1, 2, ...16
        table = _find_data_table(soup, ["科目名", "出席"])
        if table is None:
            table = _find_data_table(soup, ["科目名"])
        if table is None:
            result = AttendanceResponse(entries=[])
            self._set_cache("attendance", result)
            return result

        rows = table.find_all("tr")
        if len(rows) < 2:
            result = AttendanceResponse(entries=[])
            self._set_cache("attendance", result)
            return result

        header_cells = rows[0].find_all(["th", "td"])
        headers = [_text(c) for c in header_cells]

        entries: list[AttendanceEntry] = []
        for row in rows[1:]:
            cells = row.find_all("td")
            if not cells or len(cells) < 6:
                continue
            cell_texts = [_text(c) for c in cells]
            col = {
                h: cell_texts[i] for i, h in enumerate(headers) if i < len(cell_texts)
            }
            subject = col.get("科目名", "")
            if not subject:
                continue

            # 各回の出欠記録を取得 (ヘッダーが数字のもの)
            session_records = []
            for i, h in enumerate(headers):
                if h.isdigit() and i < len(cell_texts):
                    val = cell_texts[i]
                    if val:
                        session_records.append(val)

            _safe_int(col.get("出席回数", col.get("出席\n回数", ""))) + _safe_int(
                col.get("欠席回数", col.get("欠席\n回数", ""))
            )

            entries.append(
                AttendanceEntry(
                    subject_name=subject,
                    subject_code=col.get("開講番号", ""),
                    day_period=col.get("曜日・時限", ""),
                    semester=col.get("開講区分", ""),
                    instructor=col.get("担当教員", ""),
                    alert_condition=col.get("アラート条件", ""),
                    attendance_count=_safe_int(
                        col.get("出席回数", col.get("出席\n回数", ""))
                    ),
                    absence_count=_safe_int(
                        col.get("欠席回数", col.get("欠席\n回数", ""))
                    ),
                    late_count=_safe_int(
                        col.get("遅刻回数", col.get("遅刻\n回数", ""))
                    ),
                    early_leave_count=_safe_int(
                        col.get("早退回数", col.get("早退\n回数", ""))
                    ),
                    invalid_count=_safe_int(
                        col.get("無効回数", col.get("無効\n回数", ""))
                    ),
                    session_records=session_records,
                )
            )

        result = AttendanceResponse(entries=entries)
        self._set_cache("attendance", result)
        return result

    # ─── 出欠リスク分析 ──────────────────────────────

    async def get_attendance_risk(self) -> AttendanceRiskResponse:
        """出欠リスクを分析。欠席率が危険水準の科目を検出。

        safeでも「あとX回で危険」を計算して表示する。
        """
        attendance = await self.get_attendance()
        entries: list[AttendanceRiskEntry] = []

        for a in attendance.entries:
            total = a.attendance_count + a.absence_count
            if total == 0:
                continue
            absence_rate = a.absence_count / total

            # アラート条件からの最大欠席回数を取得
            if a.alert_condition:
                m = re.search(r"(\d+)", a.alert_condition)
                if m:
                    int(m.group(1))

            # 残り許容欠席回数
            remaining_to_warning = max(0, int(total / 4) - a.absence_count)
            remaining_to_danger = max(0, int(total / 3) - a.absence_count)

            if absence_rate > 1 / 3:
                risk = "danger"
                msg = f"欠席率{absence_rate:.0%} ({a.absence_count}/{total}回)。単位取得が危険です。"
            elif absence_rate > 1 / 4:
                risk = "warning"
                msg = (
                    f"欠席率{absence_rate:.0%} ({a.absence_count}/{total}回)。"
                    f"あと{remaining_to_danger}回欠席で単位取得危険。"
                )
            else:
                risk = "safe"
                msg = (
                    f"欠席{a.absence_count}/{total}回。"
                    f"あと{remaining_to_warning}回で注意、{remaining_to_danger}回で危険。"
                )

            entries.append(
                AttendanceRiskEntry(
                    subject_name=a.subject_name,
                    absence_rate=round(absence_rate, 3),
                    absence_count=a.absence_count,
                    total_classes=total,
                    risk_level=risk,
                    message=msg,
                )
            )

        at_risk = [e for e in entries if e.risk_level != "safe"]
        return AttendanceRiskResponse(entries=entries, at_risk_count=len(at_risk))

    # ─── シラバス検索 ──────────────────────────────────

    async def search_syllabus(
        self,
        subject_name: str = "",
        instructor: str = "",
        keyword: str = "",
        year: str = "2025",
    ) -> SyllabusSearchResponse:
        """シラバスを検索。"""
        cache_key = f"syllabus_{subject_name}_{instructor}_{keyword}_{year}"
        cached = self._get_cache(cache_key)
        if cached:
            return cached

        html = await self.client.access_web_flow("SYW0001000-flow")
        soup = BeautifulSoup(html, "lxml")
        fek = _extract_flow_execution_key(soup)

        # 検索フォーム送信 (SearchForm のフィールド名に合わせる)
        form_data = {
            "_flowExecutionKey": fek,
            "_eventId": "search",
            "s_no": "0",
            "nendo": year,
            "jikanwariShozokuCode": "",
            "kaikoNo": "",
            "kaikoKamokunm": subject_name,
            "kyokannm": instructor,
            "keywords": keyword,
        }
        html = await self.client.submit_web_flow("campussquare.do", form_data)
        soup = BeautifulSoup(html, "lxml")

        # 結果テーブル: No., 学期, 開講, 曜日・時限, 科目区分, 開講番号, 科目名, 担当教員, 参照
        table = _find_data_table(soup, ["No.", "科目名", "担当教員"])
        if table is None:
            result = SyllabusSearchResponse(entries=[], total_count=0)
            self._set_cache(cache_key, result)
            return result

        rows = table.find_all("tr")
        header_cells = rows[0].find_all(["th", "td"])
        headers = [_text(c) for c in header_cells]

        entries: list[SyllabusSearchResult] = []
        for row in rows[1:]:
            cells = row.find_all("td")
            if not cells or len(cells) < 5:
                continue
            cell_texts = [_text(c) for c in cells]
            col = {
                h: cell_texts[i] for i, h in enumerate(headers) if i < len(cell_texts)
            }
            name = col.get("科目名", "")
            if not name:
                continue
            entries.append(
                SyllabusSearchResult(
                    subject_name=name,
                    subject_code=col.get("開講番号", ""),
                    instructor=col.get("担当教員", ""),
                    semester=col.get("学期", ""),
                    term=col.get("開講", ""),
                    day_period=col.get("曜日・時限", ""),
                    category=col.get("科目区分", ""),
                )
            )

        result = SyllabusSearchResponse(entries=entries, total_count=len(entries))
        self._set_cache(cache_key, result)
        return result

    async def get_syllabus_detail(
        self, subject_code: str, year: str = "2025"
    ) -> SyllabusDetailResponse:
        """シラバス詳細を取得。refer関数相当の処理。"""
        cache_key = f"syllabus_detail_{subject_code}_{year}"
        cached = self._get_cache(cache_key)
        if cached:
            return cached

        # まず検索ページを開いてflowExecutionKeyを取得
        html = await self.client.access_web_flow("SYW0001000-flow")
        soup = BeautifulSoup(html, "lxml")
        fek = _extract_flow_execution_key(soup)

        # ReferForm相当: refer(nendo, jikanwariShozokuCode, jikanwaricd, locale)
        # jikanwariShozokuCode は検索結果から取得する必要があるが、
        # 空文字でもfallbackできることがある。まず検索して取得する。
        search_data = {
            "_flowExecutionKey": fek,
            "_eventId": "search",
            "s_no": "0",
            "nendo": year,
            "kaikoNo": subject_code,
            "kaikoKamokunm": "",
            "kyokannm": "",
            "keywords": "",
        }
        html = await self.client.submit_web_flow("campussquare.do", search_data)
        soup = BeautifulSoup(html, "lxml")

        # 結果の「和文」ボタンからrefer()パラメータを取得
        refer_btn = soup.find("input", {"value": "和文"})
        if not refer_btn:
            return SyllabusDetailResponse(subject_name="科目が見つかりません")

        onclick = refer_btn.get("onclick", "")
        # refer('2025','28','250F3823','ja_JP');
        m = re.search(r"refer\('([^']+)','([^']+)','([^']+)','([^']+)'\)", onclick)
        if not m:
            return SyllabusDetailResponse(subject_name="シラバス詳細の取得に失敗")

        nendo, jscd, jcd, locale = m.group(1), m.group(2), m.group(3), m.group(4)

        # ReferFormを使って詳細ページに遷移
        fek2 = _extract_flow_execution_key(soup)
        detail_data = {
            "_flowExecutionKey": fek2,
            "_eventId": "input",
            "nendo": nendo,
            "jikanwariShozokuCode": jscd,
            "jikanwaricd": jcd,
            "locale": locale,
        }
        html = await self.client.submit_web_flow("campussquare.do", detail_data)
        soup = BeautifulSoup(html, "lxml")

        # 詳細ページの3テーブルをパース
        tables = soup.find_all("table")
        result = SyllabusDetailResponse()

        for table in tables:
            for row in table.find_all("tr"):
                cells = row.find_all(["th", "td"])
                if len(cells) < 2:
                    continue
                label = _text(cells[0])
                value = _text(cells[1])

                # テーブル0: 基本情報
                if "科目名" in label and "Course Title" in label:
                    parts = value.split("／", 1)
                    result.subject_name = parts[0].strip()
                    if len(parts) > 1:
                        result.subject_name_en = parts[1].strip()
                elif "担当教員" in label:
                    result.instructor = value
                elif "開講番号" in label:
                    result.subject_code = value
                elif "対象学年" in label:
                    result.target_grade = value
                elif "講義室" in label:
                    result.classroom = value
                elif "開講学期" in label:
                    result.semester = value
                elif "曜日・時限" in label:
                    result.day_period = value
                elif "単位数" in label and "Credits" in label:
                    result.credits = value
                elif "科目区分" in label:
                    result.category = value
                elif "副専攻" in label:
                    result.minor_program = value
                elif "定員" in label:
                    result.capacity = value
                # テーブル1: 概要情報
                elif "科目の概要" in label:
                    result.outline = value
                elif "科目のねらい" in label:
                    result.objectives = value
                elif "学習の到達目標" in label:
                    result.learning_goals = value
                elif "登録のための条件" in label:
                    result.prerequisites = value
                elif "授業実施形態" in label:
                    result.class_format = value

        # テーブル2: 授業計画 (全回分を結合)
        schedule_parts = []
        for table in tables:
            first_row = table.find("tr")
            if not first_row:
                continue
            cells = first_row.find_all(["th", "td"])
            headers = [_text(c) for c in cells]
            if "内容" not in " ".join(headers):
                continue
            for row in table.find_all("tr")[1:]:
                cells = row.find_all("td")
                if len(cells) >= 2:
                    no = _text(cells[0])
                    content = _text(cells[1])
                    if no and content:
                        schedule_parts.append(f"第{no}回: {content}")
        if schedule_parts:
            result.schedule = "\n".join(schedule_parts)

        self._set_cache(cache_key, result)
        return result

    # ─── (学生便覧検索は GPTs Knowledge Files に移行済み) ──

    def _placeholder_removed(self) -> None:  # noqa: This method intentionally left empty
        pass

    # (以下の便覧系メソッドは削除済み: search_handbook, check_graduation)
    # GPTs が Knowledge Files と grades API の集計データを使って自力で修了判定する

    # ─── ダイジェスト (朝のブリーフィング) ──────────────

    async def get_digest(self) -> DigestResponse:
        """全データソースを統合したダイジェストを生成。"""
        import asyncio

        reports_task = asyncio.create_task(self.get_reports())
        cancellations_task = asyncio.create_task(self.get_cancellations())
        attendance_risk_task = asyncio.create_task(self.get_attendance_risk())
        notices_task = asyncio.create_task(self.get_notices(limit=5))
        grades_task = asyncio.create_task(self.get_grades())

        reports, cancellations, attendance_risk, notices, grades = await asyncio.gather(
            reports_task,
            cancellations_task,
            attendance_risk_task,
            notices_task,
            grades_task,
        )

        urgent_reports = [
            e
            for e in reports.entries
            if e.urgency in ("critical", "warning", "overdue")
        ]
        summary_parts = []
        if reports.unsubmitted_count > 0:
            summary_parts.append(f"未提出{reports.unsubmitted_count}件")
        if reports.overdue_count > 0:
            summary_parts.append(f"期限超過{reports.overdue_count}件")
        if not summary_parts:
            summary_parts.append("全て提出済み")
        reports_summary = "、".join(summary_parts)

        risk_entries = [e for e in attendance_risk.entries if e.risk_level != "safe"]

        return DigestResponse(
            reports_summary=reports_summary,
            urgent_reports=urgent_reports,
            cancellations=cancellations.entries,
            attendance_risks=risk_entries,
            recent_notices=notices.entries,
            total_credits=grades.total_credits,
            average_score=grades.average_score,
        )

    # ─── ICS カレンダーエクスポート ──────────────────────

    async def export_timetable_ics(
        self, year: int = 2025, semester: str = "前期"
    ) -> FileExportResponse:
        """時間割をICSファイルとしてエクスポート。"""
        import base64

        timetable = await self.get_timetable(year=year, semester=semester)
        cancellations = await self.get_cancellations()

        ics_content = _generate_ics(timetable.entries, cancellations.entries)
        encoded = base64.b64encode(ics_content.encode("utf-8")).decode("ascii")

        return FileExportResponse(
            openaiFileResponse=[
                FileItem(
                    name=f"timetable_{year}_{semester}.ics",
                    mime_type="text/calendar",
                    content=encoded,
                )
            ]
        )

    # ─── CSV 成績エクスポート ──────────────────────────

    async def export_grades_csv(self) -> FileExportResponse:
        """成績をCSVファイルとしてエクスポート。"""
        import base64
        import csv
        import io

        grades = await self.get_grades()

        output = io.StringIO()
        writer = csv.writer(output)
        writer.writerow(
            [
                "No.",
                "年度",
                "学期",
                "科目名",
                "開講番号",
                "担当教員",
                "科目区分",
                "必選区分",
                "単位数",
                "得点",
                "評語",
                "合否",
                "GP",
            ]
        )
        for e in grades.entries:
            writer.writerow(
                [
                    e.no,
                    e.year,
                    e.semester,
                    e.subject_name,
                    e.subject_code,
                    e.instructor,
                    e.category,
                    e.required_type,
                    e.credits,
                    e.score,
                    e.grade,
                    e.pass_fail,
                    e.gp,
                ]
            )
        writer.writerow([])
        writer.writerow(["修得単位数合計", grades.total_credits])
        writer.writerow(["GPA", grades.gpa])

        csv_bytes = output.getvalue().encode("utf-8-sig")
        encoded = base64.b64encode(csv_bytes).decode("ascii")

        return FileExportResponse(
            openaiFileResponse=[
                FileItem(
                    name="grades.csv",
                    mime_type="text/csv",
                    content=encoded,
                )
            ]
        )


# ─── ICS生成ユーティリティ ──────────────────────────


def _generate_ics(
    timetable: list[TimetableEntry],
    cancellations: list[CancellationEntry],
) -> str:
    """時間割と休講情報からICSカレンダーを生成。"""
    from datetime import datetime, timedelta

    period_times = {
        1: ("08:30", "10:00"),
        2: ("10:15", "11:45"),
        3: ("12:40", "14:10"),
        4: ("14:25", "15:55"),
        5: ("16:10", "17:40"),
        6: ("17:50", "19:20"),
        7: ("19:30", "21:00"),
    }
    day_offset = {"月": 0, "火": 1, "水": 2, "木": 3, "金": 4, "土": 5}

    lines = [
        "BEGIN:VCALENDAR",
        "VERSION:2.0",
        "PRODID:-//GakujoGPT//新大学務AIアシスタント//JP",
        "CALSCALE:GREGORIAN",
        "METHOD:PUBLISH",
        "X-WR-CALNAME:新潟大学 時間割",
        "X-WR-TIMEZONE:Asia/Tokyo",
    ]

    now = datetime.now()
    monday = now - timedelta(days=now.weekday())

    for entry in timetable:
        if entry.period not in period_times:
            continue
        if entry.day_of_week not in day_offset:
            continue

        start_time, end_time = period_times[entry.period]
        offset = day_offset[entry.day_of_week]
        event_date = monday + timedelta(days=offset)

        dtstart = event_date.strftime(f"%Y%m%dT{start_time.replace(':', '')}00")
        dtend = event_date.strftime(f"%Y%m%dT{end_time.replace(':', '')}00")

        uid = f"{entry.subject_code or entry.subject_name}-{entry.day_of_week}-{entry.period}@gakujo-gpt"

        desc_parts = []
        if entry.subject_code:
            desc_parts.append(f"開講番号: {entry.subject_code}")
        if entry.credits:
            desc_parts.append(f"単位数: {entry.credits}")

        lines.extend(
            [
                "BEGIN:VEVENT",
                f"UID:{uid}",
                f"DTSTART;TZID=Asia/Tokyo:{dtstart}",
                f"DTEND;TZID=Asia/Tokyo:{dtend}",
                "RRULE:FREQ=WEEKLY;COUNT=15",
                f"SUMMARY:{entry.subject_name}",
                f"LOCATION:{entry.room}" if entry.room else "LOCATION:",
                f"DESCRIPTION:{' / '.join(desc_parts)}"
                if desc_parts
                else "DESCRIPTION:",
                "END:VEVENT",
            ]
        )

    for cancel in cancellations:
        if not cancel.date:
            continue
        try:
            cdate = datetime.strptime(cancel.date.strip(), "%Y/%m/%d")
        except ValueError:
            continue

        lines.extend(
            [
                "BEGIN:VEVENT",
                f"UID:cancel-{cancel.subject_code or cancel.subject_name}-{cancel.date}@gakujo-gpt",
                f"DTSTART;VALUE=DATE:{cdate.strftime('%Y%m%d')}",
                f"SUMMARY:[{cancel.cancel_type}] {cancel.subject_name}",
                f"DESCRIPTION:{cancel.room}",
                "END:VEVENT",
            ]
        )

    lines.append("END:VCALENDAR")
    return "\r\n".join(lines)
