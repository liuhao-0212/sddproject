"""日报生成：数据整理 + Markdown 生成 + Markdown→HTML 转换（design.md §4.2）。

- generate(members, date, team_name, window=None) -> DailyReport
  window=(since, until)：采集窗口（本地时区 datetime），用于头部「数据范围 HH:MM-HH:MM」
- 结构（v1.3，面向非技术读者，design.md §6.5）：
  头部（📊 标题·团队名 / 📅 日期含星期 / 数据范围）
  → ▎今日概览（汇总数字）→ 各成员段落（▎成员 → ▪板块 → ·条目）→ 落款
- 未接入的源（缺凭据/占位值）：板块不渲染；已接入但失败的源必须显示
  「数据获取失败」+ 过滤后的简短原因（design.md §6.1 严禁静默跳过）
- 代码提交：每条只展示消息标题行 + 增删行数（不含仓库全名/文件数/trailer/时间）
- 工时统计：一行展示工时与出勤状态；考勤不可用标注「考勤数据暂不可用」（proposal §3.4）
- 异常细节（堆栈、环境变量名、内部 ID）只进日志，不进日报正文
- 日报仅含统计信息，不含代码差异内容（design.md §6.2）
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import date, datetime
from html import escape

from collector.github import CommitRecord
from collector.lark_attendance import AttendanceRecord
from collector.lark_msg import MessageRecord
from collector.lark_task import TaskRecord
from generator.template import render_html

# 数据源展示顺序：概览/落款与正文板块顺序一致（代码提交 → 任务进展 → 工时统计 → 协作沟通）
SOURCE_ORDER = [
    ("github", "GitHub"),
    ("lark_task", "飞书任务"),
    ("lark_attendance", "飞书考勤"),
    ("lark_msg", "飞书消息"),
]
# 数据源 key → 展示名（与编排层填充 MemberReport.source_errors 的 key 约定一致）
SOURCE_LABELS = dict(SOURCE_ORDER)

NO_RECORDS_TEXT = "今日无记录"
FAILURE_TEXT = "数据获取失败"
ATTENDANCE_UNAVAILABLE_TEXT = "考勤数据暂不可用"

# 「未接入」（缺凭据/未授权/占位值）判定关键词（design.md §6.5）：与采集层的权威错误文案
# 及编排层跳过采集时填充的原因对齐；命中时板块整体不渲染（v1.3），原因只进日志。
NOT_CONFIGURED_KEYWORDS = ("凭据未配置", "未配置", "未授权", "缺少飞书用户标识")

# 环境变量名模式（大写字母 + 下划线），用于过滤「调用失败」原因中的内部细节
_ENV_VAR_RE = re.compile(r"\b[A-Z][A-Z0-9]*_[A-Z0-9_]+\b")

# 星期展示（date.weekday(): 0=周一）
WEEKDAYS = ("周一", "周二", "周三", "周四", "周五", "周六", "周日")

# 系统本地时区（design.md §6.4：日报中所有时间统一转换为本地时区后显示）
LOCAL_TZ = datetime.now().astimezone().tzinfo


def _local(dt: datetime) -> datetime:
    """转换为本地时区用于展示（design.md §6.4；naive 输入视为已是本地时间）。"""
    return dt.astimezone(LOCAL_TZ)


@dataclass
class MemberReport:
    """成员报告（design.md §3.2）。

    source_errors 为 §6.1 失败标注的扩展：数据源 key → 失败原因，
    由编排层（main.py）在采集失败或未配置时填入，生成层据此分级展示。
    """

    name: str                        # 成员姓名
    github_username: str             # GitHub 用户名
    commits: list[CommitRecord] = field(default_factory=list)    # 代码提交记录
    tasks: list[TaskRecord] = field(default_factory=list)        # 任务变更记录
    messages: list[MessageRecord] = field(default_factory=list)  # 相关消息记录
    source_errors: dict[str, str] = field(default_factory=dict)  # 数据源失败标注
    attendance: AttendanceRecord | None = None  # 考勤记录（v1.1 新增；None 视为不可用）


@dataclass
class DailyReport:
    """每日报告（design.md §3.2）。"""

    date: date                    # 日报日期
    team_name: str                # 团队名称
    members: list[MemberReport]   # 各成员的日报段落
    generated_at: datetime        # 生成时间
    markdown: str                 # 完整 Markdown 格式日报
    html: str                     # 完整 HTML 格式日报


def generate(members: list[MemberReport], date: date, team_name: str,
             window: tuple[datetime, datetime] | None = None) -> DailyReport:
    """生成日报（design.md §4.2）。window 为采集窗口 (since, until)，用于头部数据范围。"""
    generated_at = datetime.now()
    markdown = _build_markdown(members, date, team_name, window, generated_at)
    html = render_html(markdown, date, team_name)
    return DailyReport(
        date=date,
        team_name=team_name,
        members=members,
        generated_at=generated_at,
        markdown=markdown,
        html=html,
    )


def _build_markdown(members: list[MemberReport], report_date: date, team_name: str,
                    window: tuple[datetime, datetime] | None,
                    generated_at: datetime) -> str:
    """v1.3 结构：头部 → 今日概览 → 成员段落 → 落款。"""
    lines = _render_header(team_name, report_date, window)
    lines += _render_overview(members)
    for member in members:
        lines.extend(_render_member(member))
    lines += _render_footer(members, generated_at)
    return "\n".join(lines) + "\n"


def _render_header(team_name: str, report_date: date,
                   window: tuple[datetime, datetime] | None) -> list[str]:
    """头部（v1.3）：📊 标题·团队名；📅 日期含星期；数据范围 HH:MM-HH:MM。"""
    lines = [f"# 📊 {team_name}日报", ""]
    meta = f"📅 {report_date.isoformat()} {WEEKDAYS[report_date.weekday()]}"
    if window:
        since, until = window
        meta += f" · 数据范围 {_local(since):%H:%M}-{_local(until):%H:%M}"
    lines += [meta, ""]
    return lines


def _render_overview(members: list[MemberReport]) -> list[str]:
    """今日概览（v1.3）：全团队汇总数字，位于成员段落之前；
    未接入的源整行不输出（与正文板块规则一致，design.md §6.5）；
    已接入但为 0 的源仍显示（「0 项」「0 人」是有效信息）。"""
    errors: dict[str, str] = {}
    for member in members:
        for source, reason in member.source_errors.items():
            errors.setdefault(source, reason)

    def active(source: str) -> bool:
        return not _is_not_configured(errors.get(source, ""))

    lines = ["## ▎今日概览", ""]
    if active("github"):
        commits = sum(len(m.commits) for m in members)
        additions = sum(c.additions for m in members for c in m.commits)
        deletions = sum(c.deletions for m in members for c in m.commits)
        lines.append(f"- · 代码提交 {commits} 次，共 +{additions} / -{deletions} 行")
    if active("lark_task"):
        lines.append(f"- · 任务进展 {sum(len(m.tasks) for m in members)} 项")
    if active("lark_attendance"):
        attended = [m.attendance for m in members
                    if m.attendance is not None
                    and m.attendance.check_in and m.attendance.check_out]
        hours = sum(a.work_hours for a in attended)
        lines.append(f"- · 工时统计 {len(attended)} 人，共 {hours:g} 小时")
    if active("lark_msg"):
        lines.append(f"- · 协作沟通 {sum(len(m.messages) for m in members)} 条")
    lines.append("")
    return lines


def _render_member(member: MemberReport) -> list[str]:
    """成员段落（v1.3）：▎成员 → ▪板块 → ·条目；未接入的板块不渲染。"""
    lines = [f"## ▎{member.name}", ""]
    for section_title, section_lines in (
        ("代码提交", _render_commits(member)),
        ("任务进展", _render_tasks(member)),
        ("工时统计", _render_attendance(member)),
        ("协作沟通", _render_messages(member)),
    ):
        if section_lines:
            lines += [f"### ▪ {section_title}", "", *section_lines]
    lines += [""]
    return lines


def _render_commits(member: MemberReport) -> list[str]:
    if _unconfigured(member, "github"):
        return []  # v1.3：未接入的源板块不渲染
    if "github" in member.source_errors:
        return [f"- · {_failure_text('github', member)}", ""]
    commits = sorted(member.commits, key=lambda c: c.timestamp)
    if not commits:
        return [f"- · {NO_RECORDS_TEXT}", ""]
    additions = sum(c.additions for c in commits)
    deletions = sum(c.deletions for c in commits)
    lines = [f"- · 今日 {len(commits)} 次提交，共 +{additions} / -{deletions} 行"]
    for commit in commits:
        title = commit.message.splitlines()[0].strip()  # 标题行；trailer（Co-Authored-By 等）在正文中
        lines.append(f"- · {title}（+{commit.additions}/-{commit.deletions} 行）")
    lines.append("")
    return lines


def _render_tasks(member: MemberReport) -> list[str]:
    if _unconfigured(member, "lark_task"):
        return []
    if "lark_task" in member.source_errors:
        return [f"- · {_failure_text('lark_task', member)}", ""]
    if not member.tasks:
        return [f"- · {NO_RECORDS_TEXT}", ""]
    lines = []
    for task in sorted(member.tasks, key=lambda t: t.updated_at):
        lines.append(f"- · {task.title}：{task.status_from} → {task.status_to}"
                     f"（{_local(task.updated_at):%H:%M}）")
    lines.append("")
    return lines


def _render_messages(member: MemberReport) -> list[str]:
    if _unconfigured(member, "lark_msg"):
        return []
    if "lark_msg" in member.source_errors:
        return [f"- · {_failure_text('lark_msg', member)}", ""]
    if not member.messages:
        return [f"- · {NO_RECORDS_TEXT}", ""]
    lines = []
    for message in sorted(member.messages, key=lambda m: m.timestamp):
        content = message.content.replace("\n", " ").strip()
        # 消息已按发送者归入成员（编排层按 sender 匹配），sender 原值多为 open_id 等
        # 内部标识，展示统一用成员姓名（design.md §6.5：面向非技术读者，易读）
        lines.append(f"- · [{message.chat_name}] {member.name}：{content}"
                     f"（{_local(message.timestamp):%H:%M}）")
    lines.append("")
    return lines


def _render_attendance(member: MemberReport) -> list[str]:
    """工时统计板块（proposal §3.4 v1.1）：考勤不可用/缺记录时显示「考勤数据暂不可用」；
    未接入（缺凭据/占位值）的源板块不渲染（v1.3）；调用失败保持固定文案，原因只进日志。"""
    if _unconfigured(member, "lark_attendance"):
        return []
    attendance = member.attendance
    if attendance is None:
        return [f"- · {ATTENDANCE_UNAVAILABLE_TEXT}", ""]
    if attendance.check_in and attendance.check_out:
        return [f"- · 工时 {attendance.work_hours:g} 小时（{attendance.status}）", ""]
    if attendance.check_in is None and attendance.check_out is None \
            and attendance.status in ("缺勤", "休假"):
        return [f"- · {attendance.status}", ""]
    # 打卡不完整：一句话说明缺失项，状态有额外信息时补充
    missing = " / ".join(filter(None, (
        "签到缺失" if attendance.check_in is None else "",
        "签退缺失" if attendance.check_out is None else "",
    )))
    if attendance.status not in ("正常",) and attendance.status != missing:
        missing += f"（{attendance.status}）"
    return [f"- · {missing}", ""]


def _render_footer(members: list[MemberReport], generated_at: datetime) -> list[str]:
    """落款（v1.3）：🤖 生成器署名；数据来源（剔除未接入的源）｜ 生成时间。"""
    errors: dict[str, str] = {}
    for member in members:
        for source, reason in member.source_errors.items():
            errors.setdefault(source, reason)  # 编排层为所有成员填充同一份 source_errors
    sources = [label for key, label in SOURCE_ORDER
               if not _is_not_configured(errors.get(key, ""))]
    lines = ["---", "", "🤖 由「智能日报生成器」自动生成", ""]
    lines.append(f"数据来源：{'、'.join(sources)} ｜ 生成时间 {generated_at:%Y-%m-%d %H:%M}")
    lines.append("")
    return lines


def _unconfigured(member: MemberReport, source: str) -> bool:
    """该数据源是否未接入（缺凭据/未授权/占位值）（design.md §6.5）。"""
    reason = member.source_errors.get(source, "").strip()
    return bool(reason) and _is_not_configured(reason)


def _failure_text(source: str, member: MemberReport) -> str:
    """已接入但采集失败的标注（design.md §6.1：严禁静默跳过；§6.5：简短原因）。"""
    label = SOURCE_LABELS.get(source, source)
    reason = member.source_errors.get(source, "").strip()
    suffix = f"：{_sanitize_reason(reason)}" if reason else ""
    return f"**{FAILURE_TEXT}**（{label}采集异常{suffix}）"


def _is_not_configured(reason: str) -> bool:
    """是否属于「未接入」（缺凭据/未授权/占位值）类失败（design.md §6.5）。"""
    return any(keyword in reason for keyword in NOT_CONFIGURED_KEYWORDS)


def _sanitize_reason(reason: str) -> str:
    """「调用失败」原因的对外展示过滤（design.md §6.5）：只留一句话，剔除堆栈与环境变量名。"""
    reason = reason.splitlines()[0].strip()  # 堆栈等多行细节只出现在日志，绝不进入日报
    if not reason or reason.lower().startswith("traceback"):
        return ""  # 无法提炼出可对外的一句话时退回无原因展示
    reason = _ENV_VAR_RE.sub("", reason)  # 环境变量名（如 LARK_APP_SECRET）属内部细节
    return re.sub(r"\s{2,}", " ", reason).strip(" ：，,;；()（）")


def markdown_to_html(markdown: str) -> str:
    """将本模块生成的 Markdown 子集（标题/无序列表/分隔线/段落/加粗/链接）转换为 HTML。

    输出已做 HTML 转义，可直接嵌入页面模板（design.md §6.2：防注入）。
    """
    out: list[str] = []
    list_buffer: list[str] = []

    def flush_list() -> None:
        if list_buffer:
            out.append("<ul>")
            out.extend(f"  <li>{_inline(line)}</li>" for line in list_buffer)
            out.append("</ul>")
            list_buffer.clear()

    for line in markdown.splitlines():
        if not line.strip():
            flush_list()
            continue
        if line.strip() == "---":
            flush_list()
            out.append('<hr class="rule">')
            continue
        heading = re.match(r"^(#{1,6})\s+(.*)$", line)
        if heading:
            flush_list()
            level = len(heading.group(1))
            out.append(f"<h{level}>{_inline(heading.group(2))}</h{level}>")
            continue
        if line.startswith("- "):
            list_buffer.append(line[2:])
            continue
        flush_list()
        out.append(f"<p>{_inline(line)}</p>")
    flush_list()
    return "\n".join(out)


def _inline(text: str) -> str:
    """行内元素：先转义，再补加粗与链接。"""
    text = escape(text)
    text = re.sub(r"\*\*(.+?)\*\*", r"<strong>\1</strong>", text)
    text = re.sub(r"\[([^\]]+)\]\(([^)]+)\)", r'<a href="\2">\1</a>', text)
    return text
