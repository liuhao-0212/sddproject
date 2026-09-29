"""notifier.lark_bot 单元测试（MockTransport 模拟 webhook，不发起真实网络请求）。"""

import json
import logging
from datetime import date, datetime

import httpx
import pytest
import yaml

import notifier.lark_bot as lark_bot
from generator import DailyReport

WEBHOOK = "https://open.feishu.cn/open-apis/bot/v2/hook/token-1"


def make_report():
    return DailyReport(
        date=date(2026, 9, 29),
        team_name="研发一组",
        members=[],
        generated_at=datetime(2026, 9, 29, 18, 0),
        markdown="# 研发一组 日报（2026-09-29）\n\n## 张三\n\n### 代码提交\n\n- [org/repo] feat: 登录页",
        html="<h1>研发一组 日报</h1>",
    )


def install(monkeypatch, tmp_path, handler, record_sleep=False, webhook=WEBHOOK):
    """注入 tmp config.yaml（含 webhook）、假 HTTP 客户端与免等待 sleep。"""
    sleeps: list[float] = []
    cfg = {
        "team_name": "研发一组",
        "members": [{"name": "张三", "github": "zs", "lark": "zs@company.com"}],
        "collector": {
            "github": {"repos": ["org/repo"]},
            "lark_task": {"project_id": "p1"},
            "lark_msg": {"chat_id": "oc_test", "keywords": ["评审"]},
        },
        "notifier": {
            "email": {"smtp_host": "smtp.company.com", "smtp_port": 465,
                      "recipients": ["leader@company.com"]},
            "lark_bot": {"webhook_url": webhook},
        },
    }
    path = tmp_path / "config.yaml"
    path.write_text(yaml.safe_dump(cfg, allow_unicode=True), encoding="utf-8")
    monkeypatch.setattr(lark_bot, "CONFIG_PATH", str(path))
    monkeypatch.setattr(lark_bot, "_get_client",
                        lambda: httpx.Client(transport=httpx.MockTransport(handler)))
    monkeypatch.setattr(lark_bot, "_sleep", sleeps.append if record_sleep else (lambda s: None))
    return sleeps


# ---------- 正常场景 ----------

def test_send_success_returns_true_and_sends_markdown(monkeypatch, tmp_path):
    captured: list = []

    def handler(request: httpx.Request) -> httpx.Response:
        captured.append(request)
        return httpx.Response(200, json={"code": 0, "msg": "success"})

    install(monkeypatch, tmp_path, handler)
    result = lark_bot.send(make_report(), "oc_test")

    assert result is True
    assert len(captured) == 1
    payload = json.loads(captured[0].content)
    assert payload["msg_type"] == "interactive"
    element = payload["card"]["elements"][0]
    assert element["tag"] == "markdown"
    assert element["content"] == make_report().markdown
    assert "研发一组" in payload["card"]["header"]["title"]["content"]
    assert "2026-09-29" in payload["card"]["header"]["title"]["content"]


def test_send_accepts_v1_webhook_response(monkeypatch, tmp_path):
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"StatusCode": 0, "StatusMessage": "success"})

    install(monkeypatch, tmp_path, handler)
    assert lark_bot.send(make_report(), "oc_test") is True


def test_webhook_url_falls_back_to_env(monkeypatch, tmp_path):
    captured_urls: list = []
    monkeypatch.setenv("LARK_BOT_WEBHOOK_URL", "https://example.com/hook")

    def handler(request: httpx.Request) -> httpx.Response:
        captured_urls.append(str(request.url))
        return httpx.Response(200, json={"code": 0})

    install(monkeypatch, tmp_path, handler, webhook="")  # 配置为空 → 回退环境变量
    assert lark_bot.send(make_report(), "oc_test") is True
    assert captured_urls == ["https://example.com/hook"]


# ---------- 错误处理（design.md §6.1） ----------

def test_retry_twice_then_returns_false(monkeypatch, tmp_path, caplog):
    attempts: list = []

    def handler(request: httpx.Request) -> httpx.Response:
        attempts.append(request)
        raise httpx.ConnectTimeout("timeout", request=request)

    sleeps = install(monkeypatch, tmp_path, handler, record_sleep=True)
    with caplog.at_level(logging.ERROR):
        result = lark_bot.send(make_report(), "oc_test")

    assert result is False
    assert len(attempts) == 1 + lark_bot.MAX_RETRIES
    assert sleeps == [lark_bot.RETRY_INTERVAL] * lark_bot.MAX_RETRIES
    assert "飞书推送失败" in caplog.text


def test_retry_succeeds_on_second_attempt(monkeypatch, tmp_path):
    attempts: list = []

    def handler(request: httpx.Request) -> httpx.Response:
        attempts.append(request)
        if len(attempts) == 1:
            raise httpx.ConnectTimeout("timeout", request=request)
        return httpx.Response(200, json={"code": 0})

    sleeps = install(monkeypatch, tmp_path, handler, record_sleep=True)
    result = lark_bot.send(make_report(), "oc_test")

    assert result is True
    assert len(attempts) == 2
    assert len(sleeps) == 1


def test_business_error_retries_then_false(monkeypatch, tmp_path, caplog):
    attempts: list = []

    def handler(request: httpx.Request) -> httpx.Response:
        attempts.append(request)
        return httpx.Response(200, json={"code": 19001, "msg": "invalid webhook"})

    install(monkeypatch, tmp_path, handler)
    with caplog.at_level(logging.ERROR):
        result = lark_bot.send(make_report(), "oc_test")

    assert result is False
    assert len(attempts) == 1 + lark_bot.MAX_RETRIES
    assert "飞书推送失败" in caplog.text


def test_http_error_status_retries_then_false(monkeypatch, tmp_path):
    attempts: list = []

    def handler(request: httpx.Request) -> httpx.Response:
        attempts.append(request)
        return httpx.Response(500, text="server error")

    install(monkeypatch, tmp_path, handler)
    assert lark_bot.send(make_report(), "oc_test") is False
    assert len(attempts) == 1 + lark_bot.MAX_RETRIES


def test_webhook_missing_returns_false(monkeypatch, tmp_path, caplog):
    attempts: list = []
    monkeypatch.delenv("LARK_BOT_WEBHOOK_URL", raising=False)

    def handler(request: httpx.Request) -> httpx.Response:
        attempts.append(request)
        return httpx.Response(200, json={"code": 0})

    install(monkeypatch, tmp_path, handler, webhook="")
    with caplog.at_level(logging.ERROR):
        result = lark_bot.send(make_report(), "oc_test")

    assert result is False
    assert len(attempts) == 0  # 未发起任何请求
    assert "webhook 配置不可用" in caplog.text


def test_empty_chat_id_returns_false(monkeypatch, tmp_path, caplog):
    attempts: list = []

    def handler(request: httpx.Request) -> httpx.Response:
        attempts.append(request)
        return httpx.Response(200, json={"code": 0})

    install(monkeypatch, tmp_path, handler)
    with caplog.at_level(logging.ERROR):
        result = lark_bot.send(make_report(), "")

    assert result is False
    assert len(attempts) == 0
    assert "chat_id 为空" in caplog.text
