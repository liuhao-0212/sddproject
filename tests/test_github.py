"""collector.github 单元测试（全部使用 httpx.MockTransport，不发起真实网络请求）。"""

import logging
import time
from datetime import datetime, timezone

import httpx
import pytest

import collector.github as github

BASE_TIME = datetime(2026, 9, 29, 0, 0)
END_TIME = datetime(2026, 9, 29, 18, 0)


def commit_payload(sha, author="zhangsan", message="feat: 登录页",
                   date_str="2026-09-29T08:30:00Z"):
    return {
        "sha": sha,
        "commit": {"message": message, "author": {"name": author, "date": date_str}},
        "author": {"login": author},
    }


def detail_payload(sha, additions=10, deletions=3, files=2):
    return {
        "sha": sha,
        "stats": {"additions": additions, "deletions": deletions,
                  "total": additions + deletions},
        "files": [{"filename": f"f{i}.py"} for i in range(files)],
    }


def install(monkeypatch, handler, record_sleep=False):
    """替换 _get_client 与 _sleep（默认免等待），返回记录到的 sleep 时长列表。"""
    sleeps: list[float] = []
    monkeypatch.setattr(github, "_get_client",
                        lambda: httpx.Client(transport=httpx.MockTransport(handler)))
    monkeypatch.setattr(github, "_sleep",
                        sleeps.append if record_sleep else (lambda s: None))
    return sleeps


# ---------- 正常场景 ----------

def test_collect_returns_all_7_fields_with_correct_types(monkeypatch):
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/commits"):
            return httpx.Response(200, json=[commit_payload("c1"),
                                             commit_payload("c2", author="lisi")])
        return httpx.Response(200, json=detail_payload(request.url.path.rsplit("/", 1)[-1]))

    install(monkeypatch, handler)
    records = github.collect(["org/repo"], BASE_TIME, END_TIME)

    assert len(records) == 2
    for record in records:
        assert isinstance(record.author, str)
        assert isinstance(record.message, str)
        assert isinstance(record.timestamp, datetime)
        assert isinstance(record.repo, str)
        assert isinstance(record.additions, int)
        assert isinstance(record.deletions, int)
        assert isinstance(record.files_changed, int)

    first = records[0]
    assert first.author == "zhangsan"
    assert first.message == "feat: 登录页"
    assert first.timestamp == datetime(2026, 9, 29, 8, 30, tzinfo=timezone.utc)
    assert first.repo == "org/repo"
    assert first.additions == 10
    assert first.deletions == 3
    assert first.files_changed == 2


def test_collect_supports_pagination(monkeypatch):
    monkeypatch.setattr(github, "PER_PAGE", 2)
    pages: list[int] = []

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/commits"):
            page = int(request.url.params.get("page"))
            pages.append(page)
            if page == 1:
                return httpx.Response(200, json=[commit_payload("c1"), commit_payload("c2")])
            if page == 2:
                return httpx.Response(200, json=[commit_payload("c3")])
            return httpx.Response(200, json=[])
        return httpx.Response(200, json=detail_payload(request.url.path.rsplit("/", 1)[-1]))

    install(monkeypatch, handler)
    records = github.collect(["org/repo"], BASE_TIME, END_TIME)

    assert pages == [1, 2]
    assert len(records) == 3


def test_collect_passes_since_until_params(monkeypatch):
    captured: dict = {}

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/commits"):
            captured.update(request.url.params)
            return httpx.Response(200, json=[])
        return httpx.Response(200, json=detail_payload("c1"))

    install(monkeypatch, handler)
    github.collect(["org/repo"], BASE_TIME, END_TIME)

    assert captured["since"] == "2026-09-29T00:00:00"
    assert captured["until"] == "2026-09-29T18:00:00"
    assert str(captured["per_page"]) == "100"


def test_collect_iterates_all_repos(monkeypatch):
    repos_seen: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/commits"):
            repo = request.url.path.split("/repos/")[1].split("/commits")[0]
            repos_seen.append(repo)
            return httpx.Response(200, json=[commit_payload(f"{repo}-c1")])
        return httpx.Response(200, json=detail_payload(request.url.path.rsplit("/", 1)[-1]))

    install(monkeypatch, handler)
    records = github.collect(["org/a", "org/b"], BASE_TIME, END_TIME)

    assert repos_seen == ["org/a", "org/b"]
    assert {r.repo for r in records} == {"org/a", "org/b"}


def test_author_falls_back_to_commit_author_name(monkeypatch):
    payload = {
        "sha": "c1",
        "commit": {"message": "m", "author": {"name": "wangwu", "date": "2026-09-29T09:00:00Z"}},
    }

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/commits"):
            return httpx.Response(200, json=[payload])
        return httpx.Response(200, json=detail_payload("c1"))

    install(monkeypatch, handler)
    records = github.collect(["org/repo"], BASE_TIME, END_TIME)

    assert records[0].author == "wangwu"


def test_commit_with_unparseable_date_is_skipped(monkeypatch, caplog):
    payload = {"sha": "bad",
               "commit": {"message": "m", "author": {"name": "x", "date": "not-a-date"}}}

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/commits"):
            return httpx.Response(200, json=[payload])
        return httpx.Response(200, json=detail_payload("bad"))

    install(monkeypatch, handler)
    with caplog.at_level(logging.WARNING):
        records = github.collect(["org/repo"], BASE_TIME, END_TIME)

    assert records == []
    assert "时间解析失败" in caplog.text


# ---------- 错误处理（design.md §6.1） ----------

def test_timeout_retries_three_times_then_returns_empty(monkeypatch, caplog):
    attempts: list = []

    def handler(request: httpx.Request) -> httpx.Response:
        attempts.append(request)
        raise httpx.ConnectTimeout("timeout", request=request)

    sleeps = install(monkeypatch, handler, record_sleep=True)
    with caplog.at_level(logging.ERROR):
        records = github.collect(["org/repo"], BASE_TIME, END_TIME)

    assert records == []
    assert len(attempts) == 1 + github.MAX_RETRIES
    assert sleeps == [github.RETRY_INTERVAL] * github.MAX_RETRIES
    assert "github 数据源采集失败" in caplog.text


def test_rate_limit_waits_for_reset_then_retries(monkeypatch):
    now = int(time.time())
    list_attempts: list = []

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/commits"):
            list_attempts.append(request)
            if len(list_attempts) == 1:
                return httpx.Response(403, headers={
                    "X-RateLimit-Remaining": "0",
                    "X-RateLimit-Reset": str(now + 120),
                })
            return httpx.Response(200, json=[commit_payload("c1")])
        return httpx.Response(200, json=detail_payload("c1"))

    sleeps = install(monkeypatch, handler, record_sleep=True)
    records = github.collect(["org/repo"], BASE_TIME, END_TIME)

    assert len(records) == 1
    assert len(list_attempts) == 2
    assert len(sleeps) == 1
    assert sleeps[0] >= 120  # reset(now+120) - now + 缓冲


def test_rate_limit_retry_after_header(monkeypatch):
    list_attempts: list = []

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/commits"):
            list_attempts.append(request)
            if len(list_attempts) == 1:
                return httpx.Response(403, headers={"Retry-After": "3"})
            return httpx.Response(200, json=[])
        return httpx.Response(200, json=detail_payload("c1"))

    sleeps = install(monkeypatch, handler, record_sleep=True)
    records = github.collect(["org/repo"], BASE_TIME, END_TIME)

    assert records == []
    assert len(list_attempts) == 2
    assert sleeps == [3.0]


def test_rate_limit_retries_exhausted_returns_empty(monkeypatch, caplog):
    list_attempts: list = []

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/commits"):
            list_attempts.append(request)
            return httpx.Response(403, headers={"Retry-After": "1"})
        return httpx.Response(200, json=detail_payload("c1"))

    sleeps = install(monkeypatch, handler, record_sleep=True)
    with caplog.at_level(logging.ERROR):
        records = github.collect(["org/repo"], BASE_TIME, END_TIME)

    assert records == []
    assert len(list_attempts) == 1 + github.MAX_RETRIES
    assert len(sleeps) == github.MAX_RETRIES
    assert "github 数据源采集失败" in caplog.text


def test_non_rate_limit_error_fails_without_retry(monkeypatch, caplog):
    attempts: list = []

    def handler(request: httpx.Request) -> httpx.Response:
        attempts.append(request)
        return httpx.Response(404, json={"message": "Not Found"})

    install(monkeypatch, handler)
    with caplog.at_level(logging.ERROR):
        records = github.collect(["org/nope"], BASE_TIME, END_TIME)

    assert records == []
    assert len(attempts) == 1
    assert "github 数据源采集失败" in caplog.text


def test_detail_failure_keeps_commit_with_zero_stats(monkeypatch, caplog):
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/commits"):
            return httpx.Response(200, json=[commit_payload("c1")])
        raise httpx.ConnectTimeout("timeout", request=request)

    install(monkeypatch, handler)
    with caplog.at_level(logging.WARNING):
        records = github.collect(["org/repo"], BASE_TIME, END_TIME)

    assert len(records) == 1
    assert records[0].additions == 0
    assert records[0].deletions == 0
    assert records[0].files_changed == 0
    assert "变更统计获取失败" in caplog.text


# ---------- 客户端构造 ----------

def test_get_client_uses_token_from_env(monkeypatch):
    monkeypatch.setenv("GITHUB_TOKEN", "ghp_test")
    client = github._get_client()
    try:
        assert client.headers["Authorization"] == "Bearer ghp_test"
    finally:
        client.close()


def test_get_client_without_token_has_no_auth(monkeypatch):
    monkeypatch.delenv("GITHUB_TOKEN", raising=False)
    client = github._get_client()
    try:
        assert "Authorization" not in client.headers
    finally:
        client.close()
