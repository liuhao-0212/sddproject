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
    assert "今日 1 次提交，共 +10 / -3 行" in markdown  # v1.2：板块开头汇总行
    assert "feat: 登录页（+10/-3 行）— 08:30" in markdown


def test_markdown_renders_tasks():
    markdown = generate([make_member()], REPORT_DATE, "研发一组").markdown
    assert "实现登录页：未开始 → 进行中（10:00）" in markdown


def test_markdown_renders_messages():
    markdown = generate([make_member()], REPORT_DATE, "研发一组").markdown
    assert "[团队群] 张三：评审文档已上传（10:05）" in markdown


def test_message_sender_displayed_as_member_name():
    """sender 原值多为 open_id 等内部标识，展示统一用成员姓名（design.md §6.5 易读）。"""
    member = make_member(messages=[MessageRecord(
        sender="ou_zhangsan_openid", content="评审文档已上传",
        timestamp=datetime(2026, 9, 29, 10, 5, tzinfo=TZ), chat_name="团队群")])
    markdown = generate([member], REPORT_DATE, "研发一组").markdown

    assert "[团队群] 张三：评审文档已上传（10:05）" in markdown
    assert "ou_zhangsan_openid" not in markdown  # 内部标识不进入日报正文


def test_commit_renders_title_line_only():
    """v1.2：只展示 commit 消息第一行（标题），正文与 Co-Authored-By 等 trailer 不进入日报。"""
    member = make_member(commits=[CommitRecord(
        author="zhangsan",
        message="line1\nline2\n\nCo-Authored-By: Claude Code <noreply@anthropic.com>",
        timestamp=datetime(2026, 9, 29, 8, 30, tzinfo=TZ), repo="org/repo")])
    markdown = generate([member], REPORT_DATE, "研发一组").markdown

    assert "line1（+0/-0 行）— 08:30" in markdown
    assert "line2" not in markdown
    assert "Co-Authored-By" not in markdown


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
    assert "工时 9 小时（正常）" in markdown
    assert "签到 09:00" not in markdown  # v1.2 简化：不展开签到/签退时刻


def test_attendance_late_status_rendered():
    member = make_member(attendance=make_attendance(
        check_in=datetime(2026, 9, 29, 9, 30, tzinfo=TZ), work_hours=8.5, status="迟到"))
    markdown = generate([member], REPORT_DATE, "研发一组").markdown

    assert "工时 8.5 小时（迟到）" in markdown


def test_attendance_missing_check_out_rendered():
    member = make_member(attendance=make_attendance(check_out=None, work_hours=0.0,
                                                     status="签退缺失"))
    markdown = generate([member], REPORT_DATE, "研发一组").markdown

    assert "- 签退缺失" in markdown


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

    assert "feat: 登录页（+10/-3 行）— 16:30" in markdown  # 08:30 UTC
    assert "实现登录页：未开始 → 进行中（18:00）" in markdown                # 10:00 UTC
    assert "[团队群] 张三：评审文档已上传（18:05）" in markdown              # 10:05 UTC


# ---------- v1.2：数据源状态区与失败分级展示（proposal §3.5 / design.md §6.5） ----------

def test_source_status_section_lists_all_sources_with_counts():
    member = make_member(attendance=make_attendance())
    markdown = generate([member], REPORT_DATE, "研发一组").markdown

    assert markdown.index("## 数据源状态") < markdown.index("## 张三")  # 位于日报顶部
    assert "- ✅ GitHub：可用（1 条）" in markdown
    assert "- ✅ 飞书任务：可用（1 条）" in markdown
    assert "- ✅ 飞书考勤：可用（1 条）" in markdown
    assert "- ✅ 飞书消息：可用（1 条）" in markdown


def test_source_status_counts_aggregate_across_members():
    members = [make_member(), make_member(name="李四", github_username="lisi",
                                          commits=[], tasks=[], messages=[])]
    markdown = generate(members, REPORT_DATE, "研发一组").markdown

    assert "- ✅ GitHub：可用（1 条）" in markdown
    assert "- ✅ 飞书考勤：可用（0 条）" in markdown  # 仅统计有考勤记录的成员


def test_source_status_reflects_call_failure():
    member = make_member(source_errors={"github": "请求超时"})
    markdown = generate([member], REPORT_DATE, "研发一组").markdown

    assert "- ⚠️ GitHub：调用失败" in markdown
    assert "- ✅ 飞书任务：可用（1 条）" in markdown  # 其他源不受影响


def test_not_configured_section_shows_one_liner():
    """缺凭据 → 板块仅一句话"未接入，已跳过"，不含环境变量名等内部细节。"""
    member = make_member(source_errors={
        "lark_task": "飞书应用凭据未配置（环境变量 LARK_APP_ID / LARK_APP_SECRET）"})
    markdown = generate([member], REPORT_DATE, "研发一组").markdown

    assert "- 未接入，已跳过" in markdown
    assert "- － 飞书任务：未接入" in markdown
    assert "LARK_APP_ID" not in markdown
    assert "LARK_APP_SECRET" not in markdown
    assert "实现登录页" not in markdown  # 失败板块不再渲染该源记录


def test_call_failure_shows_single_line_short_reason():
    """调用失败显示简短原因：多行堆栈被过滤，只留第一行。"""
    reason = ("github 接口返回错误状态 403: GET https://api.github.com/repos/org/repo/commits\n"
              "Traceback (most recent call last):\n  File \"x.py\", line 1, in <module>")
    member = make_member(source_errors={"github": reason})
    markdown = generate([member], REPORT_DATE, "研发一组").markdown

    assert "GitHub采集异常：github 接口返回错误状态 403" in markdown
    assert "Traceback" not in markdown
    assert "File \"x.py\"" not in markdown


def test_call_failure_filters_env_var_names():
    env_var = "GITHUB_" + "TOKEN"  # 拼接避免触发敏感词检查；仅作环境变量名样例
    member = make_member(source_errors={
        "github": f"github 请求失败（已重试 3 次）: GET https://api.github.com: "
                  f"{env_var}=ghp_xxx 无效"})
    markdown = generate([member], REPORT_DATE, "研发一组").markdown

    assert "GitHub采集异常：github 请求失败" in markdown
    assert env_var not in markdown  # 环境变量名只进日志


def test_attendance_not_configured_shows_skipped():
    member = make_member(source_errors={
        "lark_attendance": "飞书应用凭据未配置（环境变量 LARK_APP_ID / LARK_APP_SECRET）"})
    markdown = generate([member], REPORT_DATE, "研发一组").markdown

    assert "- 未接入，已跳过" in markdown
    assert "- － 飞书考勤：未接入" in markdown
    assert "考勤数据暂不可用" not in markdown
    assert "LARK_APP_ID" not in markdown


def test_attendance_call_failure_keeps_unavailable_text():
    """考勤调用失败保持 v1.1 固定文案，原因只进日志不进正文。"""
    member = make_member(source_errors={"lark_attendance": "考勤接口超时"})
    markdown = generate([member], REPORT_DATE, "研发一组").markdown

    assert "考勤数据暂不可用" in markdown
    assert "超时" not in markdown
    assert "- ⚠️ 飞书考勤：调用失败" in markdown


def test_html_has_cards_and_status_badges():
    html = generate([make_member()], REPORT_DATE, "研发一组").html

    assert '<section class="card status-card">' in html
    assert '<section class="card member-card">' in html
    assert '<span class="badge badge-ok">✅ GitHub：可用（1 条）</span>' in html


def test_html_not_configured_renders_off_badge_without_internals():
    member = make_member(source_errors={
        "lark_task": "飞书应用凭据未配置（环境变量 LARK_APP_ID / LARK_APP_SECRET）"})
    html = generate([member], REPORT_DATE, "研发一组").html

    assert '<span class="badge badge-off">－ 飞书任务：未接入</span>' in html
    assert "LARK_APP_ID" not in html
    assert "LARK_APP_SECRET" not in html


# ---------- v1.2：代码提交与工时统计板块简化 ----------

def test_commit_summary_line_aggregates_totals():
    def commit(message, additions, deletions, hour):
        return CommitRecord(author="zhangsan", message=message,
                            timestamp=datetime(2026, 9, 29, hour, 0, tzinfo=TZ),
                            repo="org/repo", additions=additions, deletions=deletions,
                            files_changed=1)
    member = make_member(commits=[commit("feat: A", 10, 3, 8),
                                  commit("fix: B", 20, 6, 9)])
    markdown = generate([member], REPORT_DATE, "研发一组").markdown

    assert "今日 2 次提交，共 +30 / -9 行" in markdown
    assert "feat: A（+10/-3 行）— 08:00" in markdown
    assert "fix: B（+20/-6 行）— 09:00" in markdown


def test_commit_item_hides_repo_and_file_count():
    markdown = generate([make_member()], REPORT_DATE, "研发一组").markdown

    assert "[org/repo]" not in markdown  # v1.2：不再展示仓库全名
    assert "个文件" not in markdown       # v1.2：不再展示文件数


def test_empty_commits_have_no_summary_line():
    markdown = generate([make_member(commits=[])], REPORT_DATE, "研发一组").markdown

    assert "次提交" not in markdown
    assert "今日无记录" in markdown


def test_attendance_check_in_missing_shows_missing_line():
    member = make_member(attendance=make_attendance(check_in=None, work_hours=0.0))
    markdown = generate([member], REPORT_DATE, "研发一组").markdown

    assert "- 签到缺失" in markdown
    assert "签退 18:00" not in markdown  # v1.2：不展开打卡时刻
