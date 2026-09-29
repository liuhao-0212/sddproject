"""配置读取与校验（design.md §6.3）。

- 从 config.yaml 读取配置，返回配置对象（dict）
- 必填字段缺失/无效时抛出 ConfigError，错误信息一次性列出全部问题字段
- 每次调用都重新读取文件：定时任务下次执行自动生效，无需重启（配置热更新）
- 敏感信息（API 密钥）一律通过环境变量注入，严禁写入配置文件（design.md §6.2），
  统一使用 env() 读取
"""

from __future__ import annotations

import os
from pathlib import Path
from typing import Any

import yaml

from shared.errors import ConfigError

DEFAULT_DB_PATH = "data/report.db"


def env(name: str, default: str = "") -> str:
    """读取环境变量，未设置时返回默认值。API 密钥统一通过此函数注入。"""
    return os.environ.get(name, default)


def load_config(path: str | Path = "config.yaml") -> dict[str, Any]:
    """读取并校验 config.yaml，返回配置对象（dict）。"""
    cfg_path = Path(path)
    if not cfg_path.is_file():
        raise ConfigError(f"配置文件不存在: {cfg_path}")
    try:
        raw = yaml.safe_load(cfg_path.read_text(encoding="utf-8")) or {}
    except yaml.YAMLError as exc:
        raise ConfigError(f"配置文件 YAML 解析失败: {cfg_path}: {exc}") from exc
    if not isinstance(raw, dict):
        raise ConfigError(f"配置文件顶层必须是对象（mapping），实际为: {type(raw).__name__}")

    errors = _validate(raw)
    if errors:
        raise ConfigError("配置文件校验失败，以下必填字段缺失或无效:\n  - " + "\n  - ".join(errors))
    _apply_defaults(raw)
    return raw


def _section(cfg: dict, *keys: str) -> dict:
    """按路径取嵌套 dict；任何一层不存在或非 dict 时返回空 dict。"""
    node: Any = cfg
    for key in keys:
        node = node.get(key) if isinstance(node, dict) else None
        if node is None:
            return {}
    return node if isinstance(node, dict) else {}


def _validate(cfg: dict) -> list[str]:
    errors: list[str] = []

    def require(path: str, ok: bool, reason: str) -> None:
        if not ok:
            errors.append(f"{path}: {reason}")

    def nonempty_str(value: Any) -> bool:
        return isinstance(value, str) and bool(value.strip())

    def nonempty_list(value: Any) -> bool:
        return isinstance(value, list) and len(value) > 0

    require("team_name", nonempty_str(cfg.get("team_name")), "必须为非空字符串")

    members = cfg.get("members")
    require("members", nonempty_list(members), "必须为非空列表（成员身份映射表，design.md §3.3）")
    if isinstance(members, list):
        for i, member in enumerate(members):
            if not isinstance(member, dict):
                errors.append(f"members[{i}]: 必须为对象")
                continue
            for field in ("name", "github", "lark"):
                require(f"members[{i}].{field}", nonempty_str(member.get(field)), "必须为非空字符串")

    github = _section(cfg, "collector", "github")
    require("collector.github.repos", nonempty_list(github.get("repos")),
            "必须为非空列表（design.md §6.2：仅采集明确列出的仓库）")

    lark_task = _section(cfg, "collector", "lark_task")
    require("collector.lark_task.project_id", nonempty_str(lark_task.get("project_id")),
            "必须为非空字符串")

    lark_msg = _section(cfg, "collector", "lark_msg")
    require("collector.lark_msg.chat_id", nonempty_str(lark_msg.get("chat_id")), "必须为非空字符串")
    require("collector.lark_msg.keywords", nonempty_list(lark_msg.get("keywords")),
            "必须为非空列表")

    email = _section(cfg, "notifier", "email")
    require("notifier.email.smtp_host", nonempty_str(email.get("smtp_host")), "必须为非空字符串")
    require(
        "notifier.email.smtp_port",
        isinstance(email.get("smtp_port"), int) and not isinstance(email.get("smtp_port"), bool),
        "必须为整数",
    )
    require("notifier.email.recipients", nonempty_list(email.get("recipients")), "必须为非空列表")

    return errors


def _apply_defaults(cfg: dict) -> None:
    """为可选字段填充默认值；密钥类字段优先取配置文件值，否则回退环境变量。"""
    lark_msg = cfg.setdefault("collector", {}).setdefault("lark_msg", {})
    lark_msg.setdefault("sensitive_keywords", [])
    lark_bot = cfg.setdefault("notifier", {}).setdefault("lark_bot", {})
    lark_bot.setdefault("webhook_url", env("LARK_BOT_WEBHOOK_URL"))
    cfg.setdefault("storage", {}).setdefault("db_path", DEFAULT_DB_PATH)
