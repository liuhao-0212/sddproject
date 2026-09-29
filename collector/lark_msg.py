"""飞书消息采集模块：获取指定群时间范围内的消息，按关键词过滤（design.md §4.1）。

接口契约: collect(chat_id, keywords, since, until) -> list[MessageRecord]

API 流程:
1. POST /auth/v3/tenant_access_token/internal 获取 tenant_access_token
2. GET /im/v1/chats/{chat_id} 获取群名称
3. GET /im/v1/messages 分页拉取消息（container_id_type=chat，start/end_time 为秒级时间戳）

过滤规则:
- 关键词过滤：仅保留包含任一 keywords 的文本消息
- 敏感词黑名单（design.md §6.2）：命中 sensitive_keywords 的消息一律剔除，
  黑名单读取 config.yaml 的 collector.lark_msg.sensitive_keywords；
  配置缺失/读取失败时使用内置默认值（薪资/绩效/裁员），绝不放行

错误处理（design.md §6.1）:
- 超时/网络错误重试 3 次（间隔 5s）
- Token 过期自动刷新后重试 1 次
- 采集失败返回空列表 + 错误日志；群名称获取失败退化为 chat_id

Token 管理与重试逻辑见 collector/_lark.py（与 lark_task 共用）。
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import datetime
from typing import Any

import httpx

from collector._lark import (
    BASE_URL,
    MAX_PAGES,
    PAGE_SIZE,
    TokenManager,
    get_lark_client,
    parse_time,
    request_with_token,
    to_utc,
)
from shared.config import env, load_config
from shared.errors import CollectorError
from shared.logger import get_logger

logger = get_logger("collector.lark_msg")

CHAT_URL = f"{BASE_URL}/im/v1/chats"
MESSAGE_LIST_URL = f"{BASE_URL}/im/v1/messages"
# 配置读取路径（测试中可替换）
CONFIG_PATH = "config.yaml"
# 敏感词兜底默认值（design.md §6.2：薪资/绩效/裁员等；配置缺失时兜底，绝不放行）
DEFAULT_SENSITIVE_KEYWORDS = ["薪资", "绩效", "裁员"]


@dataclass
class MessageRecord:
    """消息记录（design.md §3.1）。"""

    sender: str          # 发送者（飞书用户名）
    content: str         # 消息内容（纯文本）
    timestamp: datetime  # 发送时间
    chat_name: str       # 群名称


def collect(chat_id: str, keywords: list[str], since: datetime,
            until: datetime) -> list[MessageRecord]:
    """获取飞书群在 [since, until] 内命中关键词的消息记录。

    采集失败返回空列表并记录错误日志（由生成层在日报中标注"数据获取失败"）。
    """
    client = _get_client()
    try:
        try:
            return _collect_inner(client, chat_id, keywords, since, until)
        except CollectorError as exc:
            logger.error("飞书消息数据源采集失败，返回空列表",
                         extra={"chat_id": chat_id, "reason": str(exc)}, exc_info=exc)
            return []
    finally:
        client.close()


def _collect_inner(client: httpx.Client, chat_id: str, keywords: list[str],
                   since: datetime, until: datetime) -> list[MessageRecord]:
    app_id = env("LARK_APP_ID")
    app_secret = env("LARK_APP_SECRET")
    if not app_id or not app_secret:
        raise CollectorError("飞书应用凭据未配置（环境变量 LARK_APP_ID / LARK_APP_SECRET）")
    tokens = TokenManager(client, app_id, app_secret)
    chat_name = _fetch_chat_name(client, tokens, chat_id)
    sensitive = _sensitive_keywords()
    records: list[MessageRecord] = []
    for item in _fetch_messages(client, tokens, chat_id, since, until):
        record = _to_record(item, chat_name, keywords, sensitive, since, until)
        if record is not None:
            records.append(record)
    logger.info("飞书消息采集完成",
                extra={"chat_id": chat_id, "count": len(records), "keywords": keywords})
    return records


def _get_client() -> httpx.Client:
    """构建飞书 API 客户端。"""
    return get_lark_client()


def _fetch_chat_name(client: httpx.Client, tokens: TokenManager, chat_id: str) -> str:
    """获取群名称；失败时退化为 chat_id 并记录告警（优雅降级）。"""
    try:
        data = request_with_token(client, tokens, "GET", f"{CHAT_URL}/{chat_id}")
    except CollectorError as exc:
        logger.warning("飞书群信息获取失败，使用 chat_id 作为群名称",
                       extra={"chat_id": chat_id, "reason": str(exc)})
        return chat_id
    payload = data.get("data") if isinstance(data.get("data"), dict) else {}
    return str(payload.get("name") or chat_id)


def _fetch_messages(client: httpx.Client, tokens: TokenManager, chat_id: str,
                    since: datetime, until: datetime) -> list[dict]:
    """分页拉取群消息（start/end_time 为秒级时间戳）。"""
    messages: list[dict] = []
    page_token: str | None = None
    for _ in range(MAX_PAGES):
        params: dict[str, Any] = {
            "container_id_type": "chat",
            "container_id": chat_id,
            "start_time": str(int(to_utc(since).timestamp())),
            "end_time": str(int(to_utc(until).timestamp())),
            "page_size": PAGE_SIZE,
        }
        if page_token:
            params["page_token"] = page_token
        data = request_with_token(client, tokens, "GET", MESSAGE_LIST_URL, params=params)
        payload = data.get("data") if isinstance(data.get("data"), dict) else {}
        messages.extend(item for item in (payload.get("items") or [])
                        if isinstance(item, dict))
        if not payload.get("has_more") or not payload.get("page_token"):
            return messages
        page_token = str(payload["page_token"])
    logger.warning("飞书消息分页达到上限，停止拉取",
                   extra={"chat_id": chat_id, "max_pages": MAX_PAGES})
    return messages


def _to_record(item: dict, chat_name: str, keywords: list[str], sensitive: list[str],
               since: datetime, until: datetime) -> MessageRecord | None:
    """消息 → MessageRecord；非文本、窗口外、命中敏感词或未命中关键词时返回 None。"""
    body = item.get("body") if isinstance(item.get("body"), dict) else {}
    content = _extract_text(body)
    if not content:
        return None  # 非文本消息（图片、文件等）无法参与关键词过滤
    if any(word and word in content for word in sensitive):
        return None  # 敏感词一律剔除（design.md §6.2），不进入日报
    if not any(word and word in content for word in keywords):
        return None
    timestamp = parse_time(item.get("create_time"))
    if timestamp is None:
        logger.warning("飞书消息时间解析失败，跳过",
                       extra={"message_id": item.get("message_id")})
        return None
    if not (to_utc(since) <= timestamp <= to_utc(until)):
        return None
    sender_info = item.get("sender") if isinstance(item.get("sender"), dict) else {}
    return MessageRecord(
        sender=str(sender_info.get("name") or sender_info.get("id") or "unknown"),
        content=content,
        timestamp=timestamp,
        chat_name=chat_name,
    )


def _extract_text(body: dict) -> str:
    """提取消息纯文本：body.content 可能是普通字符串或 JSON 字符串（如 {"text":"..."}）。

    结构化消息（post/image 等）本期仅支持 text 字段提取，无法提取时返回空串。
    """
    raw = body.get("content")
    if not isinstance(raw, str):
        return ""
    stripped = raw.strip()
    if not stripped:
        return ""
    if not stripped.startswith("{"):
        return raw  # 非 JSON 字符串，原样参与过滤
    try:
        payload = json.loads(stripped)
    except ValueError:
        return raw  # 非 JSON 字符串，原样参与过滤
    if isinstance(payload, dict) and isinstance(payload.get("text"), str):
        return payload["text"]
    return ""


def _sensitive_keywords() -> list[str]:
    """读取 config.yaml 的敏感词黑名单；配置缺失/读取失败时使用内置默认值。"""
    try:
        cfg = load_config(CONFIG_PATH)
    except Exception as exc:  # 配置问题不应阻断采集，降级为默认黑名单（绝不放行）
        logger.warning("读取敏感词黑名单配置失败，使用默认值",
                       extra={"path": CONFIG_PATH, "reason": str(exc)})
        return list(DEFAULT_SENSITIVE_KEYWORDS)
    lark_msg = cfg.get("collector", {}).get("lark_msg", {})
    value = lark_msg.get("sensitive_keywords")
    if isinstance(value, list) and value:
        return [str(word) for word in value]
    return list(DEFAULT_SENSITIVE_KEYWORDS)
