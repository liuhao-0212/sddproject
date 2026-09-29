"""飞书任务采集模块：获取指定项目中时间范围内的任务状态变更记录（design.md §4.1）。

接口契约: collect(project_id, since, until) -> list[TaskRecord]

API 流程:
1. POST /auth/v3/tenant_access_token/internal 获取 tenant_access_token
   （凭据经环境变量 LARK_APP_ID / LARK_APP_SECRET 注入，design.md §6.2）
2. GET /task/v1/tasks 分页拉取项目任务列表（page_token / has_more）
3. GET /task/v1/tasks/{task_id}/activity 获取任务动态，筛选窗口内的状态变更

错误处理（design.md §6.1）:
- 超时/网络错误重试 3 次（间隔 5s）
- 飞书返回 Token 失效错误码（99991661/99991663/99991672）时自动刷新
  Token 后重试 1 次，仍失败则该次调用失败
- 采集失败返回空列表 + 错误日志；单个任务动态获取失败仅跳过该任务并告警
"""

from __future__ import annotations

import time
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any

import httpx

from shared.config import env
from shared.errors import CollectorError
from shared.logger import get_logger

logger = get_logger("collector.lark_task")

BASE_URL = "https://open.feishu.cn/open-apis"
TOKEN_URL = f"{BASE_URL}/auth/v3/tenant_access_token/internal"
TASK_LIST_URL = f"{BASE_URL}/task/v1/tasks"
DEFAULT_TIMEOUT = 30.0   # 单次请求超时（秒）
PAGE_SIZE = 100
MAX_PAGES = 20           # 分页安全上限
MAX_RETRIES = 3          # 超时后最多重试次数（不含首次尝试）
RETRY_INTERVAL = 5.0     # 超时重试间隔（秒）
# 飞书 tenant/app access token 失效相关错误码
TOKEN_EXPIRED_CODES = {99991661, 99991663, 99991672}

# 模块级别名，便于测试中替换，避免影响全局 time 模块
_sleep = time.sleep


@dataclass
class TaskRecord:
    """任务变更记录（design.md §3.1）。"""

    assignee: str          # 负责人（飞书用户名）
    title: str             # 任务标题
    status_from: str       # 原状态
    status_to: str         # 新状态
    updated_at: datetime   # 变更时间


class _TokenManager:
    """tenant_access_token 管理：惰性获取 + 显式刷新。"""

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
        """强制重新获取 Token（用于 Token 过期后的刷新，design.md §6.1）。"""
        self._token = self._fetch()
        return self._token

    def _fetch(self) -> str:
        resp = _request_with_retry(
            self._client, "POST", TOKEN_URL,
            json={"app_id": self._app_id, "app_secret": self._app_secret})
        data = _safe_json(resp, f"POST {TOKEN_URL}")
        if data.get("code") != 0 or not data.get("tenant_access_token"):
            raise CollectorError(f"飞书 tenant_access_token 获取失败: {data.get('msg', data)}")
        return str(data["tenant_access_token"])


def collect(project_id: str, since: datetime, until: datetime) -> list[TaskRecord]:
    """获取飞书项目在 [since, until] 内的任务状态变更记录。

    采集失败返回空列表并记录错误日志（由生成层在日报中标注"数据获取失败"）。
    """
    client = _get_client()
    try:
        try:
            return _collect_inner(client, project_id, since, until)
        except CollectorError as exc:
            logger.error("飞书任务数据源采集失败，返回空列表",
                         extra={"project_id": project_id, "reason": str(exc)}, exc_info=exc)
            return []
    finally:
        client.close()


def _collect_inner(client: httpx.Client, project_id: str, since: datetime,
                   until: datetime) -> list[TaskRecord]:
    app_id = env("LARK_APP_ID")
    app_secret = env("LARK_APP_SECRET")
    if not app_id or not app_secret:
        raise CollectorError("飞书应用凭据未配置（环境变量 LARK_APP_ID / LARK_APP_SECRET）")
    tokens = _TokenManager(client, app_id, app_secret)
    records: list[TaskRecord] = []
    for task_id, title, assignee in _fetch_tasks(client, tokens, project_id):
        records.extend(
            _fetch_status_changes(client, tokens, task_id, title, assignee, since, until))
    logger.info("飞书任务采集完成", extra={"project_id": project_id, "count": len(records)})
    return records


def _get_client() -> httpx.Client:
    """构建飞书 API 客户端。"""
    return httpx.Client(timeout=DEFAULT_TIMEOUT)


def _fetch_tasks(client: httpx.Client, tokens: _TokenManager,
                 project_id: str) -> list[tuple[str, str, str]]:
    """分页拉取项目任务列表，返回 (task_id, title, assignee) 列表。"""
    tasks: list[tuple[str, str, str]] = []
    page_token: str | None = None
    for _ in range(MAX_PAGES):
        params: dict[str, Any] = {"project_id": project_id, "page_size": PAGE_SIZE}
        if page_token:
            params["page_token"] = page_token
        data = _request_with_token(client, tokens, "GET", TASK_LIST_URL, params=params)
        payload = data.get("data") if isinstance(data.get("data"), dict) else {}
        for item in payload.get("items") or []:
            if not isinstance(item, dict):
                continue
            task_id = item.get("id")
            if not task_id:
                logger.warning("飞书任务缺少 id，跳过")
                continue
            assignee = item.get("assignee") if isinstance(item.get("assignee"), dict) else {}
            tasks.append((str(task_id), str(item.get("summary") or ""),
                          str(assignee.get("name") or "")))
        if not payload.get("has_more") or not payload.get("page_token"):
            return tasks
        page_token = str(payload["page_token"])
    logger.warning("飞书任务分页达到上限，停止拉取",
                   extra={"project_id": project_id, "max_pages": MAX_PAGES})
    return tasks


def _fetch_status_changes(client: httpx.Client, tokens: _TokenManager, task_id: str,
                          title: str, assignee: str, since: datetime,
                          until: datetime) -> list[TaskRecord]:
    """获取任务动态并筛选窗口内的状态变更记录；动态获取失败仅跳过该任务。"""
    try:
        data = _request_with_token(client, tokens, "GET",
                                   f"{TASK_LIST_URL}/{task_id}/activity",
                                   params={"page_size": PAGE_SIZE})
    except CollectorError as exc:
        logger.warning("飞书任务动态获取失败，跳过该任务",
                       extra={"task_id": task_id, "reason": str(exc)})
        return []
    payload = data.get("data") if isinstance(data.get("data"), dict) else {}
    window_start, window_end = _to_utc(since), _to_utc(until)
    records: list[TaskRecord] = []
    for item in payload.get("items") or []:
        if not isinstance(item, dict):
            continue
        if not item.get("status_from") or not item.get("status_to"):
            continue  # 非状态变更动态（如评论、附件）
        updated_at = _parse_time(item.get("updated_at"))
        if updated_at is None:
            logger.warning("飞书任务动态时间解析失败，跳过", extra={"task_id": task_id})
            continue
        if not (window_start <= updated_at <= window_end):
            continue
        actor = item.get("assignee") if isinstance(item.get("assignee"), dict) else {}
        records.append(TaskRecord(
            assignee=str(actor.get("name") or assignee or "unknown"),
            title=title,
            status_from=str(item["status_from"]),
            status_to=str(item["status_to"]),
            updated_at=updated_at,
        ))
    return records


def _request_with_token(client: httpx.Client, tokens: _TokenManager, method: str,
                        url: str, **kwargs: Any) -> dict:
    """带 Token 的请求：Token 过期时自动刷新后重试 1 次（design.md §6.1）。"""
    data = _request_json(client, tokens, method, url, **kwargs)
    if data.get("code") == 0:
        return data
    if data.get("code") in TOKEN_EXPIRED_CODES:
        tokens.refresh()
        data = _request_json(client, tokens, method, url, **kwargs)
        if data.get("code") == 0:
            return data
        raise CollectorError(f"飞书接口调用失败（Token 刷新后仍失败）: {method} {url}: "
                             f"code={data.get('code')} msg={data.get('msg', '')}")
    raise CollectorError(f"飞书接口调用失败: {method} {url}: "
                         f"code={data.get('code')} msg={data.get('msg', '')}")


def _request_json(client: httpx.Client, tokens: _TokenManager, method: str,
                  url: str, **kwargs: Any) -> dict:
    headers = {"Authorization": f"Bearer {tokens.get()}"}
    resp = _request_with_retry(client, method, url, headers=headers, **kwargs)
    return _safe_json(resp, f"{method} {url}")


def _safe_json(resp: httpx.Response, url: str) -> dict:
    """解析响应体为 dict，格式异常时抛出 CollectorError。"""
    try:
        data = resp.json()
    except ValueError as exc:
        raise CollectorError(f"飞书接口返回非 JSON: {url}") from exc
    if not isinstance(data, dict):
        raise CollectorError(f"飞书接口返回格式异常: {url}")
    return data


def _request_with_retry(client: httpx.Client, method: str, url: str,
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


def _to_utc(ts: datetime) -> datetime:
    """无时区的时间按 UTC 处理，保证与飞书毫秒时间戳（UTC）可比。"""
    return ts if ts.tzinfo is not None else ts.replace(tzinfo=timezone.utc)


def _parse_time(value: Any) -> datetime | None:
    """解析飞书时间：优先毫秒时间戳（int/数字串），兼容 ISO 字符串；失败返回 None。"""
    if value is None or value == "":
        return None
    if isinstance(value, (int, float)) or (isinstance(value, str) and value.strip().isdigit()):
        try:
            return datetime.fromtimestamp(int(value) / 1000, tz=timezone.utc)
        except (OverflowError, OSError, ValueError):
            return None
    try:
        return _to_utc(datetime.fromisoformat(str(value)))
    except ValueError:
        return None
