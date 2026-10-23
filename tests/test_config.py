"""shared.config 单元测试。"""

import yaml
import pytest

import os

from shared.config import env, load_config, load_dotenv
from shared.errors import ConfigError

VALID = {
    "team_name": "研发一组",
    "members": [
        {"name": "张三", "github": "zhangsan", "lark": "zhangsan@company.com"},
        {"name": "李四", "github": "lisi-dev", "lark": "lisi@company.com"},
    ],
    "collector": {
        "github": {"repos": ["org/repo-a", "org/repo-b"]},
        "lark_task": {"project_id": "proj-001"},
        "lark_msg": {"chat_id": "oc_test", "keywords": ["上线", "评审"]},
    },
    "notifier": {
        "email": {
            "smtp_host": "smtp.company.com",
            "smtp_port": 465,
            "recipients": ["leader@company.com"],
        },
        "lark_bot": {"webhook_url": ""},
    },
}


def _write(tmp_path, data):
    path = tmp_path / "config.yaml"
    path.write_text(yaml.safe_dump(data, allow_unicode=True), encoding="utf-8")
    return path


def test_load_valid_config(tmp_path):
    cfg = load_config(_write(tmp_path, VALID))
    assert cfg["team_name"] == "研发一组"
    assert cfg["members"][1]["github"] == "lisi-dev"
    assert cfg["collector"]["github"]["repos"] == ["org/repo-a", "org/repo-b"]


def test_optional_fields_get_defaults(tmp_path, monkeypatch):
    monkeypatch.delenv("LARK_BOT_WEBHOOK_URL", raising=False)
    cfg = load_config(_write(tmp_path, VALID))
    assert cfg["collector"]["lark_msg"]["sensitive_keywords"] == []
    assert cfg["notifier"]["lark_bot"]["webhook_url"] == ""
    assert cfg["storage"]["db_path"] == "data/report.db"


def test_org_name_optional_field(tmp_path):
    """org_name 可选：缺失/非字符串按空串处理，字符串值原样保留。"""
    cfg = load_config(_write(tmp_path, VALID))
    assert cfg["org_name"] == ""  # 未配置：默认空字符串（不参与必填校验）

    cfg = load_config(_write(tmp_path, {**VALID, "org_name": "研一"}))
    assert cfg["org_name"] == "研一"

    cfg = load_config(_write(tmp_path, {**VALID, "org_name": 123}))
    assert cfg["org_name"] == ""  # 非字符串：按空字符串处理


def test_webhook_url_falls_back_to_env(tmp_path, monkeypatch):
    monkeypatch.setenv("LARK_BOT_WEBHOOK_URL", "https://example.com/hook")
    data = {**VALID}
    data["notifier"].pop("lark_bot")
    cfg = load_config(_write(tmp_path, data))
    assert cfg["notifier"]["lark_bot"]["webhook_url"] == "https://example.com/hook"


def test_missing_required_field_raises(tmp_path):
    data = {k: v for k, v in VALID.items() if k != "members"}
    with pytest.raises(ConfigError, match="members"):
        load_config(_write(tmp_path, data))


def test_missing_nested_field_raises_with_path(tmp_path):
    data = {**VALID}
    del data["members"][0]["github"]
    with pytest.raises(ConfigError, match=r"members\[0\]\.github"):
        load_config(_write(tmp_path, data))


def test_multiple_missing_fields_all_reported(tmp_path):
    data = {**VALID}
    del data["members"]
    data["collector"].pop("github")
    with pytest.raises(ConfigError) as exc_info:
        load_config(_write(tmp_path, data))
    message = str(exc_info.value)
    assert "members" in message
    assert "collector.github.repos" in message


def test_missing_file_raises(tmp_path):
    with pytest.raises(ConfigError, match="配置文件不存在"):
        load_config(tmp_path / "nope.yaml")


def test_invalid_yaml_raises(tmp_path):
    path = tmp_path / "config.yaml"
    path.write_text("team_name: [未闭合", encoding="utf-8")
    with pytest.raises(ConfigError, match="YAML"):
        load_config(path)


def test_env_helper(monkeypatch):
    monkeypatch.setenv("TEST_SECRET", "s3cret")
    assert env("TEST_SECRET") == "s3cret"
    assert env("NOT_SET_KEY", "fallback") == "fallback"
# ---------- v1.15：.env 自动加载 ----------

def test_load_dotenv_parses_and_injects(tmp_path, monkeypatch):
    for name in ("DOTENV_TEST_A", "DOTENV_TEST_B", "DOTENV_TEST_C"):
        monkeypatch.delenv(name, raising=False)
    path = tmp_path / ".env"
    path.write_text(
        "# 注释行\n"
        "\n"
        "DOTENV_TEST_A=value\n"
        "export DOTENV_TEST_B='quoted value'\n"
        'DOTENV_TEST_C="dq"\n'
        "没有等号的行\n",
        encoding="utf-8",
    )

    loaded = load_dotenv(path)

    assert loaded == 3
    assert os.environ["DOTENV_TEST_A"] == "value"
    assert os.environ["DOTENV_TEST_B"] == "quoted value"
    assert os.environ["DOTENV_TEST_C"] == "dq"


def test_load_dotenv_does_not_override_existing(tmp_path, monkeypatch):
    monkeypatch.setenv("DOTENV_TEST_KEEP", "shell")
    path = tmp_path / ".env"
    path.write_text("DOTENV_TEST_KEEP=file\n", encoding="utf-8")

    load_dotenv(path)

    assert os.environ["DOTENV_TEST_KEEP"] == "shell"


def test_load_dotenv_missing_file_is_noop(tmp_path):
    assert load_dotenv(tmp_path / "missing.env") == 0
