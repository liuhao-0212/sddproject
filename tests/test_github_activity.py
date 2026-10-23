"""collector.github_activity 单元测试（httpx.MockTransport，零真实网络）。"""

from datetime import datetime, timezone

import httpx
import pytest

import collector.github as github
import collector.github_activity as activity
from shared.errors import CollectorError

TZ = timezone.utc
SINCE = datetime(2026, 9, 29, 0, 0, tzinfo=TZ)
UNTIL = datetime(2026, 9, 29, 18, 0, tzinfo=TZ)
REPO = "org/repo"


def install(monkeypatch, handler):
    """把 github 客户端替换为 MockTransport，并记录重试等待（不真正 sleep）。"""
    sleeps: list[float] = []
    monkeypatch.setattr(
        github, "_get_client",
        lambda: httpx.Client(transport=httpx.MockTransport(handler)))
    monkeypatch.setattr(github, "_sleep", sleeps.append)
    return sleeps


def make_handler(pulls=None, issues=None, releases=None):
    """按路径路由到对应的固定响应（默认为空列表）。"""
    def handler(request: httpx.Request) -> httpx.Response:
        path = request.url.path
        if path.endswith("/pulls"):
            return httpx.Response(200, json=pulls or [])
        if path.endswith("/issues"):
            return httpx.Response(200, json=issues or [])
        if path.endswith("/releases"):
            return httpx.Response(200, json=releases or [])
        return httpx.Response(404)
    return handler


def pr(number, title=None, created=None, closed=None, merged=None, login="zhangsan"):
    return {
        "number": number,
        "title": title or f"PR {number}",
        "state": "closed" if (closed or merged) else "open",
        "created_at": created,
        "closed_at": closed,
        "merged_at": merged,
        "user": {"login": login},
        "html_url": f"https://github.com/{REPO}/pull/{number}",
    }


def issue(number, title=None, state="open", created=None, closed=None,
          pull_request=False):
    raw = {
        "number": number,
        "title": title or f"Issue {number}",
        "state": state,
        "created_at": created,
        "closed_at": closed,
        "user": {"login": "lisi"},
        "html_url": f"https://github.com/{REPO}/issues/{number}",
    }
    if pull_request:
        raw["pull_request"] = {"url": f"https://api.github.com/repos/{REPO}/pulls/{number}"}
    return raw


def release(rid, name=None, tag="v1.9", published=None, login="zhangsan"):
    return {
        "id": rid,
        "name": name,
        "tag_name": tag,
        "published_at": published,
        "author": {"login": login},
        "html_url": f"https://github.com/{REPO}/releases/tag/{tag}",
    }


def _collect(monkeypatch, pulls=None, issues=None, releases=None):
    install(monkeypatch, make_handler(pulls, issues, releases))
    return activity.collect([REPO], SINCE, UNTIL)


def test_pull_kinds_and_timestamps(monkeypatch):
    """合并优先 merged_at；否则 closed_at → 关闭；否则 created_at → 新建。"""
    pulls = [
        pr(1, created="2026-09-28T10:00:00Z", closed="2026-09-29T09:00:00Z",
           merged="2026-09-29T09:00:00Z"),
        pr(2, created="2026-09-29T08:00:00Z"),
        pr(3, created="2026-09-28T08:00:00Z", closed="2026-09-29T10:00:00Z"),
        pr(4, created="2026-09-28T08:00:00Z"),  # 窗口外
    ]

    events = _collect(monkeypatch, pulls=pulls)

    by_key = {(event.kind, event.number): event for event in events}
    assert set(by_key) == {("pr_merged", 1), ("pr_opened", 2), ("pr_closed", 3)}
    assert by_key[("pr_merged", 1)].timestamp == datetime(2026, 9, 29, 9, 0, tzinfo=TZ)
    assert by_key[("pr_opened", 2)].timestamp == datetime(2026, 9, 29, 8, 0, tzinfo=TZ)
    assert by_key[("pr_closed", 3)].timestamp == datetime(2026, 9, 29, 10, 0, tzinfo=TZ)
    assert by_key[("pr_merged", 1)].author == "zhangsan"
    assert by_key[("pr_merged", 1)].title == "PR 1"
    # 时间倒序
    assert [event.number for event in events] == [3, 1, 2]


def test_issues_exclude_pull_requests_and_classify_state(monkeypatch):
    issues = [
        issue(2, state="closed", created="2026-09-28T08:00:00Z",
              closed="2026-09-29T11:00:00Z"),
        issue(6, state="open", created="2026-09-29T12:00:00Z"),
        issue(7, state="closed", created="2026-09-29T08:00:00Z",
              closed="2026-09-29T13:00:00Z", pull_request=True),
    ]

    events = _collect(monkeypatch, issues=issues)

    by_key = {(event.kind, event.number): event for event in events}
    assert set(by_key) == {("issue_closed", 2), ("issue_opened", 6)}
    assert by_key[("issue_closed", 2)].timestamp == datetime(2026, 9, 29, 11, 0, tzinfo=TZ)
    assert by_key[("issue_opened", 6)].timestamp == datetime(2026, 9, 29, 12, 0, tzinfo=TZ)
    assert 7 not in {event.number for event in events}  # issues 接口混入的 PR 被排除


def test_release_uses_published_at_and_release_id(monkeypatch):
    releases = [
        release(19, name="v1.9", published="2026-09-29T18:00:00Z"),
        release(18, name="v1.8", published="2026-09-28T18:00:00Z"),  # 窗口外
        release(20, name=None, tag="v2.0-draft", published=None),    # 草稿跳过
        release(21, name="", tag="v2.1", published="2026-09-29T09:00:00Z"),
    ]

    events = _collect(monkeypatch, releases=releases)

    by_number = {event.number: event for event in events}
    assert set(by_number) == {19, 21}
    assert by_number[19].kind == "release"
    assert by_number[19].title == "v1.9"
    assert by_number[19].timestamp == datetime(2026, 9, 29, 18, 0, tzinfo=TZ)
    assert by_number[21].title == "v2.1"  # name 为空回退 tag_name


def test_window_filter_and_dedupe(monkeypatch):
    pulls = [
        pr(1, created="2026-09-29T08:00:00Z"),
        pr(1, created="2026-09-29T08:00:00Z"),  # 重复条目
        pr(9, created="2026-09-29T19:00:00Z"),  # 超出 until
        pr(10, created="2026-09-28T23:59:59Z"),  # 早于 since
    ]

    events = _collect(monkeypatch, pulls=pulls)

    assert [(event.kind, event.number) for event in events] == [("pr_opened", 1)]


def test_multiple_repos_are_merged_and_sorted(monkeypatch):
    def handler(request: httpx.Request) -> httpx.Response:
        parts = request.url.path.split("/")  # /repos/{owner}/{repo}/...
        repo = f"{parts[2]}/{parts[3]}"
        if request.url.path.endswith("/pulls"):
            number = 1 if repo == "org/a" else 2
            return httpx.Response(200, json=[pr(number, created="2026-09-29T08:00:00Z")])
        return httpx.Response(200, json=[])

    install(monkeypatch, handler)
    events = activity.collect(["org/a", "org/b"], SINCE, UNTIL)

    assert [(event.repo, event.number) for event in events] == [("org/a", 1), ("org/b", 2)]


def test_http_error_raises_collector_error(monkeypatch):
    install(monkeypatch, lambda request: httpx.Response(500))
    with pytest.raises(CollectorError):
        activity.collect([REPO], SINCE, UNTIL)


def test_non_list_response_raises_collector_error(monkeypatch):
    install(monkeypatch, lambda request: httpx.Response(200, json={"message": "bad"}))
    with pytest.raises(CollectorError):
        activity.collect([REPO], SINCE, UNTIL)


def test_issues_request_passes_since_and_excludes_prs_param(monkeypatch):
    """Issue 接口携带 since 参数（避免拉全量历史）。"""
    seen = {}

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/issues"):
            seen["since"] = request.url.params.get("since")
            seen["state"] = request.url.params.get("state")
        return httpx.Response(200, json=[])

    install(monkeypatch, handler)
    activity.collect([REPO], SINCE, UNTIL)

    assert seen["since"] == SINCE.isoformat()
    assert seen["state"] == "all"
