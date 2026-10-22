"""日报生成：数据整理 + Markdown 生成 + Markdown→HTML 转换（design.md §4.2）。

- generate(members, date, team_name, window=None) -> DailyReport
  window=(since, until)：采集窗口（本地时区 datetime），用于头部「数据范围 HH:MM-HH:MM」
- 结构（v1.3，面向非技术读者，design.md §6.5）：
  头部（📊 标题·团队名 / 📅 日期含星期 / 数据范围）
  → ▎本周概况（v1.6）→ ▎今日概览（汇总数字）→ 各成员段落（▎成员 → ▪板块 → ·条目）→ 落款
- v1.4 展示规则（design.md §6.5 修订）：有数据 → 正常显示；已接入但当天无数据 →
  「今日无记录」（板块保留）；不可用（已接入但失败）→ 板块保留 + 一行可读说明
  （技术错误经 _friendly_reason 映射为固定文案）；未接入（缺凭据/占位值）→ 不渲染
- 失败信息只进日志：原始异常文本、环境变量名、堆栈不进日报正文
  （§6.1 的「不静默跳过」由日志与告警保证）
- 代码提交：每条只展示消息标题行 + 增删行数（不含仓库全名/文件数/trailer/时间）
- 工时统计：一行展示工时与出勤状态；可用但当天无记录显示「今日无记录」
- HTML 由 template.render_html 按结构化数据渲染（v1.5）：深色标题条 → 概览卡片组
  → 板块卡片（图标 + 状态徽章）→ 落款；Markdown 输出保持 v1.3/v1.4 结构不变
- v1.6：新增「本周概况」板块（位于今日概览之前，含较上周对比）；今日概览代码提交行
  追加「较昨日」对比；history 为 None 时显示「暂无历史数据」，不报错
- v1.8：提交按 Conventional Commits 归类（generator/classify.py）；概览追加「工作分类」
  行；成员提交板块按类别分组（组内时间升序）；新增「今日亮点」板块（今日概览之后、
  成员段落之前，最多 3 条、无提交不渲染）；generate(...) 签名不变
- v1.9：未归属伪成员（MemberReport.is_unassigned=True，name=UNASSIGNED_MEMBER_NAME）
  排在所有成员之后，只渲染有数据的板块（无「今日无记录」）；概览、工作分类、
  今日亮点按普通成员计入；_configured_sources 忽略伪成员；generate(...) 签名不变
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
from generator.classify import (CATEGORY_BY_KEY, CATEGORY_ORDER, classify_commits)
from generator.template import render_html

# 数据源展示顺序：概览/落款与正文板块顺序一致（代码提交 → 任务进展 → 工时统计 → 协作沟通）
SOURCE_ORDER = [
    ("github", "GitHub"),
    ("lark_task", "飞书任务"),
    ("lark_attendance", "飞书考勤"),
    ("lark_msg", "飞书消息"),
]
# 板块顺序与元信息：source key → (板块标题, HTML 板块图标)（design.md §6.5 v1.5）；
# Markdown 标题仍为「▪ 标题」不变，图标仅用于 HTML。
SECTIONS = (
    ("github", "代码提交", "💻"),
    ("lark_task", "任务进展", "📋"),
    ("lark_attendance", "工时统计", "⏱"),
    ("lark_msg", "协作沟通", "💬"),
)
SECTION_ICONS = {source: icon for source, _title, icon in SECTIONS}

NO_RECORDS_TEXT = "今日无记录"

# 报告名称：头部标题 / HTML 页面标题 / 邮件主题 / 飞书卡片标题共用（一处定义、多处使用）
REPORT_NAME = "智能日报"

# 未归属记录归集的伪成员名（v1.9，design.md §6.5）：编排层（main._aggregate）
# 把未匹配到 config.members 的提交/任务/消息归入该成员，排在所有成员之后。
UNASSIGNED_MEMBER_NAME = "其他贡献"

# 板块数据字段映射（v1.9）：伪成员只渲染有数据的板块，据此判定是否为空
SECTION_DATA_ATTRS = {
    "github": "commits",
    "lark_task": "tasks",
    "lark_attendance": "attendance",
    "lark_msg": "messages",
}

# 「未接入」（缺凭据/未授权/占位值）判定关键词（design.md §6.5）：与采集层的权威错误文案
# 及编排层跳过采集时填充的原因对齐；命中时板块整体不渲染，原因只进日志。
NOT_CONFIGURED_KEYWORDS = ("凭据未配置", "未配置", "未授权", "缺少飞书用户标识", "未开通")

# 技术错误 → 读者可读文案映射（design.md §6.5 v1.4 修订）：按表序首条命中生效；
# 输出为固定文案，不含原始异常文本、环境变量名、堆栈。
FRIENDLY_REASONS: tuple[tuple[tuple[str, ...], str], ...] = (
    (("platform policy", "平台"), "飞书接口受限（平台策略），暂不可用"),
    (("secret", "凭据未配置"), "飞书凭证未配置"),
    (("403", "permission", "权限"), "飞书权限不足，暂不可用"),
    (("timeout", "超时", "connect"), "飞书接口连接失败"),
)
FRIENDLY_DEFAULT_REASON = "飞书数据暂不可用"

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

    source_errors 为数据源失败/未配置标注（数据源 key → 原因），
    由编排层（main.py）在采集失败或未配置时填入；生成层据此分级展示
    （v1.4 修订）：未接入的源不渲染，不可用的源保留板块 + 一行可读说明
    （原因本身只进日志）。
    """

    name: str                        # 成员姓名
    github_username: str             # GitHub 用户名
    commits: list[CommitRecord] = field(default_factory=list)    # 代码提交记录
    tasks: list[TaskRecord] = field(default_factory=list)        # 任务变更记录
    messages: list[MessageRecord] = field(default_factory=list)  # 相关消息记录
    source_errors: dict[str, str] = field(default_factory=dict)  # 数据源失败/未配置标注
    attendance: AttendanceRecord | None = None  # 考勤记录（v1.1 新增；None 视为当天无记录）
    is_unassigned: bool = False      # 未归属记录归集的伪成员（v1.9；默认值保持向后兼容）


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
             window: tuple[datetime, datetime] | None = None,
             history: dict | None = None) -> DailyReport:
    """生成日报（design.md §4.2）。window 为采集窗口 (since, until)，用于头部数据范围。

    history 为历史统计（v1.6，由编排层从 daily_stats 读取组装），用于
    「本周概况」与「较昨日」对比；None（无历史数据）时显示「暂无历史数据」。
    Markdown 供飞书卡片使用（结构不变）；HTML 由模板按结构化数据渲染（v1.5）。
    """
    generated_at = datetime.now()
    markdown = _build_markdown(members, date, team_name, window, generated_at, history)
    html = render_html(_html_structure(members, date, team_name, window, generated_at,
                                       history))
    return DailyReport(
        date=date,
        team_name=team_name,
        members=members,
        generated_at=generated_at,
        markdown=markdown,
        html=html,
    )


def _html_structure(members: list[MemberReport], report_date: date, team_name: str,
                    window: tuple[datetime, datetime] | None,
                    generated_at: datetime, history: dict | None = None) -> dict:
    """组装 HTML 模板所需的结构化数据（design.md §6.5 v1.5/v1.6）；
    板块正文与落款在此先行转 HTML，模板不再解析 Markdown。"""
    stats = _overview_stats(members)
    return {
        "team_name": team_name,
        "report_name": REPORT_NAME,
        "title_date": report_date.isoformat(),
        "date_text": f"{report_date.isoformat()} {WEEKDAYS[report_date.weekday()]}",
        "range_text": f"{_local(window[0]):%H:%M}-{_local(window[1]):%H:%M}" if window else "",
        "week_html": _week_html(history),
        "overview_cards": _overview_cards(stats, history),
        "highlights_html": _highlights_html(members),
        "member_sections": _member_html_blocks(members),
        "footer_html": _footer_html(members, generated_at),
    }


def _build_markdown(members: list[MemberReport], report_date: date, team_name: str,
                    window: tuple[datetime, datetime] | None,
                    generated_at: datetime, history: dict | None = None) -> str:
    """v1.3 结构：头部 → 本周概况（v1.6）→ 今日概览 → 今日亮点（v1.8）
    → 成员段落 → 落款。"""
    lines = _render_header(team_name, report_date, window)
    lines += _render_week(history)
    lines += _render_overview(members, history)
    lines += _render_highlights(members)
    # v1.9：稳定排序，未归属伪成员排在所有真实成员之后（design.md §6.5）
    for member in _ordered_members(members):
        lines.extend(_render_member(member))
    lines += _render_footer(members, generated_at)
    return "\n".join(lines) + "\n"


def _ordered_members(members: list[MemberReport]) -> list[MemberReport]:
    """成员渲染顺序（v1.9）：稳定排序，is_unassigned 的伪成员排最后；
    其余成员保持原有相对顺序不变。"""
    return sorted(members, key=lambda m: m.is_unassigned)


def _render_header(team_name: str, report_date: date,
                   window: tuple[datetime, datetime] | None) -> list[str]:
    """头部（v1.3）：📊 标题·团队名；📅 日期含星期；数据范围 HH:MM-HH:MM。"""
    lines = [f"# 📊 {team_name} {REPORT_NAME}", ""]
    meta = f"📅 {report_date.isoformat()} {WEEKDAYS[report_date.weekday()]}"
    if window:
        since, until = window
        meta += f" · 数据范围 {_local(since):%H:%M}-{_local(until):%H:%M}"
    lines += [meta, ""]
    return lines


def _overview_stats(members: list[MemberReport]) -> dict:
    """全团队汇总数字与各源可用性（Markdown 概览与 HTML 概览卡片共用，v1.5）。

    active 按成员聚合（v1.8 修订）：任一成员已接入即视为团队激活——个别成员
    未配置不再掩盖其他成员的数据；members 为空时所有源视为未激活。
    """
    attended = [m.attendance for m in members
                if m.attendance is not None
                and m.attendance.check_in and m.attendance.check_out]
    return {
        "active": {source: source in _configured_sources(members)
                   for source, _label in SOURCE_ORDER},
        "commits": sum(len(m.commits) for m in members),
        "additions": sum(c.additions for m in members for c in m.commits),
        "deletions": sum(c.deletions for m in members for c in m.commits),
        "tasks": sum(len(m.tasks) for m in members),
        "messages": sum(len(m.messages) for m in members),
        "attended": len(attended),
        "hours": sum(a.work_hours for a in attended),
    }


def _render_week(history: dict | None) -> list[str]:
    """本周概况（v1.6，design.md §6.5）：位于今日概览之前，展示本周（周一~当日）
    提交/变更/活跃天数与较上周对比；history 为 None（无历史数据）时显示
    「暂无历史数据」，不报错。"""
    lines = ["## ▎本周概况", ""]
    if not history:
        lines += ["- · 暂无历史数据", ""]
        return lines
    week = history["week"]
    lines.append(f"- · 提交 {week['commits']} 次 ｜ 变更 +{week['additions']} / -{week['deletions']} 行")
    diff = week["commits"] - history["last_week"]["commits"]
    lines.append(f"- · 活跃 {week['active_days']} 天 ｜ 📈 较上周：提交 {_signed(diff)} 次")
    lines.append("")
    return lines


def _signed(value: int) -> str:
    """带符号数字：非负显示 +N，负数显示 -N（v1.6 对比文案「±N」）。"""
    return f"+{value}" if value >= 0 else f"-{-value}"


def _render_overview(members: list[MemberReport], history: dict | None = None) -> list[str]:
    """今日概览（v1.4 修订）：全团队汇总数字，位于成员段落之前；
    未接入的源整行不输出；已接入的源始终保留一行（不可用/无数据时计数为 0）。
    v1.6：代码提交行追加「较昨日」对比（无昨日统计时省略）。
    v1.8：有提交时在代码提交行之后追加「工作分类」行（固定顺序，只列非零）。"""
    stats = _overview_stats(members)
    active = stats["active"]
    lines = ["## ▎今日概览", ""]
    if active["github"]:
        line = (f"- · 代码提交 {stats['commits']} 次，共 +{stats['additions']}"
                f" / -{stats['deletions']} 行")
        if history and history.get("yesterday"):
            diff = stats["commits"] - history["yesterday"]["commits"]
            line += f" ｜ 📈 较昨日 {_signed(diff)} 次"
        lines.append(line)
        if stats["commits"]:
            lines.append(_work_categories_line(members))
    if active["lark_task"]:
        lines.append(f"- · 任务进展 {stats['tasks']} 项")
    if active["lark_attendance"]:
        lines.append(f"- · 工时统计 {stats['attended']} 人，共 {stats['hours']:g} 小时")
    if active["lark_msg"]:
        lines.append(f"- · 协作沟通 {stats['messages']} 条")
    lines.append("")
    return lines


def _category_counts(members: list[MemberReport]) -> dict[str, int]:
    """全团队各类别提交数（v1.8，与提交板块分组共用 classify_commits）。"""
    counts = {key: 0 for key in CATEGORY_ORDER}
    for member in members:
        for key, commits in classify_commits(member.commits).items():
            counts[key] += len(commits)
    return counts


def _work_categories_line(members: list[MemberReport]) -> str:
    """概览「工作分类」行（v1.8，design.md §6.5）：按固定类别顺序只列非零项。"""
    counts = _category_counts(members)
    parts = [f"{CATEGORY_BY_KEY[key].icon} {CATEGORY_BY_KEY[key].label} {counts[key]}"
             for key in CATEGORY_ORDER if counts[key]]
    return "- · 工作分类：" + " ｜ ".join(parts)


def _member_sections(member: MemberReport) -> list[tuple[str, str, list[str]]]:
    """成员各板块的渲染结果 (source, 标题, markdown 行)；空行的板块不渲染。
    Markdown 正文与 HTML 板块共用同一组装结果（v1.5）。

    v1.9：未归属伪成员只渲染有数据的板块（SECTION_DATA_ATTRS 判定），
    不输出「今日无记录」空板块（design.md §6.5）。
    """
    renderers = {
        "github": _render_commits,
        "lark_task": _render_tasks,
        "lark_attendance": _render_attendance,
        "lark_msg": _render_messages,
    }
    sections: list[tuple[str, str, list[str]]] = []
    for source, title, _icon in SECTIONS:
        if member.is_unassigned and not _section_has_data(member, source):
            continue  # v1.9：伪成员跳过无数据板块（考勤 None 视为无数据，不参与归集）
        section_lines = renderers[source](member)
        if section_lines:
            sections.append((source, title, section_lines))
    return sections


def _section_has_data(member: MemberReport, source: str) -> bool:
    """该源在 member 上是否有数据（v1.9，design.md §6.5）：
    列表源看是否非空，考勤看记录是否存在（None 视为无记录）。"""
    return bool(getattr(member, SECTION_DATA_ATTRS[source]))


def _render_member(member: MemberReport) -> list[str]:
    """成员段落（v1.3）：▎成员 → ▪板块 → ·条目；未接入的板块不渲染，
    不可用的板块保留并显示可读说明（v1.4 修订）。"""
    lines = [f"## ▎{member.name}", ""]
    for _source, title, section_lines in _member_sections(member):
        lines += [f"### ▪ {title}", "", *section_lines]
    lines += [""]
    return lines


def _render_highlights(members: list[MemberReport]) -> list[str]:
    """今日亮点（v1.8，design.md §6.5）：团队级板块，位于今日概览之后、成员段落之前。

    每类只取全团队最新一条（同类取最新），按优先级（feat > fix > perf > refactor
    > 其他）排序后最多取 3 条；当天无提交时返回空列表（板块不渲染）。
    """
    newest: dict[str, tuple[CommitRecord, MemberReport]] = {}
    for member in members:
        for key, commits in classify_commits(member.commits).items():
            if not commits:
                continue
            latest = max(commits, key=lambda c: c.timestamp)
            current = newest.get(key)
            if current is None or latest.timestamp > current[0].timestamp:
                newest[key] = (latest, member)
    if not newest:
        return []
    candidates = []
    for key in CATEGORY_ORDER:
        if key in newest:
            commit, member = newest[key]
            candidates.append((CATEGORY_BY_KEY[key], commit, member))
    candidates.sort(key=lambda item: item[0].priority)  # 显式按优先级排序（与固定顺序一致）
    lines = ["## ▎今日亮点", ""]
    for category, commit, member in candidates[:3]:
        title = commit.message.splitlines()[0].strip()  # 标题行；trailer 在正文中
        lines.append(f"- · {category.icon} {title}（{member.name}，"
                     f"+{commit.additions}/-{commit.deletions} 行）")
    lines.append("")
    return lines


def _render_commits(member: MemberReport) -> list[str]:
    if _unconfigured(member, "github"):
        return []  # 未接入：板块不渲染（design.md §6.5）
    if "github" in member.source_errors:
        return [f"- · {_friendly_reason(member.source_errors['github'])}", ""]  # v1.4：保留板块 + 可读说明
    commits = sorted(member.commits, key=lambda c: c.timestamp)
    if not commits:
        return [f"- · {NO_RECORDS_TEXT}", ""]
    additions = sum(c.additions for c in commits)
    deletions = sum(c.deletions for c in commits)
    lines = [f"- · 今日 {len(commits)} 次提交，共 +{additions} / -{deletions} 行"]
    # v1.8：按类别分组（design.md §6.5）：组顺序固定，组内按提交时间升序，空类别不输出
    groups = classify_commits(commits)
    for key in CATEGORY_ORDER:
        if not groups[key]:
            continue
        category = CATEGORY_BY_KEY[key]
        lines.append(f"- · {category.icon} {category.label}（{len(groups[key])}）")
        for commit in sorted(groups[key], key=lambda c: c.timestamp):
            title = commit.message.splitlines()[0].strip()  # 标题行；trailer（Co-Authored-By 等）在正文中
            lines.append(f"  - {title}（+{commit.additions}/-{commit.deletions} 行）")
    lines.append("")
    return lines


def _render_tasks(member: MemberReport) -> list[str]:
    if _unconfigured(member, "lark_task"):
        return []  # 未接入：板块不渲染（design.md §6.5）
    if "lark_task" in member.source_errors:
        return [f"- · {_friendly_reason(member.source_errors['lark_task'])}", ""]  # v1.4：保留板块 + 可读说明
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
        return []  # 未接入：板块不渲染（design.md §6.5）
    if "lark_msg" in member.source_errors:
        return [f"- · {_friendly_reason(member.source_errors['lark_msg'])}", ""]  # v1.4：保留板块 + 可读说明
    if not member.messages:
        return [f"- · {NO_RECORDS_TEXT}", ""]
    lines = []
    for message in sorted(member.messages, key=lambda m: m.timestamp):
        content = message.content.replace("\n", " ").strip()
        # 消息已按发送者归入成员（编排层按 sender 匹配），sender 原值多为 open_id 等
        # 内部标识，展示统一用成员姓名（design.md §6.5：面向非技术读者，易读）；
        # v1.9：未归属伪成员无法落到具体成员，发言人显示「未识别成员」
        speaker = "未识别成员" if member.is_unassigned else member.name
        lines.append(f"- · [{message.chat_name}] {speaker}：{content}"
                     f"（{_local(message.timestamp):%H:%M}）")
    lines.append("")
    return lines


def _render_attendance(member: MemberReport) -> list[str]:
    """工时统计板块（proposal §3.4 v1.1）：未接入不渲染；不可用保留板块 +
    一行可读说明（v1.4 修订）；可用但当天无记录显示「今日无记录」（板块保留）。"""
    if _unconfigured(member, "lark_attendance"):
        return []
    if "lark_attendance" in member.source_errors:
        return [f"- · {_friendly_reason(member.source_errors['lark_attendance'])}", ""]  # v1.4：保留板块 + 可读说明
    attendance = member.attendance
    if attendance is None:
        return [f"- · {NO_RECORDS_TEXT}", ""]
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
    """落款（v1.4 修订）：🤖 生成器署名；数据来源列出所有团队级已接入的源
    （任一成员已接入即列出，含不可用，v1.8 修订）｜ 生成时间。"""
    sources = [label for key, label in SOURCE_ORDER if key in _configured_sources(members)]
    lines = ["---", "", "🤖 由「智能日报生成器」自动生成", ""]
    lines.append(f"数据来源：{'、'.join(sources)} ｜ 生成时间 {generated_at:%Y-%m-%d %H:%M}")
    lines.append("")
    return lines


def _overview_cards(stats: dict, history: dict | None = None) -> list[dict]:
    """HTML 概览卡片（design.md §6.5 v1.5）：提交次数 / 变更行数 / 讨论条数；
    仅呈现已接入的源（未接入不出卡片，失败时计数为 0）。
    v1.6：有昨日统计时追加「较昨日提交」卡片（与 Markdown 概览一致）。"""
    cards: list[dict] = []
    if stats["active"]["github"]:
        cards.append({"label": "提交次数", "value": str(stats["commits"])})
        cards.append({"label": "变更行数", "value": f"+{stats['additions']}/-{stats['deletions']}"})
        if history and history.get("yesterday"):
            diff = stats["commits"] - history["yesterday"]["commits"]
            cards.append({"label": "较昨日提交", "value": f"{_signed(diff)} 次"})
    if stats["active"]["lark_msg"]:
        cards.append({"label": "讨论条数", "value": str(stats["messages"])})
    return cards


def _week_html(history: dict | None) -> str:
    """HTML 版本周概况（v1.6）：与 Markdown 板块同源（_render_week），
    复用成员卡片样式渲染（template.py 包裹），不引入新配色。"""
    lines = [line for line in _render_week(history) if line.strip()]
    return markdown_to_html("\n".join(lines))


def _highlights_html(members: list[MemberReport]) -> str:
    """HTML 版今日亮点（v1.8）：与 Markdown 板块同源（_render_highlights），
    复用成员卡片样式渲染（template.py 包裹），不引入新配色；无提交时为空串。"""
    lines = [line for line in _render_highlights(members) if line.strip()]
    return markdown_to_html("\n".join(lines))


def _member_html_blocks(members: list[MemberReport]) -> list[dict]:
    """HTML 版成员板块（v1.5）：与 Markdown 正文同源组装；
    每板块附带图标与状态（✅ 正常 / ⚠️ 不可用），正文转 HTML 后交模板渲染。
    v1.9：遍历前稳定排序，未归属伪成员排在所有成员之后（与 Markdown 一致）。"""
    blocks: list[dict] = []
    for member in _ordered_members(members):
        sections = []
        for source, title, section_lines in _member_sections(member):
            sections.append({
                "icon": SECTION_ICONS[source],
                "title": title,
                "status": "warn" if source in member.source_errors else "ok",
                "body_html": markdown_to_html("\n".join(section_lines)),
            })
        blocks.append({"name": member.name, "sections": sections})
    return blocks


def _footer_html(members: list[MemberReport], generated_at: datetime) -> str:
    """落款 HTML：与 Markdown 落款同源（_render_footer），v1.5 模板直接渲染。"""
    lines = [line for line in _render_footer(members, generated_at)
             if line.strip() and line.strip() != "---"]
    return markdown_to_html("\n".join(lines))


def _unconfigured(member: MemberReport, source: str) -> bool:
    """该数据源是否未接入（缺凭据/未授权/占位值）（design.md §6.5）：板块不渲染。"""
    reason = member.source_errors.get(source, "").strip()
    return bool(reason) and _is_not_configured(reason)


def _is_not_configured(reason: str) -> bool:
    """是否属于「未接入」（缺凭据/未授权/占位值）类原因（design.md §6.5）。"""
    return any(keyword in reason for keyword in NOT_CONFIGURED_KEYWORDS)


def _configured_sources(members: list[MemberReport]) -> set[str]:
    """团队级已接入的数据源（v1.8 修订）：任一成员已接入即视为团队激活。

    仅影响概览与落款的团队级展示；成员板块仍按成员单独判定
    （未接入的成员板块不渲染，规则不变）。
    v1.9：忽略未归属伪成员——其 source_errors 为空，不参与接入判定，
    避免仅伪成员有数据时误激活数据源（design.md §6.5）。
    """
    return {source for source, _label in SOURCE_ORDER
            for member in members
            if not member.is_unassigned and not _unconfigured(member, source)}


def _friendly_reason(err: str) -> str:
    """把技术错误映射为读者可读的一句话（design.md §6.5 v1.4 修订映射表）。

    按表序首条命中的关键词输出固定文案；原始异常文本、环境变量名、
    堆栈一律不进入日报正文。
    """
    lowered = err.lower()
    for keywords, text in FRIENDLY_REASONS:
        if any(keyword in lowered for keyword in keywords):
            return text
    return FRIENDLY_DEFAULT_REASON


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
        # 兼容带缩进的子条目（v1.8 提交分组 `  - …`）：去掉行首空白与 `- ` 后作为条目
        item = re.match(r"^\s*-\s+(.*)$", line)
        if item:
            list_buffer.append(item.group(1))
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
