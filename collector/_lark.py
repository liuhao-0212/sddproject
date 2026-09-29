"""飞书采集公共能力：Token 管理、带重试的请求、响应解析与时间工具。

供 collector/lark_task.py 与 collector/lark_msg.py 共用，避免重复实现。
"""

from __future__ import annotations

import time
from datetime import datetime, timezone
from typing import Any

import httpx

from shared.errors import CollectorError
from shared.logger import get_logger

logger = get_logger("collector.lark")

BASE_URL = "https://open.feishu.cn/open-apis"
TOKEN_URL = f"{BASE_URL}/auth/v3/tenant_access_token/internal"
DEFAULT_TIMEOUT = 30.0   # 单次请求超时（秒）
PAGE_SIZE = 100
MAX_PAGES = 20           # 分页安全上限
MAX_RETRIES = 3          # 超时后最多重试次数（不含首次尝试）
RETRY_INTERVAL = 5.0     # 超时重试间隔（秒）
# 飞书 tenant/app access token 失效相关错误码
TOKEN_EXPIRED_CODES = {99991661, 99991663, 99991672}

# 模块级别名，便于测试中替换，避免影响全局 time 模块
_sleep = time.sleep


def get_lark_client() -> httpx.Client:
    """构建飞书 API 客户端。"""
    return httpx.Client(timeout=DEFAULT_TIMEOUT)


class TokenManager:
    """tenant_access_token 管理：惰性获取 + 显式刷新（design.md §6.1）。"""

    def __init__(self, client: httpx.Client, app_id: str, app_secret: str):
        self._client = client
        self._app_id = app_id
        self._app_secret = app_secret
        self._token: str | None = None

    def get(self) -> str:
        if self._token is None:
            self._token = self._fetch()
        return self._token

    def refresh(self) -> str:
        """强制重新获取 Token（用于 Token 过期后的刷新）。"""
        self._token = self._fetch()
        return self._token

    def _fetch(self) -> str:
        resp = request_with_retry(
            self._client, "POST", TOKEN_URL,
            json={"app_id": self._app_id, "app_secret": self._app_secret})
        data = safe_json(resp, f"POST {TOKEN_URL}")
        if data.get("code") != 0 or not data.get("tenant_access_token"):
            raise CollectorError(f"飞书 tenant_access_token 获取失败: {data.get('msg', data)}")
        return str(data["tenant_access_token"])


def request_with_token(client: httpx.Client, tokens: TokenManager, method: str,
                       url: str, **kwargs: Any) -> dict:
    """带 Token 的请求：Token 过期时自动刷新后重试 1 次（design.md §6.1）。"""
    data = request_json(client, tokens, method, url, **kwargs)
    if data.get("code") == 0:
        return data
    if data.get("code") in TOKEN_EXPIRED_CODES:
        tokens.refresh()
        data = request_json(client, tokens, method, url, **kwargs)
        if data.get("code") == 0:
            return data
        raise CollectorError(f"飞书接口调用失败（Token 刷新后仍失败）: {method} {url}: "
                             f"code={data.get('code')} msg={data.get('msg', '')}")
    raise CollectorError(f"飞书接口调用失败: {method} {url}: "
                         f"code={data.get('code')} msg={data.get('msg', '')}")


def request_json(client: httpx.Client, tokens: TokenManager, method: str,
                 url: str, **kwargs: Any) -> dict:
    headers = {"Authorization": f"Bearer {tokens.get()}"}
    resp = request_with_retry(client, method, url, headers=headers, **kwargs)
    return safe_json(resp, f"{method} {url}")


def safe_json(resp: httpx.Response, url: str) -> dict:
    """解析响应体为 dict，格式异常时抛出 CollectorError。"""
    try:
        data = resp.json()
    except ValueError as exc:
        raise CollectorError(f"飞书接口返回非 JSON: {url}") from exc
    if not isinstance(data, dict):
        raise CollectorError(f"飞书接口返回格式异常: {url}")
    return data


def request_with_retry(client: httpx.Client, method: str, url: str,
                       **kwargs: Any) -> httpx.Response:
    """带重试的请求：超时/网络错误最多重试 MAX_RETRIES 次（间隔 5s）。"""
    attempt = 0
    while True:
        try:
            resp = client.request(method, url, **kwargs)
        except (httpx.TimeoutException, httpx.HTTPError) as exc:
            if attempt >= MAX_RETRIES:
                raise CollectorError(
                    f"飞书请求失败（已重试 {MAX_RETRIES} 次）: {method} {url}: {exc}") from exc
            attempt += 1
            logger.warning("飞书请求失败，稍后重试",
                           extra={"url": url, "attempt": attempt, "error": str(exc)})
            _sleep(RETRY_INTERVAL)
            continue
        if resp.status_code == 200:
            return resp
        raise CollectorError(f"飞书接口返回错误状态 {resp.status_code}: {method} {url}")


def to_utc(ts: datetime) -> datetime:
    """无时区的时间按 UTC 处理，保证与飞书毫秒时间戳（UTC）可比。"""
    return ts if ts.tzinfo is not None else ts.replace(tzinfo=timezone.utc)


def parse_time(value: Any) -> datetime | None:
    """解析飞书时间：优先毫秒时间戳（int/数字串），兼容 ISO 字符串；失败返回 None。"""
    if value is None or value == "":
        return None
    if isinstance(value, (int, float)) or (isinstance(value, str) and value.strip().isdigit()):
        try:
            return datetime.fromtimestamp(int(value) / 1000, tz=timezone.utc)
        except (OverflowError, OSError, ValueError):
            return None
    try:
        return to_utc(datetime.fromisoformat(str(value)))
    except ValueError:
        return None
