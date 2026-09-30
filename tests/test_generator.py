"""generator 单元测试（使用 Mock 数据）。"""

from datetime import date, datetime, timedelta, timezone

import pytest

import generator.formatter as formatter
from collector.github import CommitRecord
from collector.lark_attendance import AttendanceRecord
from collector.lark_msg import MessageRecord
from collector.lark_task import TaskRecord
from generator import DailyReport, MemberReport, generate

REPORT_DATE = date(2026, 9, 29)
TZ = timezone.utc


@pytest.fixture(autouse=True)
def fixed_local_tz(monkeypatch):
    """固定展示时区为 UTC，保证既有断言与机器时区无关（§6.4 转换逻辑由专项测试覆盖）。"""
    monkeypatch.setattr(formatter, "LOCAL_TZ", timezone.utc)


def make_member(**overrides):
    defaults = dict(
        name="张三",
        github_username="zhangsan",
        commits=[CommitRecord(author="zhangsan", message="feat: 登录页",
                              timestamp=datetime(2026, 9, 29, 8, 30, tzinfo=TZ),
                              repo="org/repo", additions=10, deletions=3,
                              files_changed=2)],
        tasks=[TaskRecord(assignee="张三", title="实现登录页", status_from="未开始",
                          status_to="进行中",
                          updated_at=datetime(2026, 9, 29, 10, 0, tzinfo=TZ))],
        messages=[MessageRecord(sender="张三", content="评审文档已上传",
                                timestamp=datetime(2026, 9, 29, 10, 5, tzinfo=TZ),
                                chat_name="团队群")],
        attendance=None,
    )
    defaults.update(overrides)
    return MemberReport(**defaults)


def make_attendance(**overrides):
    defaults = dict(
        employee_id="zhangsan@company.com",
        date=REPORT_DATE,
        check_in=datetime(2026, 9, 29, 9, 0, tzinfo=TZ),
        check_out=datetime(2026, 9, 29, 18, 0, tzinfo=TZ),
        work_hours=9.0,
        status="正常",
    )
    defaults.update(overrides)
    return AttendanceRecord(**defaults)


def test_generate_returns_dailyreport_with_all_fields():
    member = make_member()
    report = generate([member], REPORT_DATE, "研发一组")

    assert isinstance(report, DailyReport)
    assert report.date == REPORT_DATE
    assert report.team_name == "研发一组"
    assert report.members == [member]
    assert isinstance(report.generated_at, datetime)
    assert isinstance(report.markdown, str) and report.markdown
    assert isinstance(report.html, str) and report.html


def test_markdown_contains_three_section_titles():
    report = generate([make_member()], REPORT_DATE, "研发一组")
    markdown = report.markdown

    assert "# 研发一组 日报（2026-09-29）" in markdown
    assert "## 张三" in markdown
    assert "GitHub 用户名: zhangsan" in markdown
    assert "### 代码提交" in markdown
    assert "### 任务进展" in markdown
    assert "### 协作沟通" in markdown


def test_markdown_renders_commits_with_stats():
    markdown = generate([make_member()], REPORT_DATE, "研发一组").markdown
    assert "[org/repo] feat: 登录页（+10/-3，2 个文件）— 08:30" in markdown


def test_markdown_renders_tasks():
    markdown = generate([make_member()], REPORT_DATE, "研发一组").markdown
    assert "实现登录页：未开始 → 进行中（10:00）" in markdown


def test_markdown_renders_messages():
    markdown = generate([make_member()], REPORT_DATE, "研发一组").markdown
    assert "[团队群] 张三：评审文档已上传（10:05）" in markdown


def test_multiline_commit_message_collapsed_to_single_line():
    member = make_member(commits=[CommitRecord(
        author="zhangsan", message="line1\nline2",
        timestamp=datetime(2026, 9, 29, 8, 30, tzinfo=TZ), repo="org/repo")])
    markdown = generate([member], REPORT_DATE, "研发一组").markdown
    assert "line1 line2" in markdown


def test_empty_member_shows_no_records_today():
    member = make_member(commits=[], tasks=[], messages=[])
    markdown = generate([member], REPORT_DATE, "研发一组").markdown

    assert markdown.count("今日无记录") == 3  # 三个板块各一条


def test_source_failure_annotated_in_markdown():
    member = make_member(source_errors={"github": "超时"})
    markdown = generate([member], REPORT_DATE, "研发一组").markdown

    assert "数据获取失败" in markdown
    assert "GitHub采集异常：超时" in markdown
    # 其他数据源正常渲染
    assert "实现登录页：未开始 → 进行中（10:00）" in markdown


def test_source_failure_without_reason():
    member = make_member(source_errors={"lark_msg": ""})
    markdown = generate([member], REPORT_DATE, "研发一组").markdown
    assert "**数据获取失败**（飞书消息采集异常）" in markdown


def test_all_sources_failed():
    member = make_member(source_errors={"github": "a", "lark_task": "b", "lark_msg": "c"})
    markdown = generate([member], REPORT_DATE, "研发一组").markdown

    assert markdown.count("数据获取失败") == 3
    assert "今日无记录" not in markdown


def test_multiple_members_each_have_sections():
    markdown = generate([make_member(), make_member(name="李四", github_username="lisi",
                                                    commits=[], tasks=[], messages=[])],
                        REPORT_DATE, "研发一组").markdown
    assert "## 张三" in markdown
    assert "## 李四" in markdown
    assert markdown.count("### 代码提交") == 2
    assert "今日无记录" in markdown


def test_html_wellformed_and_renders_sections():
    html = generate([make_member()], REPORT_DATE, "研发一组").html

    assert html.startswith("<!DOCTYPE html>")
    assert "<h1>研发一组 日报（2026-09-29）</h1>" in html
    assert "<title>研发一组 日报（2026-09-29）</title>" in html
    assert "<h2>张三</h2>" in html
    assert "<h3>代码提交</h3>" in html
    assert "<ul>" in html and "<li>" in html


def test_html_escapes_injected_content():
    member = make_member(commits=[CommitRecord(
        author="zhangsan", message="<script>alert(1)</script>",
        timestamp=datetime(2026, 9, 29, 8, 30, tzinfo=TZ), repo="org/repo")])
    html = generate([member], REPORT_DATE, "研发一组").html

    assert "&lt;script&gt;" in html
    assert "<script>" not in html


def test_html_renders_failure_annotation():
    member = make_member(source_errors={"github": "超时"})
    html = generate([member], REPORT_DATE, "研发一组").html

    assert "<strong>数据获取失败</strong>（GitHub采集异常：超时）" in html


# ---------- v1.1：工时统计板块 ----------

def test_attendance_section_between_tasks_and_messages():
    markdown = generate([make_member()], REPORT_DATE, "研发一组").markdown

    assert markdown.index("### 任务进展") < markdown.index("### 工时统计") \
        < markdown.index("### 协作沟通")


def test_attendance_renders_work_hours_and_status():
    member = make_member(attendance=make_attendance())
    markdown = generate([member], REPORT_DATE, "研发一组").markdown

    assert "### 工时统计" in markdown
    assert "签到 09:00 / 签退 18:00，工时 9 小时（正常）" in markdown


def test_attendance_late_status_rendered():
    member = make_member(attendance=make_attendance(
        check_in=datetime(2026, 9, 29, 9, 30, tzinfo=TZ), work_hours=8.5, status="迟到"))
    markdown = generate([member], REPORT_DATE, "研发一组").markdown

    assert "签到 09:30 / 签退 18:00，工时 8.5 小时（迟到）" in markdown


def test_attendance_missing_check_out_rendered():
    member = make_member(attendance=make_attendance(check_out=None, work_hours=0.0,
                                                     status="签退缺失"))
    markdown = generate([member], REPORT_DATE, "研发一组").markdown

    assert "签到 09:00 / 签退缺失" in markdown


def test_attendance_absent_without_punches_rendered():
    member = make_member(attendance=make_attendance(check_in=None, check_out=None,
                                                     work_hours=0.0, status="缺勤"))
    markdown = generate([member], REPORT_DATE, "研发一组").markdown

    assert "- 缺勤" in markdown


def test_attendance_unavailable_when_none():
    markdown = generate([make_member()], REPORT_DATE, "研发一组").markdown

    assert "考勤数据暂不可用" in markdown


def test_attendance_unavailable_rendered_in_html():
    html = generate([make_member()], REPORT_DATE, "研发一组").html

    assert "考勤数据暂不可用" in html


def test_attendance_unavailable_does_not_count_as_no_records():
    member = make_member(commits=[], tasks=[], messages=[])
    markdown = generate([member], REPORT_DATE, "研发一组").markdown

    assert markdown.count("今日无记录") == 3  # 三个板块各一条；工时统计显示"考勤数据暂不可用"


# ---------- §6.4 时区约定：展示统一转换为本地时区 ----------

def test_times_rendered_in_local_timezone(monkeypatch):
    """固定时区（UTC+8）下，UTC 数据应展示为本地时间（design.md §6.4）。"""
    monkeypatch.setattr(formatter, "LOCAL_TZ", timezone(timedelta(hours=8)))
    markdown = generate([make_member()], REPORT_DATE, "研发一组").markdown

    assert "[org/repo] feat: 登录页（+10/-3，2 个文件）— 16:30" in markdown  # 08:30 UTC
    assert "实现登录页：未开始 → 进行中（18:00）" in markdown                # 10:00 UTC
    assert "[团队群] 张三：评审文档已上传（18:05）" in markdown              # 10:05 UTC


def test_attendance_times_rendered_in_local_timezone(monkeypatch):
    """考勤签到/签退时间同样转换为本地时区展示（design.md §6.4），跨日不截断。"""
    monkeypatch.setattr(formatter, "LOCAL_TZ", timezone(timedelta(hours=8)))
    member = make_member(attendance=make_attendance())  # 09:00 / 18:00 UTC
    markdown = generate([member], REPORT_DATE, "研发一组").markdown

    assert "签到 17:00 / 签退 02:00，工时 9 小时（正常）" in markdown
