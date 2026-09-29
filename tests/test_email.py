"""notifier.email 单元测试（Mock SMTP，不发起真实网络连接）。"""

import email
import logging
import smtplib
from datetime import date, datetime

import pytest
import yaml

import notifier.email as email_mod
from generator import DailyReport


def make_report():
    return DailyReport(
        date=date(2026, 9, 29),
        team_name="研发一组",
        members=[],
        generated_at=datetime(2026, 9, 29, 18, 0),
        markdown="# 研发一组 日报",
        html="<h1>研发一组 日报（2026-09-29）</h1><p>张三：3 次提交</p>",
    )


class FakeSMTP:
    """记录调用的假 SMTP 服务器。"""

    def __init__(self, host, port):
        self.host = host
        self.port = port
        self.calls: list = []
        self.sent: list[str] = []
        self.failures: list[Exception] = []

    def ehlo(self):
        self.calls.append("ehlo")

    def starttls(self):
        self.calls.append("starttls")

    def login(self, user, password):
        self.calls.append(("login", user, password))

    def sendmail(self, from_addr, to_addrs, payload):
        self.calls.append(("sendmail", from_addr, list(to_addrs)))
        if self.failures:
            raise self.failures.pop(0)
        self.sent.append(payload)

    def quit(self):
        self.calls.append("quit")


def make_factory(per_instance_failures=None):
    """返回 _create_smtp 的替身工厂；per_instance_failures 按实例顺序注入失败。"""
    instances: list[FakeSMTP] = []

    def factory(host, port):
        instance = FakeSMTP(host, port)
        if per_instance_failures:
            instance.failures = list(per_instance_failures.pop(0))
        instances.append(instance)
        return instance

    factory.instances = instances
    return factory


def install(monkeypatch, tmp_path, factory=None, record_sleep=False, port=465,
            with_creds=True, with_from=True):
    """注入 SMTP 配置（tmp config.yaml）、凭据环境变量与假服务器工厂。"""
    sleeps: list[float] = []
    if with_creds:
        monkeypatch.setenv("SMTP_USER", "report@company.com")
        monkeypatch.setenv("SMTP_PASSWORD", "secret")
    if with_from:
        monkeypatch.setenv("SMTP_FROM", "report@company.com")
    cfg = {
        "team_name": "研发一组",
        "members": [{"name": "张三", "github": "zs", "lark": "zs@company.com"}],
        "collector": {
            "github": {"repos": ["org/repo"]},
            "lark_task": {"project_id": "p1"},
            "lark_msg": {"chat_id": "oc_test", "keywords": ["评审"]},
        },
        "notifier": {"email": {"smtp_host": "smtp.company.com", "smtp_port": port,
                               "recipients": ["leader@company.com"]}},
    }
    path = tmp_path / "config.yaml"
    path.write_text(yaml.safe_dump(cfg, allow_unicode=True), encoding="utf-8")
    monkeypatch.setattr(email_mod, "CONFIG_PATH", str(path))
    if factory is not None:
        monkeypatch.setattr(email_mod, "_create_smtp", factory)
    monkeypatch.setattr(email_mod, "_sleep", sleeps.append if record_sleep else (lambda s: None))
    return sleeps


# ---------- 正常场景 ----------

def test_send_success_returns_true_and_sends_html(monkeypatch, tmp_path):
    factory = make_factory()
    install(monkeypatch, tmp_path, factory)

    result = email_mod.send(make_report(), ["leader@company.com"])

    assert result is True
    assert len(factory.instances) == 1
    server = factory.instances[0]
    assert server.host == "smtp.company.com"
    assert server.port == 465
    assert ("login", "report@company.com", "secret") in server.calls

    parsed = email.message_from_string(server.sent[0], policy=email.policy.default)
    assert parsed["Subject"] == "研发一组 日报（2026-09-29）"  # 主题含团队名与日期
    assert parsed["From"] == "report@company.com"
    assert parsed["To"] == "leader@company.com"
    assert parsed.get_content_type() == "text/html"
    assert make_report().html in parsed.get_content()


def test_multiple_recipients(monkeypatch, tmp_path):
    factory = make_factory()
    install(monkeypatch, tmp_path, factory)

    assert email_mod.send(make_report(), ["leader@company.com", "pm@company.com"]) is True

    parsed = email.message_from_string(factory.instances[0].sent[0],
                                       policy=email.policy.default)
    assert parsed["To"] == "leader@company.com, pm@company.com"


def test_starttls_used_for_non_ssl_port(monkeypatch, tmp_path):
    factory = make_factory()
    install(monkeypatch, tmp_path, factory, port=587)

    assert email_mod.send(make_report(), ["leader@company.com"]) is True

    server = factory.instances[0]
    assert server.calls[:3] == ["ehlo", "starttls", "ehlo"]


def test_no_starttls_for_ssl_port(monkeypatch, tmp_path):
    factory = make_factory()
    install(monkeypatch, tmp_path, factory, port=465)

    assert email_mod.send(make_report(), ["leader@company.com"]) is True

    assert "starttls" not in factory.instances[0].calls


def test_no_login_without_credentials(monkeypatch, tmp_path):
    factory = make_factory()
    install(monkeypatch, tmp_path, factory, with_creds=False)

    assert email_mod.send(make_report(), ["leader@company.com"]) is True

    server = factory.instances[0]
    assert not any(isinstance(call, tuple) and call[0] == "login" for call in server.calls)


def test_sender_falls_back_to_smtp_user(monkeypatch, tmp_path):
    factory = make_factory()
    install(monkeypatch, tmp_path, factory, with_from=False)

    assert email_mod.send(make_report(), ["leader@company.com"]) is True

    parsed = email.message_from_string(factory.instances[0].sent[0],
                                       policy=email.policy.default)
    assert parsed["From"] == "report@company.com"  # 回退到 SMTP_USER


# ---------- 错误处理（design.md §6.1） ----------

def test_retry_twice_then_returns_false(monkeypatch, tmp_path, caplog):
    factory = make_factory(per_instance_failures=[[smtplib.SMTPException("boom")]] * 3)
    sleeps = install(monkeypatch, tmp_path, factory, record_sleep=True)

    with caplog.at_level(logging.ERROR):
        result = email_mod.send(make_report(), ["leader@company.com"])

    assert result is False
    assert len(factory.instances) == 1 + email_mod.MAX_RETRIES
    assert sleeps == [email_mod.RETRY_INTERVAL] * email_mod.MAX_RETRIES
    assert "邮件推送失败" in caplog.text


def test_retry_succeeds_on_second_attempt(monkeypatch, tmp_path):
    factory = make_factory(per_instance_failures=[[smtplib.SMTPException("boom")], []])
    sleeps = install(monkeypatch, tmp_path, factory, record_sleep=True)

    result = email_mod.send(make_report(), ["leader@company.com"])

    assert result is True
    assert len(factory.instances) == 2
    assert len(sleeps) == 1
    assert factory.instances[1].sent


def test_empty_recipients_returns_false(monkeypatch, tmp_path, caplog):
    factory = make_factory()
    install(monkeypatch, tmp_path, factory)

    with caplog.at_level(logging.ERROR):
        result = email_mod.send(make_report(), [])

    assert result is False
    assert len(factory.instances) == 0  # 未建立任何连接
    assert "收件人为空" in caplog.text


def test_config_missing_returns_false(monkeypatch, tmp_path, caplog):
    monkeypatch.setattr(email_mod, "CONFIG_PATH", str(tmp_path / "nope.yaml"))

    with caplog.at_level(logging.ERROR):
        result = email_mod.send(make_report(), ["leader@company.com"])

    assert result is False
    assert "SMTP 配置不可用" in caplog.text


def test_create_smtp_uses_ssl_for_465(monkeypatch):
    created: dict = {}

    class FakeSSL:
        def __init__(self, host, port, timeout=None):
            created["ssl"] = (host, port)

    class FakePlain:
        def __init__(self, host, port, timeout=None):
            created["plain"] = (host, port)

    monkeypatch.setattr(email_mod.smtplib, "SMTP_SSL", FakeSSL)
    monkeypatch.setattr(email_mod.smtplib, "SMTP", FakePlain)

    email_mod._create_smtp("smtp.company.com", 465)
    email_mod._create_smtp("smtp.company.com", 587)

    assert created["ssl"] == ("smtp.company.com", 465)
    assert created["plain"] == ("smtp.company.com", 587)
