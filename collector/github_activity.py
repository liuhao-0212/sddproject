"""GitHub 仓库动态采集模块：PR / Issue / Release（v1.12，design.md §6.8）。

接口契约: collect(repos, since, until) -> list[RepoEvent]

数据源（GitHub REST，复用 collector.github 的客户端与重试）:
- PR:      GET /repos/{repo}/pulls?state=all&sort=updated&direction=desc&per_page=100
- Issue:   GET /repos/{repo}/issues?state=all&since={since}&per_page=100
           （排除含 pull_request 的条目——GitHub 的 issues 接口会混入 PR）
- Release: GET /repos/{repo}/releases?per_page=30

归类口径:
- 合并 PR → pr_merged（merged_at）
- 关闭 PR → pr_closed（closed_at，merged_at 为空）
- 新建 PR → pr_opened（created_at）
- 关闭 Issue → issue_closed（closed_at）
- 新建 Issue → issue_opened（created_at）
- Release → release（published_at）

事件过滤到 [since, until]；按 (kind, repo, number) 去重；按时间倒序返回。
采集失败抛 CollectorError，由编排层兜底记录日志并隐藏板块（design.md §6.1）。
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any

import httpx

from collector import github
from shared.errors import CollectorError
from shared.logger import get_logger

logger = get_logger("collector.github_activity")

BASE_URL = github.BASE_URL
PER_PAGE = github.PER_PAGE          # PR / Issue 每页 100 条
RELEASE_PER_PAGE = 30               # Release 每页 30 条（design.md §6.8）

# RepoEvent.kind 取值（design.md §6.8）
PR_MERGED = "pr_merged"
PR_OPENED = "pr_opened"
PR_CLOSED = "pr_closed"
ISSUE_CLOSED = "issue_closed"
ISSUE_OPENED = "issue_opened"
RELEASE = "release"


@dataclass
class RepoEvent:
    """仓库动态事件（v1.12，design.md §6.8）。number 对 Release 为 release id。"""

    kind: str            # pr_merged / pr_opened / pr_closed / issue_closed / issue_opened / release
    number: int | None   # PR / Issue 编号；Release 为 release id（用于去重）
    title: str           # PR / Issue 标题；Release 名称（回退 tag_name）
    timestamp: datetime  # 归类口径对应的时间（携带时区）
    repo: str            # 仓库名称
    author: str = ""     # 操作者 GitHub 用户名
    url: str = ""        # 事件页面链接


def collect(repos: list[str], since: datetime, until: datetime) -> list[RepoEvent]:
    """获取所有指定仓库在 [since, until] 内的 PR / Issue / Release 动态。

    任一仓库任一接口失败即抛 CollectorError（由 main 兜底记录日志并隐藏板块）；
    调用方负责把异常转成「今日无仓库动态」之外的展示决策。
    """
    events: list[RepoEvent] = []
    client = github.get_client()
    try:
        for repo in repos:
            events.extend(_fetch_pulls(client, repo, since, until))
            events.extend(_fetch_issues(client, repo, since, until))
            events.extend(_fetch_releases(client, repo, since, until))
    finally:
        client.close()

    deduped: dict[tuple[str, str, int | None], RepoEvent] = {}
    for event in events:
        deduped.setdefault((event.kind, event.repo, event.number), event)
    result = sorted(deduped.values(), key=lambda event: event.timestamp, reverse=True)
    logger.info("github 仓库动态采集完成", extra={"repos": len(repos), "count": len(result)})
    return result


def _fetch_pulls(client: httpx.Client, repo: str, since: datetime,
                 until: datetime) -> list[RepoEvent]:
    """拉取仓库 PR 并按口径归类（合并 / 新建 / 关闭）。"""
    data = _get_json(client, f"{BASE_URL}/repos/{repo}/pulls", {
        "state": "all",
        "sort": "updated",
        "direction": "desc",
        "per_page": PER_PAGE,
    })
    events: list[RepoEvent] = []
    for raw in data:
        event = _parse_pull(raw, repo)
        if event is not None and _in_window(event.timestamp, since, until):
            events.append(event)
    return events


def _parse_pull(raw: dict, repo: str) -> RepoEvent | None:
    """单条 PR → RepoEvent；缺少编号或时间不可解析时跳过并告警。"""
    if raw.get("merged_at"):
        kind, stamp = PR_MERGED, raw.get("merged_at")
    elif raw.get("closed_at"):
        kind, stamp = PR_CLOSED, raw.get("closed_at")
    else:
        kind, stamp = PR_OPENED, raw.get("created_at")
    return _build_event(kind, raw.get("number"), raw.get("title"), stamp,
                        repo, _author(raw), raw.get("html_url"), "PR")


def _fetch_issues(client: httpx.Client, repo: str, since: datetime,
                  until: datetime) -> list[RepoEvent]:
    """拉取仓库 Issue（排除 PR）并按口径归类（关闭 / 新建）。"""
    data = _get_json(client, f"{BASE_URL}/repos/{repo}/issues", {
        "state": "all",
        "since": since.isoformat(),
        "per_page": PER_PAGE,
    })
    events: list[RepoEvent] = []
    for raw in data:
        event = _parse_issue(raw, repo)
        if event is not None and _in_window(event.timestamp, since, until):
            events.append(event)
    return events


def _parse_issue(raw: dict, repo: str) -> RepoEvent | None:
    """单条 Issue → RepoEvent；issues 接口返回的 PR 条目直接排除。"""
    if "pull_request" in raw:
        return None  # GitHub issues 接口会混入 PR，design.md §6.8 要求排除
    if raw.get("state") == "closed":
        kind, stamp = ISSUE_CLOSED, raw.get("closed_at")
    else:
        kind, stamp = ISSUE_OPENED, raw.get("created_at")
    return _build_event(kind, raw.get("number"), raw.get("title"), stamp,
                        repo, _author(raw), raw.get("html_url"), "Issue")


def _fetch_releases(client: httpx.Client, repo: str, since: datetime,
                    until: datetime) -> list[RepoEvent]:
    """拉取仓库 Release，按 published_at 过滤到窗口内。"""
    data = _get_json(client, f"{BASE_URL}/repos/{repo}/releases", {
        "per_page": RELEASE_PER_PAGE,
    })
    events: list[RepoEvent] = []
    for raw in data:
        event = _parse_release(raw, repo)
        if event is not None and _in_window(event.timestamp, since, until):
            events.append(event)
    return events


def _parse_release(raw: dict, repo: str) -> RepoEvent | None:
    """单条 Release → RepoEvent；草稿（无 published_at）跳过。"""
    return _build_event(RELEASE, raw.get("id"),
                        raw.get("name") or raw.get("tag_name"),
                        raw.get("published_at"), repo, _author(raw),
                        raw.get("html_url"), "Release")


def _build_event(kind: str, number: Any, title: Any, stamp: Any, repo: str,
                 author: str, url: Any, label: str) -> RepoEvent | None:
    """组装 RepoEvent；编号缺失/非整数或时间不可解析时记告警并跳过。"""
    timestamp = _parse_time(stamp)
    if not isinstance(number, int) or timestamp is None:
        logger.warning("github %s 数据不完整，跳过", label,
                       extra={"repo": repo, "number": number})
        return None
    return RepoEvent(kind=kind, number=number, title=str(title or ""),
                     timestamp=timestamp, repo=repo, author=author,
                     url=str(url or ""))


def _author(raw: dict) -> str:
    """事件操作者：PR / Issue 为 user.login，Release 为 author.login。"""
    user = raw.get("user") or raw.get("author")
    if isinstance(user, dict):
        return str(user.get("login") or "")
    return ""


def _get_json(client: httpx.Client, url: str, params: dict[str, Any]) -> list[dict]:
    """带重试的 GET 并解析 JSON 列表；格式异常统一抛 CollectorError。"""
    resp = github.request_with_retry(client, "GET", url, params=params)
    try:
        data = resp.json()
    except ValueError as exc:
        raise CollectorError(f"github 仓库动态接口返回非 JSON: {url}") from exc
    if not isinstance(data, list):
        raise CollectorError(f"github 仓库动态接口返回格式异常: {url}")
    return data


def _parse_time(value: Any) -> datetime | None:
    """解析 GitHub 时间字符串；无时区按 UTC 处理（保证可与采集窗口比较）。"""
    if not isinstance(value, str) or not value:
        return None
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError:
        return None
    return parsed if parsed.tzinfo is not None else parsed.replace(tzinfo=timezone.utc)


def _in_window(timestamp: datetime, since: datetime, until: datetime) -> bool:
    """事件时间是否落在采集窗口 [since, until] 内。"""
    return since <= timestamp <= until
