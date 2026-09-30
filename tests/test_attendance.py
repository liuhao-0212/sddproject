"""collector.lark_attendance 单元测试（全部使用 httpx.MockTransport，不发起真实网络请求）。

重点验证 Task 11 验收标准：
- collect() 签名符合 design.md §4.1，返回 CollectResult（含 success 标志位，ADR-003）
- 工时 = check_out - check_in 计算正确
- 签退缺失时状态标注为"签退缺失"
- API 不可用时 success=False 并附带详细错误信息
"""

import json
import logging
from datetime import date, datetime, timezone

import httpx
import pytest

import collector._lark as lark_common
import collector.lark_attendance as lark

SINCE = date(2026, 9, 29)
UNTIL = date(2026, 9, 29)
CHECK_IN = datetime(2026, 9, 29, 9, 0, tzinfo=timezone.utc)
CHECK_OUT = datetime(2026, 9, 29, 18, 0, tzinfo=timezone.utc)

TOKEN_PATH = "/open-apis/auth/v3/tenant_access_token/internal"
ATTENDANCE_PATH = "/open-apis/attendance/v1/user_stats_data/query"

FAKE_CONFIG = {"members": [
    {"name": "张三", "github": "zhangsan", "lark": "ou_1"},
    {"name": "李四", "github": "lisi", "lark": "ou_2"},
]}


def ms(ts: datetime) -> int:
    return int(ts.timestamp() * 1000)


def token_response(token="t-1"):
    return httpx.Response(200, json={"code": 0, "tenant_access_token": token, "expire": 7200})


def attendance_response(items, has_more=False, page_token=""):
    return httpx.Response(200, json={
        "code": 0, "data": {"user_stats_data": items,
                            "has_more": has_more, "page_token": page_token}})


def stats_item(employee_id="ou_1", date_str="20260929", check_in=ms(CHECK_IN),
               check_out=ms(CHECK_OUT), status="normal"):
    return {"employee_id": employee_id, "date": date_str,
            "check_in": check_in, "check_out": check_out, "status": status}


def install(monkeypatch, handler, record_sleep=False, with_creds=True, config=None):
    """替换 _get_client 与 _sleep（默认免等待），并注入飞书应用凭据与成员配置。"""
    sleeps: list[float] = []
    if with_creds:
        monkeypatch.setenv("LARK_APP_ID", "cli_test")
        monkeypatch.setenv("LARK_APP_SECRET", "sec_test")
    monkeypatch.setattr(lark, "load_config",
                        lambda: config if config is not None else FAKE_CONFIG)
    monkeypatch.setattr(lark, "_get_client",
                        lambda: httpx.Client(transport=httpx.MockTransport(handler)))
    monkeypatch.setattr(lark_common, "_sleep", sleeps.append if record_sleep else (lambda s: None))
    return sleeps


# ---------- 正常场景 ----------

def test_collect_returns_collectresult_with_all_fields(monkeypatch):
    bodies: list = []

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == TOKEN_PATH:
            return token_response()
        if request.url.path == ATTENDANCE_PATH:
            bodies.append(json.loads(request.content))
            return attendance_response([stats_item()])
        raise AssertionError(f"unexpected request: {request.url.path}")

    install(monkeypatch, handler)
    result = lark.collect(SINCE, UNTIL)

    assert isinstance(result, lark.CollectResult)
    assert result.success is True
    assert result.error is None
    assert len(result.data) == 1
    record = result.data[0]
    assert isinstance(record.employee_id, str)
    assert isinstance(record.date, date)
    assert isinstance(record.check_in, datetime)
    assert isinstance(record.check_out, datetime)
    assert isinstance(record.work_hours, float)
    assert isinstance(record.status, str)
    assert record.employee_id == "ou_1"
    assert record.date == SINCE
    assert record.check_in == CHECK_IN
    assert record.check_out == CHECK_OUT
    assert record.status == "正常"
    # 请求体携带全部成员与日期窗口（design.md §6.2 仅采集明确列出范围）
    assert bodies == [{
        "employee_type": "email",
        "employee_ids": ["ou_1", "ou_2"],
        "begin_date": "20260929",
        "end_date": "20260929",
        "page_size": 100,
    }]


def test_work_hours_computed_from_check_out_minus_check_in(monkeypatch):
    check_out = datetime(2026, 9, 29, 18, 30, tzinfo=timezone.utc)

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == TOKEN_PATH:
            return token_response()
        return attendance_response([stats_item(check_out=ms(check_out))])

    install(monkeypatch, handler)
    result = lark.collect(SINCE, UNTIL)

    assert result.success is True
    assert result.data[0].work_hours == 9.5


def test_missing_check_out_marked_as_status(monkeypatch):
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == TOKEN_PATH:
            return token_response()
        return attendance_response([stats_item(check_out=None)])

    install(monkeypatch, handler)
    result = lark.collect(SINCE, UNTIL)

    assert result.success is True
    record = result.data[0]
    assert record.check_in == CHECK_IN
    assert record.check_out is None
    assert record.status == "签退缺失"
    assert record.work_hours == 0.0


def test_status_codes_mapped_to_chinese(monkeypatch):
    items = [
        stats_item(employee_id="ou_1", status="normal"),
        stats_item(employee_id="ou_2", status="late"),
        stats_item(employee_id="ou_3", status="early_leave"),
        stats_item(employee_id="ou_4", status="absent", check_in=None, check_out=None),
        stats_item(employee_id="ou_5", status="leave", check_in=None, check_out=None),
    ]

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == TOKEN_PATH:
            return token_response()
        return attendance_response(items)

    install(monkeypatch, handler)
    result = lark.collect(SINCE, UNTIL)

    assert [r.status for r in result.data] == ["正常", "迟到", "早退", "缺勤", "休假"]


def test_empty_data_is_success_not_failure(monkeypatch):
    """ADR-003：接口正常但无数据（success=True）与接口失败必须可区分。"""

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == TOKEN_PATH:
            return token_response()
        return attendance_response([])

    install(monkeypatch, handler)
    result = lark.collect(SINCE, UNTIL)

    assert result.success is True
    assert result.data == []
    assert result.error is None


def test_pagination_follows_page_token(monkeypatch):
    bodies: list = []

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == TOKEN_PATH:
            return token_response()
        body = json.loads(request.content)
        bodies.append(body.get("page_token"))
        if not body.get("page_token"):
            return attendance_response([stats_item("ou_1")], has_more=True, page_token="p2")
        return attendance_response([stats_item("ou_2")])

    install(monkeypatch, handler)
    result = lark.collect(SINCE, UNTIL)

    assert bodies == [None, "p2"]
    assert [r.employee_id for r in result.data] == ["ou_1", "ou_2"]


# ---------- 错误处理（design.md §6.1 / Task 11 验收标准） ----------

def test_api_unavailable_returns_failure_result(monkeypatch, caplog):
    attempts: list = []

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == TOKEN_PATH:
            return token_response()
        attempts.append(request)
        raise httpx.ConnectTimeout("timeout", request=request)

    sleeps = install(monkeypatch, handler, record_sleep=True)
    with caplog.at_level(logging.ERROR):
        result = lark.collect(SINCE, UNTIL)

    assert result.success is False
    assert result.data == []
    assert "已重试" in result.error  # 附带详细错误信息
    assert len(attempts) == 1 + lark_common.MAX_RETRIES
    assert sleeps == [lark_common.RETRY_INTERVAL] * lark_common.MAX_RETRIES
    assert "飞书考勤数据源采集失败" in caplog.text


def test_token_expired_refreshes_and_retries_once(monkeypatch):
    token_calls: list = []
    query_calls: list = []

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == TOKEN_PATH:
            token_calls.append(request)
            return token_response(f"t-{len(token_calls)}")
        query_calls.append(request)
        if len(query_calls) == 1:
            return httpx.Response(200, json={"code": 99991663,
                                             "msg": "tenant_access_token invalid"})
        return attendance_response([stats_item()])

    install(monkeypatch, handler)
    result = lark.collect(SINCE, UNTIL)

    assert result.success is True
    assert len(result.data) == 1
    assert len(token_calls) == 2  # 首次获取 + 过期后刷新
    assert [c.headers["authorization"] for c in query_calls] == ["Bearer t-1", "Bearer t-2"]


def test_nonzero_business_code_fails_without_token_retry(monkeypatch, caplog):
    token_calls: list = []
    query_calls: list = []

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == TOKEN_PATH:
            token_calls.append(request)
            return token_response()
        query_calls.append(request)
        return httpx.Response(200, json={"code": 500, "msg": "boom"})

    install(monkeypatch, handler)
    with caplog.at_level(logging.ERROR):
        result = lark.collect(SINCE, UNTIL)

    assert result.success is False
    assert "boom" in result.error
    assert len(query_calls) == 1  # 非 Token 错误码，不刷新不重试
    assert len(token_calls) == 1


def test_missing_credentials_returns_failure(monkeypatch, caplog):
    def handler(request: httpx.Request) -> httpx.Response:
        return token_response()  # 凭据缺失时应提前失败，不会发起任何请求

    install(monkeypatch, handler, with_creds=False)
    with caplog.at_level(logging.ERROR):
        result = lark.collect(SINCE, UNTIL)

    assert result.success is False
    assert result.data == []
    assert "LARK_APP_ID" in result.error


def test_config_load_failure_returns_failure(monkeypatch, caplog):
    from shared.errors import ConfigError

    def raise_config():
        raise ConfigError("配置文件校验失败")

    def handler(request: httpx.Request) -> httpx.Response:
        return token_response()

    install(monkeypatch, handler)
    monkeypatch.setattr(lark, "load_config", raise_config)
    with caplog.at_level(logging.ERROR):
        result = lark.collect(SINCE, UNTIL)

    assert result.success is False
    assert result.error == "配置文件校验失败"


def test_config_without_member_ids_returns_failure(monkeypatch):
    def handler(request: httpx.Request) -> httpx.Response:
        return token_response()

    install(monkeypatch, handler,
            config={"members": [{"name": "张三", "github": "zhangsan", "lark": ""}]})
    result = lark.collect(SINCE, UNTIL)

    assert result.success is False
    assert "members" in result.error


def test_unexpected_exception_returns_failure_result(monkeypatch, caplog):
    def handler(request: httpx.Request) -> httpx.Response:
        return token_response()  # _collect_inner 被替换，不会发起真实请求

    install(monkeypatch, handler)

    def boom(since, until):
        raise RuntimeError("内部错误")

    monkeypatch.setattr(lark, "_collect_inner", boom)
    with caplog.at_level(logging.ERROR):
        result = lark.collect(SINCE, UNTIL)

    assert result.success is False
    assert result.data == []
    assert "内部错误" in result.error
    assert "未预期异常" in caplog.text


# ---------- 数据清洗 ----------

def test_record_without_employee_id_skipped(monkeypatch, caplog):
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == TOKEN_PATH:
            return token_response()
        return attendance_response([stats_item(employee_id=""), stats_item("ou_2")])

    install(monkeypatch, handler)
    with caplog.at_level(logging.WARNING):
        result = lark.collect(SINCE, UNTIL)

    assert [r.employee_id for r in result.data] == ["ou_2"]
    assert "缺少 employee_id" in caplog.text


def test_unknown_status_kept_with_warning(monkeypatch, caplog):
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == TOKEN_PATH:
            return token_response()
        return attendance_response([stats_item(status="unknown_code")])

    install(monkeypatch, handler)
    with caplog.at_level(logging.WARNING):
        result = lark.collect(SINCE, UNTIL)

    assert result.data[0].status == "unknown_code"
    assert "状态未知" in caplog.text


def test_unparseable_date_skipped(monkeypatch, caplog):
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == TOKEN_PATH:
            return token_response()
        return attendance_response([stats_item(date_str="not-a-date")])

    install(monkeypatch, handler)
    with caplog.at_level(logging.WARNING):
        result = lark.collect(SINCE, UNTIL)

    assert result.success is True
    assert result.data == []
    assert "日期解析失败" in caplog.text
