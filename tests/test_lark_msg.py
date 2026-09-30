"""collector.lark_msg 单元测试（全部使用 httpx.MockTransport，不发起真实网络请求）。"""

import json
import logging
from datetime import datetime, timezone

import httpx
import pytest
import yaml

import collector._lark as lark_common
import collector.lark_msg as lark_msg

BASE_TIME = datetime(2026, 9, 29, 0, 0)
END_TIME = datetime(2026, 9, 29, 18, 0)
MSG_TIME = datetime(2026, 9, 29, 10, 0, tzinfo=timezone.utc)

TOKEN_PATH = "/open-apis/auth/v3/tenant_access_token/internal"
CHAT_PATH = "/open-apis/im/v1/chats/oc_test"
MSG_PATH = "/open-apis/im/v1/messages"
KEYWORDS = ["评审", "上线"]


def ms(ts: datetime) -> int:
    return int(ts.timestamp() * 1000)


def token_response(token="t-1"):
    return httpx.Response(200, json={"code": 0, "tenant_access_token": token, "expire": 7200})


def chat_response(name="团队群"):
    return httpx.Response(200, json={"code": 0, "data": {"name": name}})


def messages_response(items, has_more=False, page_token=""):
    return httpx.Response(200, json={
        "code": 0, "data": {"items": items, "has_more": has_more, "page_token": page_token}})


def msg_item(sender="张三", text="评审文档已上传", create_time=ms(MSG_TIME), message_id="m1"):
    return {
        "message_id": message_id,
        "sender": {"id": "ou_1", "name": sender},
        "body": {"content": json.dumps({"text": text}, ensure_ascii=False)},
        "create_time": str(create_time),
    }


def install(monkeypatch, handler, record_sleep=False, patch_sensitive=True):
    """替换 _get_client 与 _sleep，注入飞书凭据；默认固定敏感词黑名单。"""
    sleeps: list[float] = []
    monkeypatch.setenv("LARK_APP_ID", "cli_test")
    monkeypatch.setenv("LARK_APP_SECRET", "sec_test")
    if patch_sensitive:
        monkeypatch.setattr(lark_msg, "_sensitive_keywords", lambda: ["薪资", "绩效", "裁员"])
    monkeypatch.setattr(lark_msg, "_get_client",
                        lambda: httpx.Client(transport=httpx.MockTransport(handler)))
    monkeypatch.setattr(lark_common, "_sleep", sleeps.append if record_sleep else (lambda s: None))
    return sleeps


def write_config(tmp_path, sensitive_keywords):
    """写入一个通过校验的最小 config.yaml，含自定义敏感词黑名单。"""
    cfg = {
        "team_name": "研发一组",
        "members": [{"name": "张三", "github": "zhangsan", "lark": "zs@company.com"}],
        "collector": {
            "github": {"repos": ["org/repo"]},
            "lark_task": {"project_id": "p1"},
            "lark_msg": {"chat_id": "oc_test", "keywords": KEYWORDS,
                         "sensitive_keywords": sensitive_keywords},
        },
        "notifier": {"email": {"smtp_host": "smtp.x.com", "smtp_port": 465,
                               "recipients": ["a@x.com"]}},
    }
    path = tmp_path / "config.yaml"
    path.write_text(yaml.safe_dump(cfg, allow_unicode=True), encoding="utf-8")
    return path


# ---------- 正常场景 ----------

def test_collect_returns_all_4_fields_with_correct_types(monkeypatch):
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == TOKEN_PATH:
            return token_response()
        if request.url.path == CHAT_PATH:
            return chat_response()
        if request.url.path == MSG_PATH:
            return messages_response([msg_item()])
        raise AssertionError(f"意外的请求: {request.url}")

    install(monkeypatch, handler)
    records = lark_msg.collect("oc_test", KEYWORDS, BASE_TIME, END_TIME)

    assert len(records) == 1
    record = records[0]
    assert isinstance(record.sender, str)
    assert isinstance(record.content, str)
    assert isinstance(record.timestamp, datetime)
    assert isinstance(record.chat_name, str)
    assert record.sender == "张三"
    assert record.content == "评审文档已上传"
    assert record.timestamp == MSG_TIME
    assert record.chat_name == "团队群"


def test_keyword_filter_only_keeps_matching_messages(monkeypatch):
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == TOKEN_PATH:
            return token_response()
        if request.url.path == CHAT_PATH:
            return chat_response()
        if request.url.path == MSG_PATH:
            return messages_response([
                msg_item(text="评审文档已上传"),
                msg_item(text="今天天气不错", message_id="m2"),
                msg_item(text="上线时间定了", message_id="m3"),
            ])
        raise AssertionError(f"意外的请求: {request.url}")

    install(monkeypatch, handler)
    records = lark_msg.collect("oc_test", KEYWORDS, BASE_TIME, END_TIME)

    assert [r.content for r in records] == ["评审文档已上传", "上线时间定了"]


def test_sensitive_message_excluded_even_if_keyword_matches(monkeypatch):
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == TOKEN_PATH:
            return token_response()
        if request.url.path == CHAT_PATH:
            return chat_response()
        if request.url.path == MSG_PATH:
            return messages_response([
                msg_item(text="关于绩效的评审结论", message_id="m1"),  # 命中关键词也命中敏感词
                msg_item(text="评审通过", message_id="m2"),
            ])
        raise AssertionError(f"意外的请求: {request.url}")

    install(monkeypatch, handler)
    records = lark_msg.collect("oc_test", KEYWORDS, BASE_TIME, END_TIME)

    assert [r.content for r in records] == ["评审通过"]


def test_non_text_message_skipped_plain_string_kept(monkeypatch):
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == TOKEN_PATH:
            return token_response()
        if request.url.path == CHAT_PATH:
            return chat_response()
        if request.url.path == MSG_PATH:
            image = {"message_id": "m-img", "sender": {"id": "ou_1", "name": "张三"},
                     "body": {"content": '{"image_key": "img_xxx"}'},
                     "create_time": str(ms(MSG_TIME))}
            plain = msg_item(text="评审完成", message_id="m-plain")
            plain["body"]["content"] = "评审完成，请查收"  # 普通字符串内容
            return messages_response([image, plain])
        raise AssertionError(f"意外的请求: {request.url}")

    install(monkeypatch, handler)
    records = lark_msg.collect("oc_test", KEYWORDS, BASE_TIME, END_TIME)

    assert len(records) == 1
    assert records[0].content == "评审完成，请查收"


def test_time_window_filter_and_params(monkeypatch):
    captured: dict = {}

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == TOKEN_PATH:
            return token_response()
        if request.url.path == CHAT_PATH:
            return chat_response()
        if request.url.path == MSG_PATH:
            captured.update(request.url.params)
            return messages_response([
                msg_item(text="上线时间定了"),
                msg_item(text="评审文档已上传",
                         create_time=ms(datetime(2026, 9, 28, 10, 0, tzinfo=timezone.utc)),
                         message_id="m-out"),
            ])
        raise AssertionError(f"意外的请求: {request.url}")

    install(monkeypatch, handler)
    records = lark_msg.collect("oc_test", KEYWORDS, BASE_TIME, END_TIME)

    assert [r.content for r in records] == ["上线时间定了"]
    assert captured["container_id_type"] == "chat"
    assert captured["container_id"] == "oc_test"
    assert captured["start_time"] == str(int(
        datetime(2026, 9, 29, 0, 0, tzinfo=timezone.utc).timestamp()))
    assert captured["end_time"] == str(int(
        datetime(2026, 9, 29, 18, 0, tzinfo=timezone.utc).timestamp()))


def test_sender_falls_back_to_id(monkeypatch):
    item = msg_item()
    item["sender"] = {"id": "ou_9"}  # 无 name

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == TOKEN_PATH:
            return token_response()
        if request.url.path == CHAT_PATH:
            return chat_response()
        if request.url.path == MSG_PATH:
            return messages_response([item])
        raise AssertionError(f"意外的请求: {request.url}")

    install(monkeypatch, handler)
    records = lark_msg.collect("oc_test", KEYWORDS, BASE_TIME, END_TIME)

    assert records[0].sender == "ou_9"


def test_messages_pagination_follows_page_token(monkeypatch):
    page_tokens: list = []

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == TOKEN_PATH:
            return token_response()
        if request.url.path == CHAT_PATH:
            return chat_response()
        if request.url.path == MSG_PATH:
            page_tokens.append(request.url.params.get("page_token"))
            if not page_tokens[-1]:
                return messages_response([msg_item(message_id="m1")],
                                         has_more=True, page_token="p2")
            return messages_response([msg_item(text="上线时间定了", message_id="m2")])
        raise AssertionError(f"意外的请求: {request.url}")

    install(monkeypatch, handler)
    records = lark_msg.collect("oc_test", KEYWORDS, BASE_TIME, END_TIME)

    assert page_tokens == [None, "p2"]
    assert len(records) == 2


# ---------- 敏感词黑名单配置（design.md §6.2） ----------

def test_sensitive_keywords_reads_config(tmp_path):
    path = write_config(tmp_path, ["福利", "内推"])
    monkeypatch = pytest.MonkeyPatch()
    try:
        monkeypatch.setattr(lark_msg, "CONFIG_PATH", str(path))
        assert lark_msg._sensitive_keywords() == ["福利", "内推"]
    finally:
        monkeypatch.undo()


def test_sensitive_keywords_default_when_config_missing(tmp_path, caplog):
    monkeypatch = pytest.MonkeyPatch()
    try:
        monkeypatch.setattr(lark_msg, "CONFIG_PATH", str(tmp_path / "nope.yaml"))
        with caplog.at_level(logging.WARNING):
            result = lark_msg._sensitive_keywords()
    finally:
        monkeypatch.undo()
    assert result == lark_msg.DEFAULT_SENSITIVE_KEYWORDS
    assert "使用默认值" in caplog.text


def test_sensitive_keywords_ignores_blank_entries(tmp_path, monkeypatch):
    # 黑名单含空串/纯空白条目：剔除无效条目，保留有效词（§6.2 绝不放行）
    path = write_config(tmp_path, ["", "福利"])
    monkeypatch.setattr(lark_msg, "CONFIG_PATH", str(path))
    assert lark_msg._sensitive_keywords() == ["福利"]

    path2 = write_config(tmp_path, [" "])
    monkeypatch.setattr(lark_msg, "CONFIG_PATH", str(path2))
    assert lark_msg._sensitive_keywords() == lark_msg.DEFAULT_SENSITIVE_KEYWORDS


def test_message_with_sensitive_from_config_excluded(tmp_path, monkeypatch):
    path = write_config(tmp_path, ["福利"])

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == TOKEN_PATH:
            return token_response()
        if request.url.path == CHAT_PATH:
            return chat_response()
        if request.url.path == MSG_PATH:
            return messages_response([
                msg_item(text="福利方案评审", message_id="m1"),  # 命中配置中的敏感词
                msg_item(text="评审通过", message_id="m2"),
            ])
        raise AssertionError(f"意外的请求: {request.url}")

    install(monkeypatch, handler, patch_sensitive=False)
    monkeypatch.setattr(lark_msg, "CONFIG_PATH", str(path))
    records = lark_msg.collect("oc_test", KEYWORDS, BASE_TIME, END_TIME)

    assert [r.content for r in records] == ["评审通过"]


# ---------- 错误处理（design.md §6.1） ----------

def test_last_error_set_on_failure_and_cleared_on_success(monkeypatch):
    def failing_handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == TOKEN_PATH:
            return token_response()
        if request.url.path == CHAT_PATH:
            return chat_response()
        raise httpx.ConnectTimeout("timeout", request=request)

    install(monkeypatch, failing_handler)
    assert lark_msg.collect("oc_test", KEYWORDS, BASE_TIME, END_TIME) == []
    assert lark_msg.get_last_error() is not None

    def ok_handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == TOKEN_PATH:
            return token_response()
        if request.url.path == CHAT_PATH:
            return chat_response()
        return messages_response([])

    install(monkeypatch, ok_handler)
    lark_msg.collect("oc_test", KEYWORDS, BASE_TIME, END_TIME)
    assert lark_msg.get_last_error() is None


def test_token_expired_refreshes_and_retries_once(monkeypatch):
    token_calls: list = []
    msg_auth: list = []

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == TOKEN_PATH:
            token_calls.append(request)
            return token_response(f"t-{len(token_calls)}")
        if request.url.path == CHAT_PATH:
            return chat_response()
        if request.url.path == MSG_PATH:
            msg_auth.append(request.headers.get("authorization"))
            if len(msg_auth) == 1:
                return httpx.Response(200, json={"code": 99991663,
                                                 "msg": "tenant_access_token invalid"})
            return messages_response([msg_item()])
        raise AssertionError(f"意外的请求: {request.url}")

    install(monkeypatch, handler)
    records = lark_msg.collect("oc_test", KEYWORDS, BASE_TIME, END_TIME)

    assert len(records) == 1
    assert len(token_calls) == 2  # 首次获取 + 过期后刷新
    assert msg_auth == ["Bearer t-1", "Bearer t-2"]


def test_timeout_retries_three_times_then_returns_empty(monkeypatch, caplog):
    msg_attempts: list = []

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == TOKEN_PATH:
            return token_response()
        if request.url.path == CHAT_PATH:
            return chat_response()
        if request.url.path == MSG_PATH:
            msg_attempts.append(request)
            raise httpx.ConnectTimeout("timeout", request=request)
        raise AssertionError(f"意外的请求: {request.url}")

    sleeps = install(monkeypatch, handler, record_sleep=True)
    with caplog.at_level(logging.ERROR):
        records = lark_msg.collect("oc_test", KEYWORDS, BASE_TIME, END_TIME)

    assert records == []
    assert len(msg_attempts) == 1 + lark_common.MAX_RETRIES
    assert sleeps == [lark_common.RETRY_INTERVAL] * lark_common.MAX_RETRIES
    assert "飞书消息数据源采集失败" in caplog.text


def test_unexpected_exception_does_not_escape_collect(monkeypatch, caplog):
    def handler(request: httpx.Request) -> httpx.Response:
        return token_response()  # _collect_inner 被替换，不会发起真实请求

    install(monkeypatch, handler)

    def boom(client, chat_id, keywords, since, until):
        raise RuntimeError("内部错误")

    monkeypatch.setattr(lark_msg, "_collect_inner", boom)
    with caplog.at_level(logging.ERROR):
        records = lark_msg.collect("oc_test", KEYWORDS, BASE_TIME, END_TIME)

    assert records == []
    assert lark_msg.get_last_error() is not None
    assert "未预期异常" in caplog.text


def test_chat_name_fetch_failure_falls_back_to_chat_id(monkeypatch, caplog):
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == TOKEN_PATH:
            return token_response()
        if request.url.path == CHAT_PATH:
            raise httpx.ConnectTimeout("timeout", request=request)
        if request.url.path == MSG_PATH:
            return messages_response([msg_item()])
        raise AssertionError(f"意外的请求: {request.url}")

    install(monkeypatch, handler)
    with caplog.at_level(logging.WARNING):
        records = lark_msg.collect("oc_test", KEYWORDS, BASE_TIME, END_TIME)

    assert len(records) == 1
    assert records[0].chat_name == "oc_test"
    assert "使用 chat_id 作为群名称" in caplog.text
