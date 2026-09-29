"""日报生成：数据整理 + Markdown 生成 + Markdown→HTML 转换（design.md §4.2）。

- generate(members, date, team_name) -> DailyReport
- 三段式编排（proposal §2.1）：代码提交 → 任务进展 → 协作沟通，每成员独立段落
- 空数据显示"今日无记录"；数据源失败标注"数据获取失败"（design.md §6.1）
- 日报仅含统计信息，不含代码差异内容（design.md §6.2）
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import date, datetime
from html import escape

from collector.github import CommitRecord
from collector.lark_msg import MessageRecord
from collector.lark_task import TaskRecord
from generator.template import render_html

# 数据源 key → 展示名（与编排层填充 MemberReport.source_errors 的 key 约定一致）
SOURCE_LABELS = {"github": "GitHub", "lark_task": "飞书任务", "lark_msg": "飞书消息"}

NO_RECORDS_TEXT = "今日无记录"
FAILURE_TEXT = "数据获取失败"


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
    for member in members:
        lines.extend(_render_member(member))
    return "\n".join(lines) + "\n"


def _render_member(member: MemberReport) -> list[str]:
    lines = [f"## {member.name}", ""]
    if member.github_username:
        lines += [f"- GitHub 用户名: {member.github_username}", ""]
    lines += ["### 代码提交", "", *_render_commits(member)]
    lines += ["### 任务进展", "", *_render_tasks(member)]
    lines += ["### 协作沟通", "", *_render_messages(member)]
    lines += [""]
    return lines


def _render_commits(member: MemberReport) -> list[str]:
    if "github" in member.source_errors:
        return [_failure_line("github", member), ""]
    if not member.commits:
        return [f"- {NO_RECORDS_TEXT}", ""]
    lines = []
    for commit in sorted(member.commits, key=lambda c: c.timestamp):
        message = commit.message.replace("\n", " ").strip()
        lines.append(f"- [{commit.repo}] {message}"
                     f"（+{commit.additions}/-{commit.deletions}，"
                     f"{commit.files_changed} 个文件）— {commit.timestamp:%H:%M}")
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
                     f"（{task.updated_at:%H:%M}）")
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
                     f"（{message.timestamp:%H:%M}）")
    lines.append("")
    return lines


def _failure_line(source: str, member: MemberReport) -> str:
    """数据源失败标注行（design.md §6.1：严禁静默跳过）。"""
    label = SOURCE_LABELS.get(source, source)
    reason = member.source_errors.get(source, "").strip()
    suffix = f"：{reason}" if reason else ""
    return f"- **{FAILURE_TEXT}**（{label}采集异常{suffix}）"


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
