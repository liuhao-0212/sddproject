"""日报生成：数据整理 + Markdown 生成 + Markdown→HTML 转换（design.md §4.2）。

- generate(members, date, team_name) -> DailyReport
- 顶部"数据源状态"区：逐源显示 可用/调用失败/未接入 + 采集条数（proposal §3.5，v1.2）
- 四段式编排（proposal §2.1，v1.1 增补工时统计）：代码提交 → 任务进展 → 工时统计 → 协作沟通，
  每成员独立段落
- 空数据显示"今日无记录"；失败板块分级展示（design.md §6.5）：
  「未接入」（缺凭据/未授权）仅一句话"未接入，已跳过"，不显示原因；
  「调用失败」显示过滤后的简短原因（不含堆栈与环境变量名）；异常细节只进日志
- 代码提交板块简化（v1.2）：板块开头汇总「今日 N 次提交，共 +X / -Y 行」，
  每条只展示消息标题行 + 增删行数（不含仓库全名、文件数、Co-Authored-By）
- 工时统计板块简化（v1.2）：一行展示工时与出勤状态，不展开签到/签退时刻
- 考勤不可用标注"考勤数据暂不可用"（proposal §3.4）
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

# 数据源展示顺序：状态区与正文板块顺序一致（代码提交 → 任务进展 → 工时统计 → 协作沟通）
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
NOT_CONFIGURED_TEXT = "未接入，已跳过"

# 数据源状态区图标（design.md §6.5；template.py 按同图标渲染 HTML 徽章）
ICON_OK = "✅"
ICON_FAILED = "⚠️"
ICON_NOT_CONFIGURED = "－"

# 「未接入」（缺凭据/未授权）判定关键词（design.md §6.5）：与采集层的权威错误文案对齐；
# 命中时板块仅显示"未接入，已跳过"，原始原因（含环境变量名）只进日志不进日报。
NOT_CONFIGURED_KEYWORDS = ("凭据未配置", "未配置", "未授权", "缺少飞书用户标识")

# 环境变量名模式（大写字母 + 下划线），用于过滤「调用失败」原因中的内部细节
_ENV_VAR_RE = re.compile(r"\b[A-Z][A-Z0-9]*_[A-Z0-9_]+\b")

# 系统本地时区（design.md §6.4：日报中所有时间统一转换为本地时区后显示）
LOCAL_TZ = datetime.now().astimezone().tzinfo


def _local(dt: datetime) -> datetime:
    """转换为本地时区用于展示（design.md §6.4；naive 输入视为已是本地时间）。"""
    return dt.astimezone(LOCAL_TZ)


@dataclass
class MemberReport:
    """成员报告（design.md §3.2）。

    source_errors 为 §6.1 失败标注的扩展：数据源 key → 失败原因，
    由编排层（main.py）在采集失败时填入，生成层据此标注"数据获取失败"。
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


def generate(members: list[MemberReport], date: date, team_name: str) -> DailyReport:
    """按"代码提交 → 任务进展 → 协作沟通"三段式生成日报（design.md §4.2）。"""
    markdown = _build_markdown(members, date, team_name)
    html = render_html(markdown, date, team_name)
    return DailyReport(
        date=date,
        team_name=team_name,
        members=members,
        generated_at=datetime.now(),
        markdown=markdown,
        html=html,
    )


def _build_markdown(members: list[MemberReport], report_date: date, team_name: str) -> str:
    lines = [f"# {team_name} 日报（{report_date.isoformat()}）", ""]
    lines += ["## 数据源状态", "", *_render_source_status(members)]  # v1.2：顶部状态区
    for member in members:
        lines.extend(_render_member(member))
    return "\n".join(lines) + "\n"


def _render_source_status(members: list[MemberReport]) -> list[str]:
    """数据源状态区（proposal §3.5 / design.md §6.5）：逐源显示 可用/调用失败/未接入 + 采集条数。

    条数按成员报告聚合（与正文一致）；失败状态取自编排层为所有成员填充的同一份
    source_errors（_aggregate 为每个成员复制同一 dict，故合并取并集即可）。
    """
    errors: dict[str, str] = {}
    for member in members:
        for source, reason in member.source_errors.items():
            errors.setdefault(source, reason)
    counts = {
        "github": sum(len(m.commits) for m in members),
        "lark_task": sum(len(m.tasks) for m in members),
        "lark_msg": sum(len(m.messages) for m in members),
        "lark_attendance": sum(1 for m in members if m.attendance is not None),
    }
    lines = []
    for source, label in SOURCE_ORDER:
        error = errors.get(source, "").strip()
        if not error:
            lines.append(f"- {ICON_OK} {label}：可用（{counts[source]} 条）")
        elif _is_not_configured(error):
            lines.append(f"- {ICON_NOT_CONFIGURED} {label}：未接入")
        else:
            lines.append(f"- {ICON_FAILED} {label}：调用失败")
    lines.append("")
    return lines


def _render_member(member: MemberReport) -> list[str]:
    lines = [f"## {member.name}", ""]
    if member.github_username:
        lines += [f"- GitHub 用户名: {member.github_username}", ""]
    lines += ["### 代码提交", "", *_render_commits(member)]
    lines += ["### 任务进展", "", *_render_tasks(member)]
    lines += ["### 工时统计", "", *_render_attendance(member)]  # v1.1：任务进展之后、协作沟通之前
    lines += ["### 协作沟通", "", *_render_messages(member)]
    lines += [""]
    return lines


def _render_commits(member: MemberReport) -> list[str]:
    if "github" in member.source_errors:
        return [_failure_line("github", member), ""]
    commits = sorted(member.commits, key=lambda c: c.timestamp)
    if not commits:
        return [f"- {NO_RECORDS_TEXT}", ""]
    # v1.2 简化：板块开头一行汇总；每条只展示消息第一行（标题）+ 增删行数，
    # 不再展示仓库全名、文件数、Co-Authored-By 等技术细节
    additions = sum(c.additions for c in commits)
    deletions = sum(c.deletions for c in commits)
    lines = [f"- 今日 {len(commits)} 次提交，共 +{additions} / -{deletions} 行"]
    for commit in commits:
        title = commit.message.splitlines()[0].strip()  # 标题行；trailer（Co-Authored-By 等）在正文中
        lines.append(f"- {title}（+{commit.additions}/-{commit.deletions} 行）"
                     f"— {_local(commit.timestamp):%H:%M}")
    lines.append("")
    return lines


def _render_tasks(member: MemberReport) -> list[str]:
    if "lark_task" in member.source_errors:
        return [_failure_line("lark_task", member), ""]
    if not member.tasks:
        return [f"- {NO_RECORDS_TEXT}", ""]
    lines = []
    for task in sorted(member.tasks, key=lambda t: t.updated_at):
        lines.append(f"- {task.title}：{task.status_from} → {task.status_to}"
                     f"（{_local(task.updated_at):%H:%M}）")
    lines.append("")
    return lines


def _render_messages(member: MemberReport) -> list[str]:
    if "lark_msg" in member.source_errors:
        return [_failure_line("lark_msg", member), ""]
    if not member.messages:
        return [f"- {NO_RECORDS_TEXT}", ""]
    lines = []
    for message in sorted(member.messages, key=lambda m: m.timestamp):
        content = message.content.replace("\n", " ").strip()
        lines.append(f"- [{message.chat_name}] {message.sender}：{content}"
                     f"（{_local(message.timestamp):%H:%M}）")
    lines.append("")
    return lines


def _render_attendance(member: MemberReport) -> list[str]:
    """工时统计板块（proposal §3.4 v1.1）：考勤不可用/缺记录时显示"考勤数据暂不可用"；
    未接入（缺凭据）时显示一句话"未接入，已跳过"（design.md §6.5）。

    v1.2 简化：只展示「工时 + 出勤状态」一行，不再展开签到/签退时刻等细节。
    """
    error = member.source_errors.get("lark_attendance", "").strip()
    if error and _is_not_configured(error):
        return [f"- {NOT_CONFIGURED_TEXT}", ""]
    attendance = member.attendance
    if attendance is None:
        return [f"- {ATTENDANCE_UNAVAILABLE_TEXT}", ""]
    if attendance.check_in and attendance.check_out:
        return [f"- 工时 {attendance.work_hours:g} 小时（{attendance.status}）", ""]
    if attendance.check_in is None and attendance.check_out is None \
            and attendance.status in ("缺勤", "休假"):
        return [f"- {attendance.status}", ""]
    # 打卡不完整：一句话说明缺失项，状态有额外信息时补充
    missing = " / ".join(filter(None, (
        "签到缺失" if attendance.check_in is None else "",
        "签退缺失" if attendance.check_out is None else "",
    )))
    if attendance.status not in ("正常",) and attendance.status != missing:
        missing += f"（{attendance.status}）"
    return [f"- {missing}", ""]


def _failure_line(source: str, member: MemberReport) -> str:
    """数据源失败标注行（design.md §6.5 分级展示；§6.1 严禁静默跳过）。

    - 「未接入」（缺凭据/未授权）：仅一句话，不显示原因（原因只进日志）
    - 「调用失败」（已接入但出错）：显示过滤后的简短原因（不含堆栈/环境变量名）
    """
    label = SOURCE_LABELS.get(source, source)
    reason = member.source_errors.get(source, "").strip()
    if reason and _is_not_configured(reason):
        return f"- {NOT_CONFIGURED_TEXT}"
    suffix = f"：{_sanitize_reason(reason)}" if reason else ""
    return f"- **{FAILURE_TEXT}**（{label}采集异常{suffix}）"


def _is_not_configured(reason: str) -> bool:
    """是否属于「未接入」（缺凭据/未授权）类失败（design.md §6.5）。"""
    return any(keyword in reason for keyword in NOT_CONFIGURED_KEYWORDS)


def _sanitize_reason(reason: str) -> str:
    """「调用失败」原因的对外展示过滤（design.md §6.5）：只留一句话，剔除堆栈与环境变量名。"""
    reason = reason.splitlines()[0].strip()  # 堆栈等多行细节只出现在日志，绝不进入日报
    if not reason or reason.lower().startswith("traceback"):
        return ""  # 无法提炼出可对外的一句话时退回无原因展示
    reason = _ENV_VAR_RE.sub("", reason)  # 环境变量名（如 LARK_APP_SECRET）属内部细节
    return re.sub(r"\s{2,}", " ", reason).strip(" ：，,;；()（）")


def markdown_to_html(markdown: str) -> str:
    """将本模块生成的 Markdown 子集（标题/无序列表/段落/加粗/链接）转换为 HTML。

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
