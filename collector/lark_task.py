"""飞书任务采集模块：获取指定项目中时间范围内的任务状态变更记录（design.md §4.1）。

接口契约: collect(project_id, since, until) -> list[TaskRecord]

API 流程:
1. POST /auth/v3/tenant_access_token/internal 获取 tenant_access_token
   （凭据经环境变量 LARK_APP_ID / LARK_APP_SECRET 注入，design.md §6.2）
2. GET /task/v1/tasks 分页拉取项目任务列表（page_token / has_more）
3. GET /task/v1/tasks/{task_id}/activity 获取任务动态，筛选窗口内的状态变更

错误处理（design.md §6.1）:
- 超时/网络错误重试 3 次（间隔 5s）
- Token 过期自动刷新后重试 1 次，仍失败则该次调用失败
- 采集失败返回空列表 + 错误日志；单个任务动态获取失败仅跳过该任务并告警

Token 管理与重试逻辑见 collector/_lark.py（与 lark_msg 共用）。
"""

from __future__ import annotations

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
from shared.config import env
from shared.errors import CollectorError
from shared.logger import get_logger

logger = get_logger("collector.lark_task")

TASK_LIST_URL = f"{BASE_URL}/task/v1/tasks"


@dataclass
class TaskRecord:
    """任务变更记录（design.md §3.1）。"""

    assignee: str          # 负责人（飞书用户名）
    title: str             # 任务标题
    status_from: str       # 原状态
    status_to: str         # 新状态
    updated_at: datetime   # 变更时间


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
    tokens = TokenManager(client, app_id, app_secret)
    records: list[TaskRecord] = []
    for task_id, title, assignee in _fetch_tasks(client, tokens, project_id):
        records.extend(
            _fetch_status_changes(client, tokens, task_id, title, assignee, since, until))
    logger.info("飞书任务采集完成", extra={"project_id": project_id, "count": len(records)})
    return records


def _get_client() -> httpx.Client:
    """构建飞书 API 客户端。"""
    return get_lark_client()


def _fetch_tasks(client: httpx.Client, tokens: TokenManager,
                 project_id: str) -> list[tuple[str, str, str]]:
    """分页拉取项目任务列表，返回 (task_id, title, assignee) 列表。"""
    tasks: list[tuple[str, str, str]] = []
    page_token: str | None = None
    for _ in range(MAX_PAGES):
        params: dict[str, Any] = {"project_id": project_id, "page_size": PAGE_SIZE}
        if page_token:
            params["page_token"] = page_token
        data = request_with_token(client, tokens, "GET", TASK_LIST_URL, params=params)
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


def _fetch_status_changes(client: httpx.Client, tokens: TokenManager, task_id: str,
                          title: str, assignee: str, since: datetime,
                          until: datetime) -> list[TaskRecord]:
    """获取任务动态并筛选窗口内的状态变更记录；动态获取失败仅跳过该任务。"""
    try:
        data = request_with_token(client, tokens, "GET",
                                  f"{TASK_LIST_URL}/{task_id}/activity",
                                  params={"page_size": PAGE_SIZE})
    except CollectorError as exc:
        logger.warning("飞书任务动态获取失败，跳过该任务",
                       extra={"task_id": task_id, "reason": str(exc)})
        return []
    payload = data.get("data") if isinstance(data.get("data"), dict) else {}
    window_start, window_end = to_utc(since), to_utc(until)
    records: list[TaskRecord] = []
    for item in payload.get("items") or []:
        if not isinstance(item, dict):
            continue
        if not item.get("status_from") or not item.get("status_to"):
            continue  # 非状态变更动态（如评论、附件）
        updated_at = parse_time(item.get("updated_at"))
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
