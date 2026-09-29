"""飞书机器人推送模块：通过 webhook 将 Markdown 日报推送至指定群（design.md §4.3）。

接口契约: send(report, chat_id) -> bool

- 发送内容为 report.markdown，以 interactive 卡片的 markdown 元素渲染
- webhook 地址读 config.yaml 的 notifier.lark_bot.webhook_url，
  为空时回退环境变量 LARK_BOT_WEBHOOK_URL（design.md §6.2）
- 发送失败重试 2 次，仍失败返回 False + 错误日志（design.md §6.1；
  跨渠道告警由编排层 main.py 负责）
- chat_id 用于日志审计（webhook 本身已绑定目标群）
"""

from __future__ import annotations

import time
from typing import Any

import httpx

from generator import DailyReport
from shared.config import env, load_config
from shared.errors import NotifierError
from shared.logger import get_logger

logger = get_logger("notifier.lark_bot")

CONFIG_PATH = "config.yaml"
SEND_TIMEOUT = 30.0    # 单次请求超时（秒）
MAX_RETRIES = 2        # 发送失败最多重试次数（不含首次尝试）
RETRY_INTERVAL = 5.0   # 重试间隔（秒）

# 模块级别名，便于测试中替换，避免影响全局 time 模块
_sleep = time.sleep


def send(report: DailyReport, chat_id: str) -> bool:
    """将 Markdown 格式日报通过飞书机器人 webhook 发送到指定群。

    成功返回 True；发送失败重试 2 次，仍失败记录错误日志并返回 False。
    """
    if not chat_id:
        logger.error("飞书推送失败：chat_id 为空")
        return False
    try:
        webhook_url = _webhook_url()
    except Exception as exc:  # 配置不可用同样降级为推送失败（§6.1）
        logger.error("飞书推送失败：webhook 配置不可用",
                     extra={"path": CONFIG_PATH, "reason": str(exc)}, exc_info=exc)
        return False
    payload = _build_payload(report)

    attempt = 0
    while True:
        try:
            _send_once(webhook_url, payload)
            break
        except (httpx.TimeoutException, httpx.HTTPError, NotifierError) as exc:
            if attempt >= MAX_RETRIES:
                logger.error("飞书推送失败",
                             extra={"chat_id": chat_id, "reason": str(exc)}, exc_info=exc)
                return False
            attempt += 1
            logger.warning("飞书消息发送失败，稍后重试",
                           extra={"chat_id": chat_id, "attempt": attempt,
                                  "error": str(exc)})
            _sleep(RETRY_INTERVAL)
    logger.info("飞书推送成功", extra={"chat_id": chat_id,
                                       "team": report.team_name,
                                       "date": report.date.isoformat()})
    return True


def _webhook_url() -> str:
    """webhook 地址：优先 config.yaml，为空时回退环境变量（design.md §6.2）。"""
    cfg = load_config(CONFIG_PATH)
    lark_bot_cfg = cfg.get("notifier", {}).get("lark_bot") or {}
    url = str(lark_bot_cfg.get("webhook_url") or "")
    if not url:
        url = env("LARK_BOT_WEBHOOK_URL")
    if not url:
        raise NotifierError("notifier.lark_bot.webhook_url 为空，"
                            "且环境变量 LARK_BOT_WEBHOOK_URL 未设置")
    return url


def _build_payload(report: DailyReport) -> dict[str, Any]:
    """构造 interactive 卡片消息，以 markdown 元素渲染日报正文。"""
    return {
        "msg_type": "interactive",
        "card": {
            "header": {
                "title": {"tag": "plain_text",
                          "content": f"{report.team_name} 日报（{report.date.isoformat()}）"},
                "template": "blue",
            },
            "elements": [{"tag": "markdown", "content": report.markdown}],
        },
    }


def _send_once(webhook_url: str, payload: dict[str, Any]) -> None:
    """完成一次 webhook 推送；失败抛出交由重试层处理。"""
    client = _get_client()
    try:
        resp = client.post(webhook_url, json=payload)
    finally:
        client.close()
    if resp.status_code != 200:
        raise NotifierError(f"webhook 返回错误状态 {resp.status_code}")
    try:
        data = resp.json()
    except ValueError as exc:
        raise NotifierError(f"webhook 返回非 JSON: {webhook_url}") from exc
    # 兼容自定义机器人（StatusCode/StatusMessage）与新版机器人（code/msg）两种响应格式
    if isinstance(data, dict) and (data.get("code") == 0 or data.get("StatusCode") == 0):
        return
    if isinstance(data, dict):
        msg = data.get("msg") or data.get("StatusMessage")
    else:
        msg = data
    raise NotifierError(f"webhook 返回业务错误: {msg}")


def _get_client() -> httpx.Client:
    """构建 webhook HTTP 客户端。"""
    return httpx.Client(timeout=SEND_TIMEOUT)
