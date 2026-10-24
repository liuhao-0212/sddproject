"""周报生成（v1.17，design.md §6.13）：汇总本周（周一~当日）的提交、分类、
每日趋势、仓库动态与成员排名。

纯生成层：输入已聚合的成员（含周窗口内的提交）与仓库事件，输出 Markdown / HTML；
不推送、不落库（由 main.py 的 --weekly 负责写文件与打印）。
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime, timedelta
from typing import Any

from collector.github_activity import RepoEvent
from generator.classify import CATEGORY_BY_KEY, CATEGORY_ORDER, classify_commits
from generator.formatter import MemberReport, SPARK_CHARS, markdown_to_html

WEEKDAYS = ("周一", "周二", "周三", "周四", "周五", "周六", "周日")
_LOCAL_TZ = datetime.now().astimezone().tzinfo

# 仓库动态展示顺序（与日报一致）
_EVENT_ORDER = (
    ("pr_merged", "合并 PR"),
    ("pr_opened", "新建 PR"),
    ("pr_closed", "关闭 PR"),
    ("issue_closed", "关闭 Issue"),
    ("issue_opened", "新建 Issue"),
    ("release", "发布版本"),
)
_EVENT_LIMIT = 5


@dataclass
class WeeklyReport:
    """周报生成结果（v1.17）。"""

    start: date
    end: date
    team_name: str
    markdown: str
    html: str


def generate_weekly(members: list[MemberReport], start: date, end: date,
                    last_week_rows: list[dict[str, Any]] | None,
                    repo_events: list[RepoEvent] | None,
                    team_name: str,
                    generated_at: datetime | None = None) -> WeeklyReport:
    """生成本周周报（v1.17）：Markdown + HTML，纯函数、无外部调用。

    每日趋势与活跃天数直接由本周采集到的提交按本地日期统计，
    不依赖 daily_stats（避免历史统计缺失导致趋势失真）。
    """
    generated_at = generated_at or datetime.now()
    markdown = _build_markdown(
        members=members, start=start, end=end,
        last_week_rows=last_week_rows or [],
        repo_events=repo_events or [],
        team_name=team_name, generated_at=generated_at)
    html = _render_html(markdown, team_name, start, end)
    return WeeklyReport(start=start, end=end, team_name=team_name,
                        markdown=markdown, html=html)


def _day_of(commit) -> date:
    return commit.timestamp.astimezone(_LOCAL_TZ).date()


def _totals(members: list[MemberReport]) -> dict[str, int]:
    commits = [commit for member in members for commit in member.commits]
    days = {_day_of(commit) for commit in commits}
    return {
        "commits": len(commits),
        "additions": sum(commit.additions for commit in commits),
        "deletions": sum(commit.deletions for commit in commits),
        "active_days": len(days),
    }


def _category_counts(members: list[MemberReport]) -> dict[str, int]:
    counts = {key: 0 for key in CATEGORY_ORDER}
    for member in members:
        for key, commits in classify_commits(member.commits).items():
            counts[key] += len(commits)
    return counts


def _trend_values(members: list[MemberReport], start: date,
                  end: date) -> list[tuple[date, int]]:
    counts: dict[date, int] = {}
    for member in members:
        for commit in member.commits:
            day = _day_of(commit)
            if start <= day <= end:
                counts[day] = counts.get(day, 0) + 1
    values: list[tuple[date, int]] = []
    day = start
    while day <= end:
        values.append((day, counts.get(day, 0)))
        day += timedelta(days=1)
    return values


def _sparkline(values: list[int]) -> str:
    if not values:
        return ""
    peak = max(values)
    if peak <= 0:
        return SPARK_CHARS[0] * len(values)
    return "".join(
        SPARK_CHARS[min(len(SPARK_CHARS) - 1,
                        max(0, int(value / peak * (len(SPARK_CHARS) - 1))))]
        for value in values
    )


def _signed(value: int) -> str:
    return f"+{value}" if value >= 0 else f"-{-value}"


def _render_events(events: list[RepoEvent]) -> list[str]:
    if not events:
        return ["- · 本周无仓库动态"]
    lines: list[str] = []
    for kind, label in _EVENT_ORDER:
        group = [event for event in events if event.kind == kind]
        if not group:
            continue
        lines.append(f"- · {label}（{len(group)}）")
        for event in group[:_EVENT_LIMIT]:
            title = f"#{event.number} {event.title}" if event.number else event.title
            lines.append(f"  - {title}（{event.timestamp:%m-%d}）")
        if len(group) > _EVENT_LIMIT:
            lines.append(f"  - 另有 {len(group) - _EVENT_LIMIT} 条")
    return lines


def _render_ranking(members: list[MemberReport]) -> list[str]:
    rows = [member for member in members if member.commits]
    rows.sort(key=lambda m: (len(m.commits), sum(c.additions for c in m.commits)),
              reverse=True)
    if not rows:
        return ["- · 本周无提交"]
    lines: list[str] = []
    for index, member in enumerate(rows, start=1):
        additions = sum(c.additions for c in member.commits)
        deletions = sum(c.deletions for c in member.commits)
        lines.append(f"- · {index}. {member.name} — {len(member.commits)} 次提交，"
                     f"+{additions} / -{deletions} 行")
    return lines


def _build_markdown(members: list[MemberReport], start: date, end: date,
                    last_week_rows: list[dict[str, Any]], repo_events: list[RepoEvent],
                    team_name: str, generated_at: datetime) -> str:
    totals = _totals(members)
    last_commits = sum(int(row["commits_count"]) for row in last_week_rows)
    diff = totals["commits"] - last_commits
    counts = _category_counts(members)
    categories = " ｜ ".join(
        f"{CATEGORY_BY_KEY[key].icon} {CATEGORY_BY_KEY[key].label} {counts[key]}"
        for key in CATEGORY_ORDER if counts[key]
    )
    trend = _trend_values(members, start, end)
    trend_values = [value for _day, value in trend]
    peak = max(trend_values) if trend_values else 0
    days = (end - start).days + 1

    lines = [
        f"# 📊 {team_name} 智能周报",
        "",
        f"🗓️ {start.isoformat()} ~ {end.isoformat()} · 共 {days} 天",
        "",
        "## ▎本周概览",
        "",
        f"- · 提交 {totals['commits']} 次 ｜ 变更 +{totals['additions']} / -{totals['deletions']} 行",
        f"- · 活跃 {totals['active_days']} 天 ｜ 📈 较上周：提交 {_signed(diff)} 次",
        "",
        "## ▎工作分类",
        "",
        f"- · {categories}" if categories else "- · 本周无提交",
        "",
        "## ▎每日趋势",
        "",
        f"- · {_sparkline(trend_values)}（峰值 {peak} 次）",
        "- · " + " ｜ ".join(
            f"{WEEKDAYS[day.weekday()]} {value}" for day, value in trend),
        "",
        "## ▎仓库动态",
        "",
        *_render_events(repo_events),
        "",
        "## ▎成员排名",
        "",
        *_render_ranking(members),
        "",
        "---",
        "",
        "🤖 由「智能日报生成器」自动生成 · 周报",
        f"数据来源：GitHub ｜ 生成时间 {generated_at:%Y-%m-%d %H:%M}",
    ]
    return "\n".join(lines) + "\n"


def _render_html(markdown: str, team_name: str, start: date, end: date) -> str:
    body = markdown_to_html(markdown)
    return f"""<!DOCTYPE html>
<html lang="zh-CN">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>{team_name} 智能周报（{start.isoformat()} ~ {end.isoformat()}）</title>
<style>
  body {{ font-family: -apple-system, "Segoe UI", "PingFang SC", "Microsoft YaHei",
         sans-serif; max-width: 860px; margin: 0 auto; padding: 1rem 1rem 2rem;
         background: #f6f8fa; color: #1f2937; line-height: 1.6; }}
  h1 {{ background: #2563eb; color: #fff; border-radius: 10px; padding: 1rem 1.2rem; font-size: 1.4rem; }}
  h2 {{ color: #2563eb; border-left: 4px solid #2563eb; padding-left: .5rem; margin-top: 1.4rem; }}
  ul {{ padding-left: 1.2rem; }}
  li {{ margin: .25rem 0; }}
  hr {{ border: 0; border-top: 1px solid #e5e7eb; margin: 1.5rem 0 1rem; }}
  p {{ color: #6b7280; font-size: 13px; }}
</style>
</head>
<body>
{body}
</body>
</html>
"""
