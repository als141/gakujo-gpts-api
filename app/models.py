"""GPTs向けレスポンスモデル定義。

OpenAPIスキーマが自動生成され、GPTs Custom Actionsにインポート可能。
Playwright DOM探索 (2026-03-15) に基づき、CampusSquareの全フィールドを正確に反映。
"""

from pydantic import BaseModel, Field


# ─── 共通 ──────────────────────────────────────────


class ErrorResponse(BaseModel):
    """エラーレスポンス"""

    error: str = Field(..., description="エラーメッセージ")
    detail: str | None = Field(None, description="詳細情報")


# ─── ファイルエクスポート (openaiFileResponse) ──────


class FileItem(BaseModel):
    """openaiFileResponse のファイルアイテム"""

    name: str = Field(..., description="ファイル名")
    mime_type: str = Field(..., description="MIMEタイプ")
    content: str = Field(..., description="Base64エンコードされたファイル内容")


class FileExportResponse(BaseModel):
    """ファイルエクスポートレスポンス (openaiFileResponse形式)"""

    openaiFileResponse: list[FileItem] = Field(
        ..., description="エクスポートされたファイルのリスト"
    )


# ─── 時間割 (RSW0001000-flow: 履修登録から取得) ──────


class TimetableEntry(BaseModel):
    """時間割の1コマ"""

    day_of_week: str = Field(..., description="曜日 (月, 火, 水, 木, 金, 土)")
    period: int = Field(..., description="時限 (1-7)")
    subject_name: str = Field(..., description="科目名")
    subject_code: str = Field("", description="開講番号 (例: 250F3823)")
    room: str = Field("", description="教室 (例: 工学部B303)")
    credits: str = Field("", description="単位数 (例: 2.0)")
    instructor: str = Field("", description="担当教員")


class TimetableIntensiveEntry(BaseModel):
    """集中講義の1件"""

    subject_name: str = Field(..., description="科目名")
    subject_code: str = Field("", description="開講番号")
    room: str = Field("", description="教室")
    period_type: str = Field("", description="開講期間")
    credits: str = Field("", description="単位数")
    note: str = Field("", description="備考")


class TimetableResponse(BaseModel):
    """時間割レスポンス"""

    year: str = Field("", description="年度")
    semester: str = Field("", description="学期 (第1学期/第2学期)")
    course_count: int = Field(0, description="履修科目数")
    entries: list[TimetableEntry] = Field(
        default_factory=list, description="通常時間割 (曜日×時限)"
    )
    intensive_courses: list[TimetableIntensiveEntry] = Field(
        default_factory=list, description="集中講義"
    )


# ─── 休講・補講 ──────────────────────────────────────


class CancellationEntry(BaseModel):
    """休講・補講の1件"""

    date: str = Field(..., description="日付 (例: 2026/02/20)")
    period: str = Field(..., description="時限")
    subject_name: str = Field(..., description="科目名")
    subject_code: str = Field("", description="開講番号")
    instructor: str = Field("", description="担当教員")
    cancel_type: str = Field("", description="種別 (休講/補講/変更)")
    room: str = Field("", description="講義室")


class CancellationListResponse(BaseModel):
    """休講・補講一覧レスポンス"""

    entries: list[CancellationEntry] = Field(
        default_factory=list, description="休講・補講一覧"
    )
    total_count: int = Field(0, description="件数")


# ─── 成績 (全フィールド対応) ──────────────────────────


class GradeEntry(BaseModel):
    """成績の1件 (CampusSquare DOM全列に対応)"""

    no: str = Field("", description="No.")
    subject_name: str = Field(..., description="科目名")
    subject_code: str = Field("", description="開講番号")
    instructor: str = Field("", description="担当教員")
    category: str = Field("", description="科目区分 (例: (A)(B)必修科目)")
    required_type: str = Field("", description="必選区分 (必修/選択/選択必修)")
    credits: str = Field("", description="単位数")
    score: str = Field("", description="得点 (数値または ---)")
    grade: str = Field("", description="評語 (秀/A/B/C等)")
    pass_fail: str = Field("", description="合否 (合/否)")
    gp: str = Field("", description="GP値")
    year: str = Field("", description="修得年度")
    semester: str = Field("", description="修得学期 (第1学期/第2学期)")
    report_date: str = Field("", description="報告日")
    exam_type: str = Field("", description="試験種別 (本試験等)")
    field: str = Field("", description="分野コード")
    level: str = Field("", description="水準コード")


class CreditSummaryItem(BaseModel):
    """科目区分別の単位集計"""

    category: str = Field(..., description="科目区分名")
    earned_credits: float = Field(0, description="修得単位数 (合格分のみ)")
    subject_count: int = Field(0, description="科目数")


class GradeResponse(BaseModel):
    """成績一覧レスポンス"""

    student_name: str = Field("", description="氏名")
    student_id: str = Field("", description="在籍番号")
    department: str = Field("", description="所属")
    grade_year: str = Field("", description="学年")
    entries: list[GradeEntry] = Field(default_factory=list, description="成績一覧")
    total_credits: str = Field("", description="修得単位数合計")
    gpa: str = Field("", description="GPA")
    average_score: str = Field("", description="総合成績平均")
    credits_by_category: list[CreditSummaryItem] = Field(
        default_factory=list,
        description="科目区分別の修得単位集計 (例: 必修科目/選択科目/他専攻科目/自然科学総論)",
    )
    credits_by_required_type: list[CreditSummaryItem] = Field(
        default_factory=list,
        description="必選区分別の修得単位集計 (必修/選択必修/選択)",
    )
    passed_count: int = Field(0, description="合格科目数")
    failed_count: int = Field(0, description="不合格科目数")


# ─── レポート・小テスト ──────────────────────────────


class ReportEntry(BaseModel):
    """レポート・小テストの1件"""

    report_type: str = Field("", description="種別 (レポート/小テスト/アンケート)")
    title: str = Field(..., description="タイトル")
    subject_name: str = Field(..., description="科目名")
    subject_code: str = Field("", description="開講番号")
    status: str = Field(..., description="提出状態 (未提出/一時保存/提出済)")
    semester: str = Field("", description="開講 (第1学期/第2学期)")
    day_period: str = Field("", description="曜日・時限 (例: 火1)")
    deadline_start: str = Field("", description="提出開始日時")
    deadline_end: str = Field("", description="提出期限")
    days_until_deadline: int | None = Field(
        None, description="期限までの日数 (負の値は期限超過)"
    )
    urgency: str = Field(
        "",
        description="緊急度 (overdue/critical/warning/safe/submitted)",
    )


class ReportListResponse(BaseModel):
    """レポート一覧レスポンス"""

    entries: list[ReportEntry] = Field(default_factory=list, description="レポート一覧")
    total_count: int = Field(0, description="件数")
    unsubmitted_count: int = Field(0, description="未提出件数")
    overdue_count: int = Field(0, description="期限超過件数")


# ─── 連絡通知 ──────────────────────────────────────


class NoticeEntry(BaseModel):
    """連絡通知の1件"""

    title: str = Field(..., description="タイトル")
    sender: str = Field("", description="送信者")
    date: str = Field("", description="日付")
    genre: str = Field("", description="ジャンル (全学連絡通知/授業連絡通知等)")
    content: str = Field("", description="本文 (一覧取得時は空)")
    is_read: bool = Field(False, description="既読かどうか")
    detail_key: str = Field(
        "",
        description="詳細取得用キー。/notices/detail に渡して本文を取得できる",
    )


class NoticeDetailResponse(BaseModel):
    """連絡通知詳細レスポンス"""

    title: str = Field(..., description="タイトル")
    sender: str = Field("", description="送信者")
    period: str = Field("", description="連絡通知期間")
    target: str = Field("", description="対象学生")
    url: str = Field("", description="関連URL")
    content: str = Field("", description="本文")


class NoticeListResponse(BaseModel):
    """連絡通知一覧レスポンス"""

    entries: list[NoticeEntry] = Field(default_factory=list, description="連絡通知一覧")
    total_count: int = Field(0, description="件数")


# ─── 出欠 (全フィールド対応) ──────────────────────────


class AttendanceEntry(BaseModel):
    """出欠の1件 (CampusSquare DOM全列に対応)"""

    subject_name: str = Field(..., description="科目名")
    subject_code: str = Field("", description="開講番号")
    day_period: str = Field("", description="曜日・時限 (例: 木5)")
    semester: str = Field("", description="開講区分 (第1学期/第2学期)")
    instructor: str = Field("", description="担当教員")
    alert_condition: str = Field("", description="アラート条件 (例: ﾄｰﾀﾙ:5回)")
    attendance_count: int = Field(0, description="出席回数")
    absence_count: int = Field(0, description="欠席回数")
    late_count: int = Field(0, description="遅刻回数")
    early_leave_count: int = Field(0, description="早退回数")
    invalid_count: int = Field(0, description="無効回数")
    session_records: list[str] = Field(
        default_factory=list,
        description="各回の出欠記録 (出/欠/遅/早/無/休/未登録)",
    )


class AttendanceResponse(BaseModel):
    """出欠一覧レスポンス"""

    entries: list[AttendanceEntry] = Field(default_factory=list, description="出欠一覧")


# ─── 出欠リスク ──────────────────────────────────────


class AttendanceRiskEntry(BaseModel):
    """出欠リスクの1件"""

    subject_name: str = Field(..., description="科目名")
    absence_rate: float = Field(0.0, description="欠席率 (0.0-1.0)")
    absence_count: int = Field(0, description="欠席回数")
    total_classes: int = Field(0, description="授業実施回数")
    risk_level: str = Field(
        "",
        description="リスクレベル (danger: 欠席率1/3超, warning: 欠席率1/4超, safe: 問題なし)",
    )
    message: str = Field("", description="警告メッセージ")


class AttendanceRiskResponse(BaseModel):
    """出欠リスク分析レスポンス"""

    entries: list[AttendanceRiskEntry] = Field(
        default_factory=list, description="科目別リスク一覧"
    )
    at_risk_count: int = Field(0, description="リスクありの科目数")


# ─── シラバス ──────────────────────────────────────


class SyllabusSearchResult(BaseModel):
    """シラバス検索結果の1件"""

    subject_name: str = Field(..., description="科目名")
    subject_code: str = Field("", description="開講番号")
    instructor: str = Field("", description="担当教員")
    semester: str = Field("", description="学期")
    term: str = Field("", description="開講 (第1ターム等)")
    day_period: str = Field("", description="曜日・時限")
    category: str = Field("", description="科目区分")


class SyllabusSearchResponse(BaseModel):
    """シラバス検索結果レスポンス"""

    entries: list[SyllabusSearchResult] = Field(
        default_factory=list, description="検索結果"
    )
    total_count: int = Field(0, description="件数")


class SyllabusDetailResponse(BaseModel):
    """シラバス詳細レスポンス"""

    subject_name: str = Field("", description="科目名")
    subject_name_en: str = Field("", description="科目名 (英語)")
    subject_code: str = Field("", description="開講番号")
    instructor: str = Field("", description="担当教員")
    target_grade: str = Field("", description="対象学年")
    classroom: str = Field("", description="講義室")
    semester: str = Field("", description="開講学期")
    day_period: str = Field("", description="曜日・時限")
    credits: str = Field("", description="単位数")
    category: str = Field("", description="科目区分")
    minor_program: str = Field("", description="副専攻")
    capacity: str = Field("", description="定員")
    outline: str = Field("", description="科目の概要")
    objectives: str = Field("", description="科目のねらい")
    learning_goals: str = Field("", description="学習の到達目標")
    schedule: str = Field("", description="授業計画 (全回分)")
    prerequisites: str = Field("", description="登録のための条件")
    class_format: str = Field("", description="授業実施形態")


# ─── ダイジェスト ──────────────────────────────────────


class DigestResponse(BaseModel):
    """週間ダイジェスト / 朝のブリーフィング用の統合レスポンス"""

    reports_summary: str = Field(
        "", description="レポート状況サマリー (例: 未提出2件、うち期限超過1件)"
    )
    urgent_reports: list[ReportEntry] = Field(
        default_factory=list, description="緊急レポート (未提出で期限7日以内)"
    )
    cancellations: list[CancellationEntry] = Field(
        default_factory=list, description="休講・補講情報"
    )
    attendance_risks: list[AttendanceRiskEntry] = Field(
        default_factory=list, description="出欠リスク警告のある科目"
    )
    recent_notices: list[NoticeEntry] = Field(
        default_factory=list, description="最新の連絡通知 (5件)"
    )
    total_credits: str = Field("", description="修得単位数合計")
    average_score: str = Field("", description="総合成績平均")
