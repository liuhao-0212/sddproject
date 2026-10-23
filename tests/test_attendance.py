"""collector.lark_attendance 单元测试（全部使用 httpx.MockTransport，不发起真实网络请求）。

重点验证：
- Task 11 验收标准：CollectResult / 工时计算 / 签退缺失 / API 失败降级
- v1.13 契约（design.md §6.9）：user_tasks/query、employee_type 查询参数、
  整数日期、attendance 优先与通讯录解析（employee_id → user_id 回退）、
  同一天多条打卡合并、原始响应日志、404 文案
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
ATTENDANCE_PATH = "/open-apis/attendance/v1/user_tasks/query"
CONTACT_PREFIX = "/open-apis/contact/v3/users/"

# v1.13：默认成员显式配置 attendance，避免大多数用例触发通讯录请求
FAKE_CONFIG = {"members": [
    {"name": "张三", "github": "zhangsan", "lark": "ou_1", "attendance": "emp_1"},
    {"name": "李四", "github": "lisi", "lark": "ou_2", "attendance": "emp_2"},
]}


def token_response(token="t-1"):
    return httpx.Response(200, json={"code": 0, "tenant_access_token": token, "expire": 7200})


def attendance_response(tasks, **extra):
    data = {"user_task_results": tasks}
    data.update(extra)
    return httpx.Response(200, json={"code": 0, "data": data})


def contact_response(employee_id="emp_9", user_id=""):
    user = {}
    if employee_id:
        user["employee_id"] = employee_id
    if user_id:
        user["user_id"] = user_id
    return httpx.Response(200, json={"code": 0, "data": {"user": user}})


def check_time_value(ts: datetime, fmt: str):
    if fmt == "epoch":
        return int(ts.timestamp())
    if fmt == "epoch_ms":
        return int(ts.timestamp() * 1000)
    if fmt == "datetime":
        return ts.strftime("%Y-%m-%d %H:%M:%S")
    if fmt == "iso":
        return ts.isoformat()
    raise AssertionError(f"unknown format: {fmt}")


def punch_record(check_in=CHECK_IN, check_out=CHECK_OUT, in_result="Normal",
                 out_result="Normal", fmt="epoch"):
    return {
        "check_in_record": {"check_time": check_time_value(check_in, fmt)} if check_in else {},
        "check_out_record": {"check_time": check_time_value(check_out, fmt)} if check_out else {},
        "check_in_result": in_result,
        "check_out_result": out_result,
    }


def user_task(records=None, day=20260929, user_id="emp_1"):
    return {"user_id": user_id, "day": day,
            "records": records if records is not None else [punch_record()]}


def install(monkeypatch, handler, record_sleep=False, with_creds=True, config=None):
    """替换 _get_client 与 _sleep（默认免等待），并注入飞书应用凭据与成员配置。"""
    sleeps: list[float] = []
    if with_creds:
        monkeypatch.setenv("LARK_APP_ID", "cli_test")
        monkeypatch.setenv("LARK_APP_SECRET", "sec_test")
    else:
        # 宿主环境可能残留真实凭据（shell 配置等），必须显式清除，保证测试环境隔离
        monkeypatch.delenv("LARK_APP_ID", raising=False)
        monkeypatch.delenv("LARK_APP_SECRET", raising=False)
    monkeypatch.setattr(lark, "load_config",
                        lambda: config if config is not None else FAKE_CONFIG)
    monkeypatch.setattr(lark, "_get_client",
                        lambda: httpx.Client(transport=httpx.MockTransport(handler)))
    monkeypatch.setattr(lark_common, "_sleep", sleeps.append if record_sleep else (lambda s: None))
    return sleeps


# ---------- 正常场景 ----------

def test_collect_returns_collectresult_with_all_fields(monkeypatch):
    requests: list[httpx.Request] = []
    bodies: list = []

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == TOKEN_PATH:
            return token_response()
        if request.url.path == ATTENDANCE_PATH:
            requests.append(request)
            bodies.append(json.loads(request.content))
            return attendance_response([user_task()])
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
    assert record.employee_id == "emp_1"
    assert record.date == SINCE
    assert record.check_in == CHECK_IN
    assert record.check_out == CHECK_OUT
    assert record.work_hours == 9.0
    assert record.status == "正常"
    # v1.13 契约：user_tasks/query + employee_type 作为 URL 查询参数
    assert lark.ATTENDANCE_URL.endswith("/attendance/v1/user_tasks/query")
    assert requests[0].url.path == ATTENDANCE_PATH
    assert requests[0].url.params["employee_type"] == "employee_id"
    # 请求体字段与官方契约一致（整数日期 yyyyMMdd）
    assert bodies == [{
        "user_ids": ["emp_1", "emp_2"],
        "check_date_from": 20260929,
        "check_date_to": 20260929,
    }]


def test_work_hours_computed_from_check_out_minus_check_in(monkeypatch):
    check_out = datetime(2026, 9, 29, 18, 30, tzinfo=timezone.utc)

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == TOKEN_PATH:
            return token_response()
        return attendance_response([user_task([punch_record(check_out=check_out)])])

    install(monkeypatch, handler)
    result = lark.collect(SINCE, UNTIL)

    assert result.success is True
    assert result.data[0].work_hours == 9.5


def test_normal_status(monkeypatch):
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == TOKEN_PATH:
            return token_response()
        return attendance_response([user_task()])

    install(monkeypatch, handler)
    result = lark.collect(SINCE, UNTIL)

    assert result.success is True
    assert result.data[0].status == "正常"


def test_late_status(monkeypatch):
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == TOKEN_PATH:
            return token_response()
        return attendance_response([
            user_task([punch_record(in_result="Late")]),
        ])

    install(monkeypatch, handler)
    result = lark.collect(SINCE, UNTIL)

    assert result.success is True
    assert result.data[0].status == "迟到"


def test_early_leave_status(monkeypatch):
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == TOKEN_PATH:
            return token_response()
        return attendance_response([
            user_task([punch_record(out_result="Early")]),
        ])

    install(monkeypatch, handler)
    result = lark.collect(SINCE, UNTIL)

    assert result.success is True
    assert result.data[0].status == "早退"


def test_missing_check_out_marked_as_status(monkeypatch):
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == TOKEN_PATH:
            return token_response()
        return attendance_response([user_task([punch_record(check_out=None)])])

    install(monkeypatch, handler)
    result = lark.collect(SINCE, UNTIL)

    assert result.success is True
    record = result.data[0]
    assert record.check_in == CHECK_IN
    assert record.check_out is None
    assert record.status == "签退缺失"
    assert record.work_hours == 0.0


def test_unknown_status(monkeypatch):
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == TOKEN_PATH:
            return token_response()
        return attendance_response([
            user_task([punch_record(in_result="Abnormal", out_result="Abnormal")]),
        ])

    install(monkeypatch, handler)
    result = lark.collect(SINCE, UNTIL)

    assert result.success is True
    assert result.data[0].status == "未知"


def test_same_day_records_merged_earliest_in_latest_out(monkeypatch):
    one_hour_later = CHECK_IN.replace(hour=10)
    two_hours_later = CHECK_IN.replace(hour=11)
    records = [
        punch_record(check_in=CHECK_IN, check_out=one_hour_later,
                     in_result="Late", out_result="Normal"),
        punch_record(check_in=two_hours_later, check_out=CHECK_OUT,
                     in_result="Normal", out_result="Normal"),
    ]

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == TOKEN_PATH:
            return token_response()
        return attendance_response([user_task(records)])

    install(monkeypatch, handler)
    result = lark.collect(SINCE, UNTIL)

    assert result.success is True
    assert len(result.data) == 1
    record = result.data[0]
    assert record.check_in == CHECK_IN          # 最早签到
    assert record.check_out == CHECK_OUT        # 最晚签退
    assert record.work_hours == 9.0
    assert record.status == "迟到"              # 第一条非「正常」


def test_empty_user_task_results_is_success_not_failure(monkeypatch):
    """ADR-003：接口正常但无打卡记录（success=True）与接口失败必须可区分。"""

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == TOKEN_PATH:
            return token_response()
        return attendance_response([])

    install(monkeypatch, handler)
    result = lark.collect(SINCE, UNTIL)

    assert result.success is True
    assert result.data == []
    assert result.error is None


@pytest.mark.parametrize("fmt", ["epoch", "epoch_ms", "datetime", "iso"])
def test_check_time_formats_parsed(monkeypatch, fmt):
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == TOKEN_PATH:
            return token_response()
        return attendance_response([user_task([punch_record(fmt=fmt)])])

    install(monkeypatch, handler)
    result = lark.collect(SINCE, UNTIL)

    assert result.success is True
    assert result.data[0].check_in == CHECK_IN
    assert result.data[0].check_out == CHECK_OUT
    assert result.data[0].work_hours == 9.0


# ---------- v1.13：员工 ID 解析（design.md §6.9） ----------

def test_explicit_attendance_takes_priority_over_contact(monkeypatch):
    bodies: list[dict] = []
    config = {"members": [
        {"name": "张三", "github": "zhangsan", "lark": "ou_1", "attendance": "emp_cfg"},
    ]}

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == TOKEN_PATH:
            return token_response()
        if request.url.path.startswith(CONTACT_PREFIX):
            raise AssertionError("显式配置 attendance 时不应请求通讯录")
        bodies.append(json.loads(request.content))
        return attendance_response([user_task(user_id="emp_cfg")])

    install(monkeypatch, handler, config=config)
    result = lark.collect(SINCE, UNTIL)

    assert result.success is True
    assert bodies[0]["user_ids"] == ["emp_cfg"]
    assert result.data[0].employee_id == "emp_cfg"


def test_open_id_resolved_via_contact_api(monkeypatch):
    contact_requests: list[httpx.Request] = []
    bodies: list[dict] = []
    config = {"members": [
        {"name": "张三", "github": "zhangsan", "lark": "ou_1"},
    ]}

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == TOKEN_PATH:
            return token_response()
        if request.url.path.startswith(CONTACT_PREFIX):
            contact_requests.append(request)
            return contact_response("emp_9")
        bodies.append(json.loads(request.content))
        return attendance_response([user_task(user_id="emp_9")])

    install(monkeypatch, handler, config=config)
    result = lark.collect(SINCE, UNTIL)

    assert result.success is True
    assert len(contact_requests) == 1
    assert contact_requests[0].url.path == f"{CONTACT_PREFIX}ou_1"
    assert contact_requests[0].url.params["user_id_type"] == "open_id"
    assert bodies[0]["user_ids"] == ["emp_9"]
    assert [r.employee_id for r in result.data] == ["emp_9"]


def test_contact_falls_back_to_user_id_when_employee_id_missing(monkeypatch):
    bodies: list[dict] = []
    config = {"members": [
        {"name": "张三", "github": "zhangsan", "lark": "ou_1"},
    ]}

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == TOKEN_PATH:
            return token_response()
        if request.url.path.startswith(CONTACT_PREFIX):
            return contact_response(employee_id="", user_id="c9916d91")
        bodies.append(json.loads(request.content))
        return attendance_response([user_task(user_id="c9916d91")])

    install(monkeypatch, handler, config=config)
    result = lark.collect(SINCE, UNTIL)

    assert result.success is True
    assert bodies[0]["user_ids"] == ["c9916d91"]
    assert result.data[0].employee_id == "c9916d91"
    # 编排层据该映射把考勤记录归属到成员
    assert lark.get_last_employee_ids() == {"ou_1": "c9916d91"}


def test_contact_failure_skips_member_but_keeps_others(monkeypatch, caplog):
    bodies: list[dict] = []
    config = {"members": [
        {"name": "张三", "github": "zhangsan", "lark": "ou_1"},
        {"name": "李四", "github": "lisi", "lark": "ou_2"},
    ]}

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == TOKEN_PATH:
            return token_response()
        if request.url.path.startswith(CONTACT_PREFIX):
            if request.url.path.endswith("ou_1"):
                return httpx.Response(200, json={"code": 500, "msg": "user not found"})
            return contact_response("emp_2")
        bodies.append(json.loads(request.content))
        return attendance_response([user_task(user_id="emp_2")])

    install(monkeypatch, handler, config=config)
    with caplog.at_level(logging.WARNING):
        result = lark.collect(SINCE, UNTIL)

    assert result.success is True
    assert bodies[0]["user_ids"] == ["emp_2"]
    assert "通讯录解析 employee_id 失败" in caplog.text


def test_all_employee_ids_unresolved_returns_failure(monkeypatch, caplog):
    attendance_calls: list[httpx.Request] = []
    config = {"members": [
        {"name": "张三", "github": "zhangsan", "lark": "ou_1"},
        {"name": "李四", "github": "lisi", "lark": "ou_2"},
    ]}

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == TOKEN_PATH:
            return token_response()
        if request.url.path.startswith(CONTACT_PREFIX):
            return contact_response(employee_id="", user_id="")  # 两个 ID 都缺失
        attendance_calls.append(request)
        return attendance_response([])

    install(monkeypatch, handler, config=config)
    with caplog.at_level(logging.WARNING):
        result = lark.collect(SINCE, UNTIL)

    assert result.success is False
    assert result.data == []
    assert "员工 ID" in result.error
    assert attendance_calls == []
    assert "通讯录响应缺少 employee_id 与 user_id" in caplog.text


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
        return attendance_response([user_task()])

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


def test_non_2xx_error_message_includes_body_summary():
    """非 2xx 时错误消息附带响应体摘要（截断 500 字符），且不泄露请求头。"""
    body_text = '{"code":99991672,"msg":"Access denied:' + "x" * 600 + '"}'

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(400, text=body_text)

    client = httpx.Client(transport=httpx.MockTransport(handler))
    try:
        with pytest.raises(lark_common.CollectorError) as excinfo:
            lark_common.request_with_retry(
                client, "GET",
                "https://open.feishu.cn/open-apis/contact/v3/users/ou_1")
    finally:
        client.close()

    message = str(excinfo.value)
    assert "400" in message
    assert "| body=" in message
    assert message.endswith(body_text[:500])
    assert len(message.split("| body=", 1)[1]) == 500
    assert "Authorization" not in message
    assert "Bearer" not in message


def test_404_message_mentions_path_or_module(monkeypatch, caplog):
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == TOKEN_PATH:
            return token_response()
        return httpx.Response(404, text="not found")

    install(monkeypatch, handler)
    with caplog.at_level(logging.ERROR):
        result = lark.collect(SINCE, UNTIL)

    assert result.success is False
    assert result.error == "飞书考勤接口 404（路径错误或考勤模块未开通）"


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
    assert "员工 ID" in result.error


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

def test_record_without_user_id_skipped(monkeypatch, caplog):
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == TOKEN_PATH:
            return token_response()
        return attendance_response([
            user_task(user_id=""),
            user_task(user_id="emp_2"),
        ])

    install(monkeypatch, handler)
    with caplog.at_level(logging.WARNING):
        result = lark.collect(SINCE, UNTIL)

    assert [r.employee_id for r in result.data] == ["emp_2"]
    assert "缺少 user_id" in caplog.text


def test_unparseable_date_skipped(monkeypatch, caplog):
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == TOKEN_PATH:
            return token_response()
        return attendance_response([user_task(day="not-a-date")])

    install(monkeypatch, handler)
    with caplog.at_level(logging.WARNING):
        result = lark.collect(SINCE, UNTIL)

    assert result.success is True
    assert result.data == []
    assert "日期解析失败" in caplog.text


def test_unparsed_response_logs_raw_payload_truncated(monkeypatch, caplog):
    """解析不出记录时记录真实响应，截断 2000 字符。"""

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == TOKEN_PATH:
            return token_response()
        return attendance_response([], debug="x" * 5000)

    install(monkeypatch, handler)
    with caplog.at_level(logging.INFO):
        result = lark.collect(SINCE, UNTIL)

    assert result.success is True
    raw_logs = [r for r in caplog.records if "考勤原始响应" in r.getMessage()]
    assert raw_logs
    assert len(raw_logs[0].payload) == lark.RAW_PAYLOAD_LIMIT
