"""shared.config 单元测试。"""

import yaml
import pytest

from shared.config import env, load_config
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
