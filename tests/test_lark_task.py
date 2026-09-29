"""collector.lark_task 单元测试（全部使用 httpx.MockTransport，不发起真实网络请求）。"""

import logging
from datetime import datetime, timezone

import httpx
import pytest

import collector._lark as lark_common
import collector.lark_task as lark

BASE_TIME = datetime(2026, 9, 29, 0, 0)
END_TIME = datetime(2026, 9, 29, 18, 0)
CHANGE_TIME = datetime(2026, 9, 29, 10, 0, tzinfo=timezone.utc)

TOKEN_PATH = "/open-apis/auth/v3/tenant_access_token/internal"
TASK_PATH = "/open-apis/task/v1/tasks"


def ms(ts: datetime) -> int:
    return int(ts.timestamp() * 1000)


def token_response(token="t-1"):
    return httpx.Response(200, json={"code": 0, "tenant_access_token": token, "expire": 7200})


def tasks_response(items, has_more=False, page_token=""):
    return httpx.Response(200, json={
        "code": 0, "data": {"items": items, "has_more": has_more, "page_token": page_token}})


def activity_response(items):
    return httpx.Response(200, json={"code": 0, "data": {"items": items}})


def task_item(task_id, summary="实现登录页", assignee="张三"):
    return {"id": task_id, "summary": summary, "assignee": {"id": "ou_1", "name": assignee}}


def status_item(status_from="未开始", status_to="进行中", updated_at=ms(CHANGE_TIME),
                assignee=None):
    item = {"activity_type": "status", "status_from": status_from, "status_to": status_to,
            "updated_at": updated_at}
    if assignee:
        item["assignee"] = {"name": assignee}
    return item


def install(monkeypatch, handler, record_sleep=False, with_creds=True):
    """替换 _get_client 与 _sleep（默认免等待），并注入飞书应用凭据。"""
    sleeps: list[float] = []
    if with_creds:
        monkeypatch.setenv("LARK_APP_ID", "cli_test")
        monkeypatch.setenv("LARK_APP_SECRET", "sec_test")
    monkeypatch.setattr(lark, "_get_client",
                        lambda: httpx.Client(transport=httpx.MockTransport(handler)))
    monkeypatch.setattr(lark_common, "_sleep", sleeps.append if record_sleep else (lambda s: None))
    return sleeps


# ---------- 正常场景 ----------

def test_collect_returns_all_5_fields_with_correct_types(monkeypatch):
    auth_headers: list = []

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == TOKEN_PATH:
            return token_response()
        if request.url.path == TASK_PATH:
            auth_headers.append(request.headers.get("authorization"))
            return tasks_response([task_item("task-1")])
        return activity_response([status_item()])

    install(monkeypatch, handler)
    records = lark.collect("proj-001", BASE_TIME, END_TIME)

    assert len(records) == 1
    record = records[0]
    assert isinstance(record.assignee, str)
    assert isinstance(record.title, str)
    assert isinstance(record.status_from, str)
    assert isinstance(record.status_to, str)
    assert isinstance(record.updated_at, datetime)
    assert record.assignee == "张三"
    assert record.title == "实现登录页"
    assert record.status_from == "未开始"
    assert record.status_to == "进行中"
    assert record.updated_at == CHANGE_TIME
    assert auth_headers == ["Bearer t-1"]


def test_assignee_falls_back_to_task_list_name(monkeypatch):
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == TOKEN_PATH:
            return token_response()
        if request.url.path == TASK_PATH:
            return tasks_response([task_item("task-1", assignee="王五")])
        return activity_response([status_item()])

    install(monkeypatch, handler)
    records = lark.collect("proj-001", BASE_TIME, END_TIME)

    assert len(records) == 1
    assert records[0].assignee == "王五"


def test_filters_by_time_window(monkeypatch):
    items = [
        status_item(),  # 窗口内
        status_item(status_to="已完成",
                    updated_at=ms(datetime(2026, 9, 28, 10, 0, tzinfo=timezone.utc))),  # 窗口前
        status_item(status_to="待验收",
                    updated_at=ms(datetime(2026, 9, 29, 20, 0, tzinfo=timezone.utc))),  # 窗口后
    ]

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == TOKEN_PATH:
            return token_response()
        if request.url.path == TASK_PATH:
            return tasks_response([task_item("task-1")])
        return activity_response(items)

    install(monkeypatch, handler)
    records = lark.collect("proj-001", BASE_TIME, END_TIME)

    assert len(records) == 1
    assert records[0].status_to == "进行中"


def test_multiple_status_changes_produce_multiple_records(monkeypatch):
    items = [
        status_item(updated_at=ms(CHANGE_TIME)),
        status_item("进行中", "已完成",
                    updated_at=ms(datetime(2026, 9, 29, 12, 0, tzinfo=timezone.utc))),
    ]

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == TOKEN_PATH:
            return token_response()
        if request.url.path == TASK_PATH:
            return tasks_response([task_item("task-1")])
        return activity_response(items)

    install(monkeypatch, handler)
    records = lark.collect("proj-001", BASE_TIME, END_TIME)

    assert [(r.status_from, r.status_to) for r in records] == [
        ("未开始", "进行中"), ("进行中", "已完成")]
    assert records[0].updated_at < records[1].updated_at


def test_non_status_activity_ignored(monkeypatch):
    items = [{"activity_type": "comment", "content": "请尽快处理"}, status_item()]

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == TOKEN_PATH:
            return token_response()
        if request.url.path == TASK_PATH:
            return tasks_response([task_item("task-1")])
        return activity_response(items)

    install(monkeypatch, handler)
    records = lark.collect("proj-001", BASE_TIME, END_TIME)

    assert len(records) == 1


def test_tasks_pagination_follows_page_token(monkeypatch):
    page_tokens: list = []
    activity_paths: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == TOKEN_PATH:
            return token_response()
        if request.url.path == TASK_PATH:
            page_tokens.append(request.url.params.get("page_token"))
            if not page_tokens[-1]:
                return tasks_response([task_item("task-1")], has_more=True, page_token="p2")
            return tasks_response([task_item("task-2")])
        activity_paths.append(request.url.path)
        return activity_response([status_item()])

    install(monkeypatch, handler)
    records = lark.collect("proj-001", BASE_TIME, END_TIME)

    assert page_tokens == [None, "p2"]
    assert activity_paths == [
        "/open-apis/task/v1/tasks/task-1/activity",
        "/open-apis/task/v1/tasks/task-2/activity",
    ]
    assert len(records) == 2


# ---------- 错误处理（design.md §6.1） ----------

def test_token_expired_refreshes_and_retries_once(monkeypatch):
    token_calls: list = []
    list_auth: list = []

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == TOKEN_PATH:
            token_calls.append(request)
            return token_response(f"t-{len(token_calls)}")
        if request.url.path == TASK_PATH:
            list_auth.append(request.headers.get("authorization"))
            if len(list_auth) == 1:
                return httpx.Response(200, json={"code": 99991663,
                                                 "msg": "tenant_access_token invalid"})
            return tasks_response([task_item("task-1")])
        return activity_response([status_item()])

    install(monkeypatch, handler)
    records = lark.collect("proj-001", BASE_TIME, END_TIME)

    assert len(records) == 1
    assert len(token_calls) == 2  # 首次获取 + 过期后刷新
    assert list_auth == ["Bearer t-1", "Bearer t-2"]


def test_token_refresh_retry_still_fails_returns_empty(monkeypatch, caplog):
    token_calls: list = []
    list_calls: list = []

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == TOKEN_PATH:
            token_calls.append(request)
            return token_response(f"t-{len(token_calls)}")
        if request.url.path == TASK_PATH:
            list_calls.append(request)
            return httpx.Response(200, json={"code": 99991663, "msg": "token invalid"})
        return activity_response([status_item()])

    install(monkeypatch, handler)
    with caplog.at_level(logging.ERROR):
        records = lark.collect("proj-001", BASE_TIME, END_TIME)

    assert records == []
    assert len(list_calls) == 2  # 首次 + 刷新后重试 1 次
    assert len(token_calls) == 2
    assert "飞书任务数据源采集失败" in caplog.text


def test_timeout_retries_three_times_then_returns_empty(monkeypatch, caplog):
    list_attempts: list = []

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == TOKEN_PATH:
            return token_response()
        if request.url.path == TASK_PATH:
            list_attempts.append(request)
            raise httpx.ConnectTimeout("timeout", request=request)
        return activity_response([status_item()])

    sleeps = install(monkeypatch, handler, record_sleep=True)
    with caplog.at_level(logging.ERROR):
        records = lark.collect("proj-001", BASE_TIME, END_TIME)

    assert records == []
    assert len(list_attempts) == 1 + lark_common.MAX_RETRIES
    assert sleeps == [lark_common.RETRY_INTERVAL] * lark_common.MAX_RETRIES
    assert "飞书任务数据源采集失败" in caplog.text


def test_activity_failure_skips_task_with_warning(monkeypatch, caplog):
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == TOKEN_PATH:
            return token_response()
        if request.url.path == TASK_PATH:
            return tasks_response([task_item("task-1")])
        raise httpx.ConnectTimeout("timeout", request=request)

    install(monkeypatch, handler)
    with caplog.at_level(logging.WARNING):
        records = lark.collect("proj-001", BASE_TIME, END_TIME)

    assert records == []
    assert "任务动态获取失败" in caplog.text


def test_nonzero_business_code_fails_without_token_retry(monkeypatch, caplog):
    token_calls: list = []
    list_calls: list = []

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == TOKEN_PATH:
            token_calls.append(request)
            return token_response()
        if request.url.path == TASK_PATH:
            list_calls.append(request)
            return httpx.Response(200, json={"code": 500, "msg": "boom"})
        return activity_response([status_item()])

    install(monkeypatch, handler)
    with caplog.at_level(logging.ERROR):
        records = lark.collect("proj-001", BASE_TIME, END_TIME)

    assert records == []
    assert len(list_calls) == 1  # 非 Token 错误码，不刷新不重试
    assert len(token_calls) == 1
    assert "飞书任务数据源采集失败" in caplog.text


def test_missing_credentials_returns_empty(monkeypatch, caplog):
    def handler(request: httpx.Request) -> httpx.Response:
        return token_response()  # 凭据缺失时应提前失败，不会发起任何请求

    install(monkeypatch, handler, with_creds=False)
    with caplog.at_level(logging.ERROR):
        records = lark.collect("proj-001", BASE_TIME, END_TIME)

    assert records == []
    assert "LARK_APP_ID" in caplog.text
