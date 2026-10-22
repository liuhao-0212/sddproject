"""generator 单元测试（使用 Mock 数据）。"""

import re
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


def test_markdown_contains_member_and_section_titles():
    report = generate([make_member()], REPORT_DATE, "研发一组")
    markdown = report.markdown

    assert markdown.startswith("# 📊 研发一组 智能日报")
    assert "📅 2026-09-29 周二" in markdown
    assert "## ▎今日概览" in markdown
    assert "## ▎张三" in markdown
    assert "### ▪ 代码提交" in markdown
    assert "### ▪ 任务进展" in markdown
    assert "### ▪ 工时统计" in markdown
    assert "### ▪ 协作沟通" in markdown
    assert "GitHub 用户名" not in markdown  # v1.3：不再展示内部标识行


def test_markdown_renders_commits_with_stats():
    markdown = generate([make_member()], REPORT_DATE, "研发一组").markdown
    assert "· 今日 1 次提交，共 +10 / -3 行" in markdown
    assert "· ✨ 新功能（1）" in markdown  # v1.8：按类别分组，组头带类别图标
    assert "  - feat: 登录页（+10/-3 行）" in markdown
    assert "— 08:30" not in markdown  # v1.3：提交只留标题 + 行数，不展示时间等细节


def test_markdown_renders_tasks():
    markdown = generate([make_member()], REPORT_DATE, "研发一组").markdown
    assert "· 实现登录页：未开始 → 进行中（10:00）" in markdown


def test_markdown_renders_messages():
    markdown = generate([make_member()], REPORT_DATE, "研发一组").markdown
    assert "· [团队群] 张三：评审文档已上传（10:05）" in markdown


def test_message_sender_displayed_as_member_name():
    """sender 原值多为 open_id 等内部标识，展示统一用成员姓名（design.md §6.5 易读）。"""
    member = make_member(messages=[MessageRecord(
        sender="ou_zhangsan_openid", content="评审文档已上传",
        timestamp=datetime(2026, 9, 29, 10, 5, tzinfo=TZ), chat_name="团队群")])
    markdown = generate([member], REPORT_DATE, "研发一组").markdown

    assert "· [团队群] 张三：评审文档已上传（10:05）" in markdown
    assert "ou_zhangsan_openid" not in markdown  # 内部标识不进入日报正文


def test_commit_renders_title_line_only():
    """只展示 commit 消息第一行（标题），正文与 Co-Authored-By 等 trailer 不进入日报。"""
    member = make_member(commits=[CommitRecord(
        author="zhangsan",
        message="line1\nline2\n\nCo-Authored-By: Claude Code <noreply@anthropic.com>",
        timestamp=datetime(2026, 9, 29, 8, 30, tzinfo=TZ), repo="org/repo")])
    markdown = generate([member], REPORT_DATE, "研发一组").markdown

    assert "· 📦 其他（1）" in markdown  # v1.8：无 Conventional 前缀归「其他」
    assert "  - line1（+0/-0 行）" in markdown
    assert "line2" not in markdown
    assert "Co-Authored-By" not in markdown


def test_empty_member_shows_no_records_today():
    member = make_member(commits=[], tasks=[], messages=[])
    markdown = generate([member], REPORT_DATE, "研发一组").markdown

    assert markdown.count("· 今日无记录") == 4  # 四个板块各一条


def test_source_failure_shows_friendly_line():
    """v1.4 修订：不可用的源板块保留，显示一行可读说明（原始原因不进正文）。"""
    member = make_member(source_errors={"github": "超时"})
    markdown = generate([member], REPORT_DATE, "研发一组").markdown

    assert "▪ 代码提交" in markdown
    assert "· 飞书接口连接失败" in markdown
    assert "超时" not in markdown
    # 其他数据源正常渲染
    assert "实现登录页：未开始 → 进行中（10:00）" in markdown


def test_source_failure_without_reason_shows_default_line():
    """v1.4 修订：原因为空时显示兜底文案「飞书数据暂不可用」。"""
    member = make_member(source_errors={"lark_msg": ""})
    markdown = generate([member], REPORT_DATE, "研发一组").markdown

    assert "▪ 协作沟通" in markdown
    assert "· 飞书数据暂不可用" in markdown


def test_all_sources_failed_show_friendly_lines():
    """v1.4 修订：三个源全部不可用 → 三个板块保留、各显示可读说明；可用源板块保留。"""
    member = make_member(source_errors={"github": "a", "lark_task": "b", "lark_msg": "c"})
    markdown = generate([member], REPORT_DATE, "研发一组").markdown

    assert markdown.count("· 飞书数据暂不可用") == 3
    assert "▪ 代码提交" in markdown
    assert "▪ 任务进展" in markdown
    assert "▪ 协作沟通" in markdown
    assert "· 今日无记录" in markdown  # 考勤源可用但无记录：板块保留


def test_multiple_members_each_have_sections():
    markdown = generate([make_member(), make_member(name="李四", github_username="lisi",
                                                    commits=[], tasks=[], messages=[])],
                        REPORT_DATE, "研发一组").markdown
    assert "## ▎张三" in markdown
    assert "## ▎李四" in markdown
    assert markdown.count("### ▪ 代码提交") == 2
    assert "今日无记录" in markdown


def test_html_wellformed_and_renders_sections():
    html = generate([make_member()], REPORT_DATE, "研发一组").html

    assert html.startswith("<!DOCTYPE html>")
    assert "<h1>📊 研发一组 智能日报</h1>" in html
    assert "<title>研发一组 智能日报（2026-09-29）</title>" in html
    assert "<h2>▎张三</h2>" in html
    assert '<h3>💻 ▪ 代码提交 <span class="badge badge-ok">✅ 正常</span></h3>' in html
    assert "<ul>" in html and "<li>" in html


def test_html_escapes_injected_content():
    member = make_member(commits=[CommitRecord(
        author="zhangsan", message="<script>alert(1)</script>",
        timestamp=datetime(2026, 9, 29, 8, 30, tzinfo=TZ), repo="org/repo")])
    html = generate([member], REPORT_DATE, "研发一组").html

    assert "&lt;script&gt;" in html
    assert "<script>" not in html


def test_html_renders_friendly_line():
    """v1.4 修订：HTML 中同样渲染可读说明，不出现原始异常。"""
    member = make_member(source_errors={"github": "超时"})
    html = generate([member], REPORT_DATE, "研发一组").html

    assert "▪ 代码提交" in html
    assert "飞书接口连接失败" in html
    assert "超时" not in html


# ---------- v1.1：工时统计板块 ----------

def test_attendance_section_between_tasks_and_messages():
    markdown = generate([make_member()], REPORT_DATE, "研发一组").markdown

    assert markdown.index("### ▪ 任务进展") < markdown.index("### ▪ 工时统计") \
        < markdown.index("### ▪ 协作沟通")


def test_attendance_renders_work_hours_and_status():
    member = make_member(attendance=make_attendance())
    markdown = generate([member], REPORT_DATE, "研发一组").markdown

    assert "### ▪ 工时统计" in markdown
    assert "· 工时 9 小时（正常）" in markdown
    assert "签到 09:00" not in markdown  # v1.2 简化：不展开签到/签退时刻


def test_attendance_late_status_rendered():
    member = make_member(attendance=make_attendance(
        check_in=datetime(2026, 9, 29, 9, 30, tzinfo=TZ), work_hours=8.5, status="迟到"))
    markdown = generate([member], REPORT_DATE, "研发一组").markdown

    assert "· 工时 8.5 小时（迟到）" in markdown


def test_attendance_missing_check_out_rendered():
    member = make_member(attendance=make_attendance(check_out=None, work_hours=0.0,
                                                     status="签退缺失"))
    markdown = generate([member], REPORT_DATE, "研发一组").markdown

    assert "· 签退缺失" in markdown


def test_attendance_absent_without_punches_rendered():
    member = make_member(attendance=make_attendance(check_in=None, check_out=None,
                                                     work_hours=0.0, status="缺勤"))
    markdown = generate([member], REPORT_DATE, "研发一组").markdown

    assert "· 缺勤" in markdown


def test_attendance_no_record_shows_no_records():
    """v1.4 修订：考勤源可用但当天无记录 → 「今日无记录」（板块保留）。"""
    markdown = generate([make_member()], REPORT_DATE, "研发一组").markdown

    assert "▪ 工时统计" in markdown
    assert "· 今日无记录" in markdown


def test_attendance_no_record_rendered_in_html():
    html = generate([make_member()], REPORT_DATE, "研发一组").html

    assert "今日无记录" in html


def test_attendance_no_record_counts_as_no_records():
    member = make_member(commits=[], tasks=[], messages=[])
    markdown = generate([member], REPORT_DATE, "研发一组").markdown

    assert markdown.count("· 今日无记录") == 4  # 四个板块各一条


# ---------- §6.4 时区约定：展示统一转换为本地时区 ----------

def test_times_rendered_in_local_timezone(monkeypatch):
    """固定时区（UTC+8）下，UTC 数据应展示为本地时间（design.md §6.4）。"""
    monkeypatch.setattr(formatter, "LOCAL_TZ", timezone(timedelta(hours=8)))
    markdown = generate([make_member()], REPORT_DATE, "研发一组").markdown

    assert "· 实现登录页：未开始 → 进行中（18:00）" in markdown                # 10:00 UTC
    assert "· [团队群] 张三：评审文档已上传（18:05）" in markdown              # 10:05 UTC


# ---------- v1.2：板块简化（保留部分） ----------

def test_commit_summary_line_aggregates_totals():
    def commit(message, additions, deletions, hour):
        return CommitRecord(author="zhangsan", message=message,
                            timestamp=datetime(2026, 9, 29, hour, 0, tzinfo=TZ),
                            repo="org/repo", additions=additions, deletions=deletions,
                            files_changed=1)
    member = make_member(commits=[commit("feat: A", 10, 3, 8),
                                  commit("fix: B", 20, 6, 9)])
    markdown = generate([member], REPORT_DATE, "研发一组").markdown

    assert "· 今日 2 次提交，共 +30 / -9 行" in markdown
    assert "· ✨ 新功能（1）" in markdown  # v1.8：类别分组
    assert "  - feat: A（+10/-3 行）" in markdown
    assert "· 🐛 修复（1）" in markdown
    assert "  - fix: B（+20/-6 行）" in markdown


def test_commit_item_hides_repo_and_file_count():
    markdown = generate([make_member()], REPORT_DATE, "研发一组").markdown

    assert "[org/repo]" not in markdown  # 不展示仓库全名
    assert "个文件" not in markdown       # 不展示文件数


def test_empty_commits_have_no_summary_line():
    markdown = generate([make_member(commits=[])], REPORT_DATE, "研发一组").markdown

    assert "次提交" not in markdown
    assert "今日无记录" in markdown


def test_attendance_check_in_missing_shows_missing_line():
    member = make_member(attendance=make_attendance(check_in=None, work_hours=0.0))
    markdown = generate([member], REPORT_DATE, "研发一组").markdown

    assert "· 签到缺失" in markdown
    assert "签退 18:00" not in markdown  # 不展开打卡时刻


def test_call_failure_shows_friendly_text_not_stack():
    """v1.4 修订：调用失败显示映射后的人话；堆栈与原始错误不进日报。"""
    reason = ("github 接口返回错误状态 403: GET https://api.github.com/repos/org/repo/commits\n"
              "Traceback (most recent call last):\n  File \"x.py\", line 1, in <module>")
    member = make_member(source_errors={"github": reason})
    markdown = generate([member], REPORT_DATE, "研发一组").markdown

    assert "▪ 代码提交" in markdown
    assert "· 飞书权限不足，暂不可用" in markdown
    assert "github 接口返回错误状态 403" not in markdown
    assert "Traceback" not in markdown
    assert "File \"x.py\"" not in markdown


def test_call_failure_never_leaks_env_var_names():
    """v1.4 修订：失败原因（含环境变量名）只映射为固定文案，不进日报正文。"""
    env_var = "GITHUB_" + "TOKEN"  # 拼接避免触发敏感词检查；仅作环境变量名样例
    member = make_member(source_errors={
        "github": f"github 请求失败（已重试 3 次）: GET https://api.github.com: "
                  f"{env_var}=ghp_xxx 无效"})
    markdown = generate([member], REPORT_DATE, "研发一组").markdown

    assert "▪ 代码提交" in markdown
    assert "· 飞书数据暂不可用" in markdown
    assert "github 请求失败" not in markdown
    assert env_var not in markdown  # 环境变量名只进日志


def test_attendance_failure_shows_friendly_line():
    """v1.4 修订：考勤源不可用 → 工时统计板块保留 + 可读说明，失败原因不进正文。"""
    member = make_member(source_errors={"lark_attendance": "考勤接口超时"})
    markdown = generate([member], REPORT_DATE, "研发一组").markdown

    assert "▪ 工时统计" in markdown
    assert "· 飞书接口连接失败" in markdown
    assert "考勤数据暂不可用" not in markdown
    assert "超时" not in markdown


# ---------- v1.3：结构强化（头部 / 概览 / 落款；未接入不渲染） ----------

def test_header_window_shows_data_range():
    window = (datetime(2026, 9, 29, 0, 0, tzinfo=TZ),
              datetime(2026, 9, 29, 18, 30, tzinfo=TZ))
    markdown = generate([make_member()], REPORT_DATE, "研发一组", window=window).markdown

    assert "📅 2026-09-29 周二" in markdown
    assert "数据范围 00:00-18:30" in markdown


def test_header_without_window_has_no_data_range():
    markdown = generate([make_member()], REPORT_DATE, "研发一组").markdown

    assert "📅 2026-09-29 周二" in markdown
    assert "数据范围" not in markdown


def test_overview_summarizes_across_members():
    members = [make_member(),
               make_member(name="李四", github_username="lisi",
                           commits=[], tasks=[], messages=[])]
    markdown = generate(members, REPORT_DATE, "研发一组").markdown

    assert markdown.index("## ▎今日概览") < markdown.index("## ▎张三")
    assert "· 代码提交 1 次，共 +10 / -3 行" in markdown
    assert "· 任务进展 1 项" in markdown
    assert "· 协作沟通 1 条" in markdown
    assert "· 工时统计 0 人，共 0 小时" in markdown


def test_overview_sums_attendance_hours():
    member = make_member(attendance=make_attendance())
    markdown = generate([member], REPORT_DATE, "研发一组").markdown

    assert "· 工时统计 1 人，共 9 小时" in markdown


def test_overview_excludes_unconfigured_sources():
    """未接入的源整行不输出（design.md §6.5：概览与正文规则一致）。"""
    member = make_member(source_errors={
        "lark_task": "飞书任务未配置（collector.lark_task.project_id 为空或占位值）",
        "lark_attendance": "飞书应用凭据未配置（环境变量 LARK_APP_ID / LARK_APP_SECRET）"})
    markdown = generate([member], REPORT_DATE, "研发一组").markdown

    assert "- · 代码提交 1 次，共 +10 / -3 行" in markdown
    assert "- · 协作沟通 1 条" in markdown
    assert "- · 任务进展" not in markdown  # 未接入：整行不输出
    assert "- · 工时统计" not in markdown


def test_overview_shows_zero_for_configured_sources():
    """已接入但为 0 的源仍显示（「0 项」「0 人」是有效信息）。"""
    member = make_member(commits=[], tasks=[], messages=[])
    markdown = generate([member], REPORT_DATE, "研发一组").markdown

    assert "- · 任务进展 0 项" in markdown
    assert "- · 工时统计 0 人，共 0 小时" in markdown


def test_footer_contains_generator_and_sources():
    markdown = generate([make_member()], REPORT_DATE, "研发一组").markdown

    assert "🤖 由「智能日报生成器」自动生成" in markdown
    assert "数据来源：GitHub、飞书任务、飞书考勤、飞书消息" in markdown
    assert re.search(r"｜ 生成时间 \d{4}-\d{2}-\d{2} \d{2}:\d{2}", markdown)


def test_footer_excludes_unconfigured_sources():
    member = make_member(source_errors={
        "lark_task": "飞书任务未配置（collector.lark_task.project_id 为空或占位值）"})
    markdown = generate([member], REPORT_DATE, "研发一组").markdown

    assert "数据来源：GitHub、飞书考勤、飞书消息" in markdown
    assert "▪ 任务进展" not in markdown  # 未接入：板块不渲染
    assert "未接入" not in markdown      # 不出现任何未接入痕迹


def test_unconfigured_source_section_not_rendered():
    member = make_member(source_errors={
        "lark_task": "飞书应用凭据未配置（环境变量 LARK_APP_ID / LARK_APP_SECRET）"})
    markdown = generate([member], REPORT_DATE, "研发一组").markdown

    assert "▪ 任务进展" not in markdown
    assert "LARK_APP_ID" not in markdown
    assert "LARK_APP_SECRET" not in markdown
    assert "实现登录页" not in markdown  # 该源记录不渲染


def test_configured_source_failure_shows_friendly_line():
    """v1.4 修订：已接入但失败的源板块保留并显示可读说明；§6.1「不静默」由日志与告警保证。"""
    member = make_member(source_errors={"github": "请求超时"})
    markdown = generate([member], REPORT_DATE, "研发一组").markdown

    assert "▪ 代码提交" in markdown
    assert "· 飞书接口连接失败" in markdown
    assert "请求超时" not in markdown


def test_github_username_line_removed():
    markdown = generate([make_member()], REPORT_DATE, "研发一组").markdown

    assert "GitHub 用户名" not in markdown
    assert "zhangsan" not in markdown  # 内部标识不进入正文


def test_html_has_header_overview_and_footer():
    html = generate([make_member()], REPORT_DATE, "研发一组").html

    assert '<header class="report-head">' in html
    assert '<section class="overview">' in html
    assert '<section class="card member-card">' in html
    assert '<footer class="report-foot">' in html
    assert "🤖 由「智能日报生成器」自动生成" in html
    assert "数据来源：GitHub、飞书任务、飞书考勤、飞书消息" in html


def test_html_unconfigured_source_section_absent():
    member = make_member(source_errors={
        "lark_task": "飞书应用凭据未配置（环境变量 LARK_APP_ID / LARK_APP_SECRET）"})
    html = generate([member], REPORT_DATE, "研发一组").html

    assert "▪ 任务进展" not in html
    assert "LARK_APP_ID" not in html
    assert "LARK_APP_SECRET" not in html


# ---------- v1.4 修订：日报展示失败原因（design.md §6.5） ----------

def test_overview_keeps_failed_source_with_zero_count():
    """v1.4 修订：已接入但失败的源在概览保留一行（计数为 0），与正文一致。"""
    member = make_member(commits=[], source_errors={"github": "限流"})  # 失败时数据为空
    markdown = generate([member], REPORT_DATE, "研发一组").markdown

    assert "- · 代码提交 0 次，共 +0 / -0 行" in markdown
    assert "限流" not in markdown
    assert "- · 任务进展 1 项" in markdown


def test_footer_lists_configured_sources_including_failed():
    """v1.4 修订：落款「数据来源」列出所有已接入的源（含不可用）。"""
    member = make_member(source_errors={"github": "请求超时"})
    markdown = generate([member], REPORT_DATE, "研发一组").markdown

    assert "数据来源：GitHub、飞书任务、飞书考勤、飞书消息" in markdown


@pytest.mark.parametrize("reason,expected", [
    ("platform policy 限制", "飞书接口受限（平台策略），暂不可用"),
    ("飞书接口触发平台限制", "飞书接口受限（平台策略），暂不可用"),
    ("tenant_access_token 获取失败: app secret invalid", "飞书凭证未配置"),
    ("HTTP 403 Forbidden", "飞书权限不足，暂不可用"),
    ("permission denied", "飞书权限不足，暂不可用"),
    ("无权限访问", "飞书权限不足，暂不可用"),
    ("请求超时", "飞书接口连接失败"),
    ("connect timeout", "飞书接口连接失败"),
    ("飞书考勤接口超时", "飞书接口连接失败"),
    ("未知异常", "飞书数据暂不可用"),
])
def test_friendly_reason_mapping(reason, expected):
    """v1.4 修订：技术错误按映射表转为固定人话（design.md §6.5 表序首条命中）。"""
    assert formatter._friendly_reason(reason) == expected


def test_failure_reason_never_leaks_raw_terms():
    """v1.4 修订：正文只有映射后的人话，不出现原始异常术语/环境变量名/堆栈。"""
    member = make_member(source_errors={
        "lark_attendance": ("飞书 tenant_access_token 获取失败: app secret invalid\n"
                            "Traceback (most recent call last):\n  File \"x.py\", line 1")})
    markdown = generate([member], REPORT_DATE, "研发一组").markdown

    assert "▪ 工时统计" in markdown
    assert "· 飞书凭证未配置" in markdown
    assert "tenant_access_token" not in markdown
    assert "secret" not in markdown
    assert "Traceback" not in markdown
    assert "File \"x.py\"" not in markdown


# ---------- v1.5：HTML 视觉强化（design.md §6.5） ----------

def test_html_header_bar_with_primary_color():
    """v1.5：深色标题条（主色 #2563eb + 白字），含团队名/日期（含星期）。"""
    html = generate([make_member()], REPORT_DATE, "研发一组").html

    assert '<header class="report-head">' in html
    assert '<h1>📊 研发一组 智能日报</h1>' in html
    assert '📅 2026-09-29 周二' in html
    assert 'background: #2563eb' in html
    assert 'color: #ffffff' in html


def test_html_header_includes_data_range():
    """v1.5：标题条含数据范围（传入 window 时）。"""
    window = (datetime(2026, 9, 29, 0, 0, tzinfo=TZ),
              datetime(2026, 9, 29, 18, 30, tzinfo=TZ))
    html = generate([make_member()], REPORT_DATE, "研发一组", window=window).html

    assert '数据范围 00:00-18:30' in html


def test_html_overview_cards_with_big_numbers():
    """v1.5：概览卡片组——提交次数/变更行数/讨论条数，大号数字。"""
    html = generate([make_member()], REPORT_DATE, "研发一组").html

    assert '<section class="overview">' in html
    assert html.count('class="stat-card"') == 3
    assert '<span class="stat-value">+10/-3</span>' in html
    assert '<span class="stat-label">提交次数</span>' in html
    assert '<span class="stat-label">变更行数</span>' in html
    assert '<span class="stat-label">讨论条数</span>' in html


def test_html_overview_cards_exclude_unconfigured_sources():
    """v1.5：未接入的源不出概览卡片（与 Markdown 概览规则一致）。"""
    member = make_member(source_errors={
        "lark_msg": "飞书消息未配置（collector.lark_msg.chat_id 为空或占位值）"})
    html = generate([member], REPORT_DATE, "研发一组").html

    assert html.count('class="stat-card"') == 2  # 仅 GitHub 的提交次数/变更行数
    assert '讨论条数' not in html


def test_html_member_section_cards_with_icons():
    """v1.5：板块为浅色卡片（#f9fafb），标题带图标与正常徽章（成功色 #16a34a）。"""
    html = generate([make_member()], REPORT_DATE, "研发一组").html

    assert '<section class="card member-card">' in html
    assert 'background: #f9fafb' in html
    assert '💻 ▪ 代码提交' in html
    assert '📋 ▪ 任务进展' in html
    assert '⏱ ▪ 工时统计' in html
    assert '💬 ▪ 协作沟通' in html
    assert '<span class="badge badge-ok">✅ 正常</span>' in html
    assert 'color: #16a34a' in html


def test_html_warning_badge_for_unavailable_section():
    """v1.5：不可用的板块带 ⚠️ 徽章（警告色 #f59e0b），其余板块保持 ✅。"""
    member = make_member(source_errors={"github": "请求超时"})
    html = generate([member], REPORT_DATE, "研发一组").html

    assert '<span class="badge badge-warn">⚠️ 不可用</span>' in html
    assert 'color: #f59e0b' in html
    assert '<span class="badge badge-ok">✅ 正常</span>' in html


def test_html_footer_light_gray_centered():
    """v1.5：落款浅灰背景（#f3f4f6）、居中、小字。"""
    html = generate([make_member()], REPORT_DATE, "研发一组").html

    assert '<footer class="report-foot">' in html
    assert 'background: #f3f4f6' in html
    assert 'text-align: center' in html
    assert '🤖 由「智能日报生成器」自动生成' in html
    assert '数据来源：GitHub、飞书任务、飞书考勤、飞书消息' in html


def test_html_responsive_stacks_cards_on_narrow_screens():
    """v1.5：窄屏（<600px）概览卡片纵向堆叠（@media + display:block 降级）。"""
    html = generate([make_member()], REPORT_DATE, "研发一组").html

    assert '@media (max-width: 600px)' in html
    assert 'display: block' in html


def test_markdown_output_unchanged_by_v1_5():
    """v1.5：Markdown（飞书卡片）结构与内容不受 HTML 模板改造影响。"""
    window = (datetime(2026, 9, 29, 0, 0, tzinfo=TZ),
              datetime(2026, 9, 29, 18, 30, tzinfo=TZ))
    markdown = generate([make_member()], REPORT_DATE, "研发一组", window=window).markdown

    assert markdown.startswith("# 📊 研发一组 智能日报\n\n📅 2026-09-29 周二"
                               " · 数据范围 00:00-18:30\n")
    assert "## ▎今日概览" in markdown
    assert "### ▪ 代码提交" in markdown
    assert "💻" not in markdown          # 图标仅用于 HTML
    assert "✅ 正常" not in markdown     # 徽章仅用于 HTML
    assert "stat-card" not in markdown   # HTML 组件不进 Markdown


# ---------- v1.6：趋势与周报（design.md §6.5） ----------

def _history(**overrides):
    defaults = dict(
        week={"start": date(2026, 9, 28), "end": REPORT_DATE, "commits": 12,
              "additions": 100, "deletions": 40, "active_days": 3},
        last_week={"start": date(2026, 9, 21), "end": date(2026, 9, 22),
                   "commits": 8, "additions": 60, "deletions": 30, "active_days": 2},
        yesterday={"commits": 3, "additions": 20, "deletions": 8},
    )
    defaults.update(overrides)
    return defaults


def test_week_summary_rendered_before_today_overview():
    """v1.6：本周概况板块位于今日概览之前，含提交/变更/活跃天数/较上周对比。"""
    markdown = generate([make_member()], REPORT_DATE, "研发一组",
                        history=_history()).markdown

    assert markdown.index("## ▎本周概况") < markdown.index("## ▎今日概览")
    assert markdown.index("## ▎今日概览") < markdown.index("## ▎张三")  # 既有结构不变
    assert "· 提交 12 次 ｜ 变更 +100 / -40 行" in markdown
    assert "· 活跃 3 天 ｜ 📈 较上周：提交 +4 次" in markdown


def test_week_summary_negative_diff_shows_minus():
    """v1.6：本周提交少于上周时「较上周」显示负数。"""
    history = _history(week={"start": date(2026, 9, 28), "end": REPORT_DATE,
                             "commits": 5, "additions": 40, "deletions": 20,
                             "active_days": 2})
    markdown = generate([make_member()], REPORT_DATE, "研发一组",
                        history=history).markdown

    assert "· 活跃 2 天 ｜ 📈 较上周：提交 -3 次" in markdown


def test_overview_appends_yesterday_comparison():
    """v1.6：今日概览代码提交行追加「较昨日」对比（今日 1 次 - 昨日 3 次 = -2）。"""
    markdown = generate([make_member()], REPORT_DATE, "研发一组",
                        history=_history()).markdown

    assert "· 代码提交 1 次，共 +10 / -3 行 ｜ 📈 较昨日 -2 次" in markdown


def test_yesterday_none_omits_comparison():
    """v1.6：无昨日统计时今日概览不追加对比（仅省略，不影响其余概览行）。"""
    markdown = generate([make_member()], REPORT_DATE, "研发一组",
                        history=_history(yesterday=None)).markdown

    assert "· 代码提交 1 次，共 +10 / -3 行" in markdown
    assert "较昨日" not in markdown
    assert "· 任务进展 1 项" in markdown  # 其余概览行不变


def test_no_history_shows_placeholder_without_error():
    """v1.6：history 为 None 时本周概况显示「暂无历史数据」，不报错、无对比。"""
    markdown = generate([make_member()], REPORT_DATE, "研发一组").markdown

    assert "## ▎本周概况" in markdown
    assert "· 暂无历史数据" in markdown
    assert "较昨日" not in markdown
    assert "较上周" not in markdown


def test_history_none_markdown_structure_backward_compatible():
    """v1.6：history 缺省为 None，除新增本周概况板块外既有结构与内容不变。"""
    markdown = generate([make_member()], REPORT_DATE, "研发一组").markdown

    assert markdown.startswith("# 📊 研发一组 智能日报\n\n📅 2026-09-29 周二\n")
    assert markdown.index("## ▎今日概览") < markdown.index("## ▎张三")
    assert "### ▪ 代码提交" in markdown
    assert "🤖 由「智能日报生成器」自动生成" in markdown


def test_html_week_section_reuses_card_style_and_shows_comparisons():
    """v1.6：HTML 本周概况复用成员卡片样式；概览含「较昨日提交」卡片。"""
    html = generate([make_member()], REPORT_DATE, "研发一组",
                    history=_history()).html

    assert '<section class="card member-card">' in html
    assert "▎本周概况" in html
    assert "提交 12 次 ｜ 变更 +100 / -40 行" in html
    assert "📈 较上周：提交 +4 次" in html
    assert '<span class="stat-label">较昨日提交</span>' in html
    assert '<span class="stat-value">-2 次</span>' in html


def test_html_no_history_shows_placeholder_card():
    """v1.6：无历史数据时 HTML 本周概况卡片显示「暂无历史数据」，无较昨日卡片。"""
    html = generate([make_member()], REPORT_DATE, "研发一组").html

    assert "▎本周概况" in html
    assert "暂无历史数据" in html
    assert "较昨日提交" not in html


# ---------- v1.8：日报内容智能化（design.md §6.5） ----------

def _commit(message, additions=10, deletions=3, hour=8):
    return CommitRecord(author="zhangsan", message=message,
                        timestamp=datetime(2026, 9, 29, hour, 0, tzinfo=TZ),
                        repo="org/repo", additions=additions, deletions=deletions,
                        files_changed=1)


def test_overview_shows_work_categories_nonzero_only():
    """v1.8：概览「工作分类」行按固定顺序只列非零类别。"""
    member = make_member(commits=[_commit("feat: A"), _commit("fix: B"),
                                  _commit("feat: C"), _commit("更新依赖")])
    markdown = generate([member], REPORT_DATE, "研发一组").markdown

    assert "· 工作分类：✨ 新功能 2 ｜ 🐛 修复 1 ｜ 📦 其他 1" in markdown


def test_overview_work_categories_fixed_order():
    """v1.8：类别按固定顺序输出，与提交时间先后无关。"""
    member = make_member(commits=[_commit("fix: B", hour=9), _commit("feat: A", hour=8)])
    markdown = generate([member], REPORT_DATE, "研发一组").markdown

    assert "· 工作分类：✨ 新功能 1 ｜ 🐛 修复 1" in markdown


def test_overview_no_work_categories_without_commits():
    member = make_member(commits=[])
    markdown = generate([member], REPORT_DATE, "研发一组").markdown

    assert "工作分类" not in markdown


def test_commits_grouped_by_category_time_ascending():
    """v1.8：提交板块按类别分组，组顺序固定，组内按提交时间升序。"""
    member = make_member(commits=[_commit("fix: B", hour=9), _commit("feat: A", hour=8),
                                  _commit("feat: C", hour=10)])
    markdown = generate([member], REPORT_DATE, "研发一组").markdown

    assert "· 今日 3 次提交，共 +30 / -9 行" in markdown
    assert "· ✨ 新功能（2）" in markdown
    assert "  - feat: A（+10/-3 行）" in markdown
    assert "  - feat: C（+10/-3 行）" in markdown
    assert "· 🐛 修复（1）" in markdown
    assert "  - fix: B（+10/-3 行）" in markdown
    # 组顺序固定：feat 组在 fix 组之前
    assert markdown.index("· ✨ 新功能（2）") < markdown.index("· 🐛 修复（1）")
    # 组内按时间升序（比较提交板块内的完整子条目，避开「今日亮点」里的同款标题）
    assert markdown.index("  - feat: A（+10/-3 行）") < markdown.index("  - feat: C（+10/-3 行）")


def test_highlights_max3_by_priority_newest_per_category():
    """v1.8：今日亮点最多 3 条，按优先级取；同类取最新（feat 取 C 不取 A）。"""
    member = make_member(commits=[
        _commit("feat: A", hour=8), _commit("feat: C", hour=11),
        _commit("fix: B", hour=9), _commit("perf: D", hour=10),
        _commit("refactor: E", hour=7), _commit("更新依赖", hour=12),
    ])
    markdown = generate([member], REPORT_DATE, "研发一组").markdown

    assert markdown.index("## ▎今日亮点") < markdown.index("## ▎张三")  # 概览之后、成员之前
    highlight_block = markdown.split("## ▎今日亮点")[1].split("## ▎张三")[0]
    assert highlight_block.count("- · ") == 3  # 最多 3 条
    assert "· ✨ feat: C（张三，+10/-3 行）" in highlight_block
    assert "· 🐛 fix: B（张三，+10/-3 行）" in highlight_block
    assert "· ⚡ perf: D（张三，+10/-3 行）" in highlight_block
    assert "refactor" not in highlight_block  # 超出 3 条被截断
    assert "feat: A" not in highlight_block   # 同类只取最新


def test_highlights_same_category_across_members_takes_newest():
    """v1.8：同类取最新按全团队比较，与成员无关。"""
    m1 = make_member(commits=[_commit("feat: 张三早", hour=8)])
    m2 = make_member(name="李四", github_username="lisi",
                     commits=[_commit("feat: 李四晚", hour=10)], tasks=[], messages=[])
    markdown = generate([m1, m2], REPORT_DATE, "研发一组").markdown

    highlight_block = markdown.split("## ▎今日亮点")[1].split("## ▎张三")[0]
    assert "· ✨ feat: 李四晚（李四，+10/-3 行）" in highlight_block
    assert "feat: 张三早" not in highlight_block


def test_highlights_priority_order_feat_before_fix():
    """v1.8：亮点按优先级 feat > fix 排序，与提交时间无关。"""
    member = make_member(commits=[_commit("fix: B", hour=10), _commit("feat: A", hour=8)])
    markdown = generate([member], REPORT_DATE, "研发一组").markdown

    highlight_block = markdown.split("## ▎今日亮点")[1].split("## ▎张三")[0]
    assert highlight_block.index("feat: A") < highlight_block.index("fix: B")


def test_highlights_not_rendered_without_commits():
    """v1.8：当天无提交时不渲染「今日亮点」（Markdown 与 HTML 一致）。"""
    member = make_member(commits=[], tasks=[], messages=[])
    markdown = generate([member], REPORT_DATE, "研发一组").markdown
    html = generate([member], REPORT_DATE, "研发一组").html

    assert "今日亮点" not in markdown
    assert "今日亮点" not in html


def test_html_highlights_reuses_card_style():
    """v1.8：HTML 今日亮点复用成员卡片样式，位于概览之后、成员板块之前。"""
    html = generate([make_member()], REPORT_DATE, "研发一组").html

    assert "▎今日亮点" in html
    assert "feat: 登录页" in html
    assert html.index('class="overview"') < html.index("▎今日亮点") < html.index("▎张三")
    # 本周概况 + 今日亮点 + 成员卡片，共用 .card.member-card 样式
    assert html.count('<section class="card member-card">') == 3


# ---------- v1.8 修订：概览 active 按成员聚合（design.md §6.5） ----------

def test_overview_active_when_any_member_configured():
    """任一成员已接入 GitHub 即团队激活：B 未配置不掩盖 A 的提交
    （概览代码提交行、工作分类行、落款数据来源均保留；B 的板块仍不渲染）。"""
    a = make_member(commits=[_commit("feat: A", hour=8), _commit("fix: B", hour=9)])
    b = make_member(name="李四", github_username="lisi", commits=[], tasks=[],
                    messages=[], source_errors={"github": "GitHub 凭据未配置"})
    markdown = generate([a, b], REPORT_DATE, "研发一组").markdown

    assert "· 代码提交 2 次，共 +20 / -6 行" in markdown
    assert "· 工作分类：✨ 新功能 1 ｜ 🐛 修复 1" in markdown
    assert "数据来源：GitHub、飞书任务、飞书考勤、飞书消息" in markdown
    # 板块级规则不变：B 的「代码提交」板块仍不渲染
    assert "▪ 代码提交" not in markdown.split("## ▎李四")[1]


def test_overview_inactive_when_all_members_unconfigured():
    """所有成员都未配置 github → 概览不出现代码提交行与工作分类行。"""
    member = make_member(commits=[], tasks=[], messages=[],
                         source_errors={"github": "GitHub 凭据未配置"})
    markdown = generate([member], REPORT_DATE, "研发一组").markdown

    assert "· 代码提交" not in markdown
    assert "工作分类" not in markdown
    assert "数据来源：飞书任务、飞书考勤、飞书消息" in markdown


def test_overview_all_inactive_without_members():
    """members 为空时所有源视为未激活（v1.8 修订边界）。"""
    markdown = generate([], REPORT_DATE, "研发一组").markdown

    assert "· 代码提交" not in markdown
    assert "工作分类" not in markdown


# ---------- v1.9：未归属记录归集（design.md §6.5） ----------

def test_unassigned_member_rendered_last_with_data_sections_only():
    """未归属伪成员排在所有成员之后（传入顺序无关）；只渲染有数据的板块，
    不输出「今日无记录」空板块。"""
    normal = make_member()
    unassigned = make_member(name=formatter.UNASSIGNED_MEMBER_NAME, github_username="",
                             commits=[_commit("hotfix typo in report", hour=16)],
                             tasks=[], messages=[], is_unassigned=True)
    markdown = generate([unassigned, normal], REPORT_DATE, "研发一组").markdown

    assert markdown.index("## ▎张三") < markdown.index("## ▎其他贡献")
    block = markdown.split("## ▎其他贡献")[1]
    assert "hotfix typo in report" in block
    assert "▪ 代码提交" in block
    assert "▪ 任务进展" not in block
    assert "▪ 工时统计" not in block
    assert "▪ 协作沟通" not in block
    assert "今日无记录" not in block
    # HTML 成员板块顺序与 Markdown 一致
    html = generate([unassigned, normal], REPORT_DATE, "研发一组").html
    assert html.index("▎张三") < html.index("▎其他贡献")


def test_overview_categories_and_highlights_include_unassigned():
    """v1.9：今日概览、工作分类、今日亮点把「其他贡献」当作普通成员计入。"""
    normal = make_member()
    unassigned = make_member(name=formatter.UNASSIGNED_MEMBER_NAME, github_username="",
                             commits=[_commit("fix: 修复文案", hour=16)],
                             tasks=[], messages=[], is_unassigned=True)
    markdown = generate([normal, unassigned], REPORT_DATE, "研发一组").markdown

    assert "· 代码提交 2 次，共 +20 / -6 行" in markdown
    assert "· 工作分类：✨ 新功能 1 ｜ 🐛 修复 1" in markdown
    # 亮点：fix 类别最新为伪成员的提交，与真实成员按同一优先级规则参与
    assert "· 🐛 fix: 修复文案（其他贡献，+10/-3 行）" in markdown


def test_unassigned_member_ignored_for_team_source_availability():
    """伪成员 source_errors 为空也不激活数据源：所有真实成员未配置 github 时，
    概览不出现代码提交行与工作分类行（_configured_sources 忽略伪成员）。"""
    real = make_member(commits=[], tasks=[], messages=[],
                       source_errors={"github": "GitHub 凭据未配置"})
    unassigned = make_member(name=formatter.UNASSIGNED_MEMBER_NAME, github_username="",
                             commits=[_commit("fix: X")], tasks=[], messages=[],
                             is_unassigned=True)
    markdown = generate([real, unassigned], REPORT_DATE, "研发一组").markdown

    assert "- · 代码提交" not in markdown
    assert "工作分类" not in markdown
    assert "## ▎其他贡献" in markdown          # 伪成员段落照常渲染
    assert "fix: X" in markdown


def test_unassigned_member_messages_show_unrecognized_speaker():
    """v1.9：伪成员的消息行发言人显示「未识别成员」——
    不暴露 open_id 等内部标识，也不冒充具体成员姓名。"""
    member = make_member(name=formatter.UNASSIGNED_MEMBER_NAME, github_username="",
                         commits=[], tasks=[],
                         messages=[MessageRecord(
                             sender="ou_newbie_openid", content="登录联调已通过",
                             timestamp=datetime(2026, 9, 29, 17, 5, tzinfo=TZ),
                             chat_name="研发一组")],
                         is_unassigned=True)
    markdown = generate([member], REPORT_DATE, "研发一组").markdown

    assert "· [研发一组] 未识别成员：登录联调已通过（17:05）" in markdown
    assert "其他贡献：" not in markdown       # 段落名不作为发言人出现
    assert "ou_newbie_openid" not in markdown  # 内部标识不进入日报正文


def test_normal_member_messages_still_show_member_name():
    """普通成员的消息行仍以成员姓名发言（v1.9 不影响既有展示规则）。"""
    markdown = generate([make_member()], REPORT_DATE, "研发一组").markdown

    assert "· [团队群] 张三：评审文档已上传（10:05）" in markdown
    assert "未识别成员" not in markdown
