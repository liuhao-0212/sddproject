"""飞书考勤采集模块：获取团队成员的签到、签退与工时数据（design.md §4.1、ADR-003）。

接口契约: collect(since, until) -> CollectResult[AttendanceRecord]

与 v1.0 三个采集器不同，本模块返回 CollectResult 而非裸 list（ADR-003）：
- success=False：接口调用失败（error 携带原因），调用方标注"考勤数据暂不可用"
- success=True + 空 data：接口正常但无数据
二者必须可明确区分，调用方据此决定降级标注。

采集范围（design.md §6.2）：仅采集 config.yaml members 映射表中列出的成员，
members[].lark 作为飞书考勤的员工标识（employee_type=email）。

API 流程（测试中以 httpx.MockTransport 打桩，见 tests/test_attendance.py）:
1. POST /auth/v3/tenant_access_token/internal 获取 tenant_access_token
   （凭据经环境变量 LARK_APP_ID / LARK_APP_SECRET 注入，design.md §6.2）
2. POST /attendance/v1/user_stats_data/query 分页拉取员工日度考勤统计
   请求体: {"employee_type": "email", "employee_ids": [...],
            "begin_date": "yyyyMMdd", "end_date": "yyyyMMdd",
            "page_token": "", "page_size": 100}
   响应:  data.user_stats_data 每项含
             employee_id 员工标识 | date 考勤日期（yyyyMMdd）
             check_in / check_out 毫秒时间戳（缺失时为 null/空）
             status 状态码（normal/late/early_leave/absent/leave）
          分页经 data.has_more / data.page_token 控制

工时计算: work_hours = (check_out - check_in) 小时数，保留两位小数；
签退数据缺失时状态标注为"签退缺失"（Task 11 验收标准）。

错误处理（design.md §6.1）:
- 超时/网络错误重试 3 次（间隔 5s）
- Token 过期自动刷新后重试 1 次
- 任何失败返回 CollectResult(success=False, error=原因)，不抛出异常

Token 管理与重试逻辑见 collector/_lark.py（与 lark_task / lark_msg 共用）。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date, datetime
from typing import Any, Generic, TypeVar

import httpx

from collector._lark import (
    BASE_URL,
    MAX_PAGES,
    PAGE_SIZE,
    TokenManager,
    get_lark_client,
    parse_time,
    request_with_token,
)
from shared.config import env, load_config
from shared.errors import CollectorError, ConfigError
from shared.logger import get_logger

logger = get_logger("collector.lark_attendance")

ATTENDANCE_URL = f"{BASE_URL}/attendance/v1/user_stats_data/query"
# 成员标识类型：config.yaml members[].lark 存放的是飞书用户邮箱（design.md §3.3）
EMPLOYEE_TYPE = "email"

# 飞书考勤状态码 → 展示名（design.md §3.1：正常 / 迟到 / 早退 / 缺勤 / 休假）
STATUS_MAP = {
    "normal": "正常",
    "late": "迟到",
    "early_leave": "早退",
    "absent": "缺勤",
    "leave": "休假",
}
# 签退数据缺失时的状态标注（Task 11 验收标准）
MISSING_CHECK_OUT_STATUS = "签退缺失"

T = TypeVar("T")


@dataclass
class CollectResult(Generic[T]):
    """采集结果（ADR-003）：success 区分"接口调用失败"与"接口成功但无数据"。"""

    success: bool                             # 调用是否成功
    data: list[T] = field(default_factory=list)  # 成功时的数据（失败时为空列表）
    error: str | None = None                  # 失败原因（成功时为 None）


@dataclass
class AttendanceRecord:
    """考勤记录（design.md §3.1）。"""

    employee_id: str              # 飞书用户 ID（本项目中为 members[].lark）
    date: date                    # 考勤日期
    check_in: datetime | None     # 签到时间
    check_out: datetime | None    # 签退时间
    work_hours: float             # 工时（小时）
    status: str                   # 正常 / 迟到 / 早退 / 缺勤 / 休假 / 签退缺失


def collect(since: date, until: date) -> CollectResult[AttendanceRecord]:
    """获取团队在 [since, until]（含两端）的考勤记录（design.md §4.1）。

    任何采集失败都不抛出异常：返回 CollectResult(success=False, error=原因)，
    由编排层在日报中标注"考勤数据暂不可用"（design.md §6.1 优雅降级）。
    """
    try:
        return _collect_inner(since, until)
    except (CollectorError, ConfigError) as exc:
        logger.error("飞书考勤数据源采集失败", extra={"reason": str(exc)}, exc_info=exc)
        return CollectResult(success=False, data=[], error=str(exc))
    except Exception as exc:  # 兜底：任何失败不得逃逸出 collect()（design.md §6.1 原则 1）
        logger.error("飞书考勤采集发生未预期异常", extra={"reason": str(exc)}, exc_info=exc)
        return CollectResult(success=False, data=[], error=str(exc))


def _collect_inner(since: date, until: date) -> CollectResult[AttendanceRecord]:
    app_id = env("LARK_APP_ID")
    app_secret = env("LARK_APP_SECRET")
    if not app_id or not app_secret:
        raise CollectorError("飞书应用凭据未配置（环境变量 LARK_APP_ID / LARK_APP_SECRET）")
    cfg = load_config()
    user_ids = [str(member.get("lark") or "") for member in cfg.get("members", [])
                if str(member.get("lark") or "")]
    if not user_ids:
        raise CollectorError("config.yaml members 映射表缺少飞书用户标识（members[].lark）")
    client = _get_client()
    try:
        tokens = TokenManager(client, app_id, app_secret)
        records = _fetch_stats(client, tokens, user_ids, since, until)
    finally:
        client.close()
    logger.info("飞书考勤采集完成", extra={"count": len(records)})
    return CollectResult(success=True, data=records, error=None)


def _get_client() -> httpx.Client:
    """构建飞书 API 客户端。"""
    return get_lark_client()


def _fetch_stats(client: httpx.Client, tokens: TokenManager, user_ids: list[str],
                 since: date, until: date) -> list[AttendanceRecord]:
    """分页拉取员工日度考勤统计并解析为 AttendanceRecord 列表。"""
    records: list[AttendanceRecord] = []
    page_token: str | None = None
    for _ in range(MAX_PAGES):
        body: dict[str, Any] = {
            "employee_type": EMPLOYEE_TYPE,
            "employee_ids": user_ids,
            "begin_date": since.strftime("%Y%m%d"),
            "end_date": until.strftime("%Y%m%d"),
            "page_size": PAGE_SIZE,
        }
        if page_token:
            body["page_token"] = page_token
        data = request_with_token(client, tokens, "POST", ATTENDANCE_URL, json=body)
        payload = data.get("data") if isinstance(data.get("data"), dict) else {}
        for item in payload.get("user_stats_data") or []:
            record = _parse_item(item)
            if record is not None:
                records.append(record)
        if not payload.get("has_more") or not payload.get("page_token"):
            return records
        page_token = str(payload["page_token"])
    logger.warning("飞书考勤分页达到上限，停止拉取", extra={"max_pages": MAX_PAGES})
    return records


def _parse_item(item: Any) -> AttendanceRecord | None:
    """解析单条日度考勤统计；字段缺失/解析失败返回 None（记录告警，不静默丢弃）。"""
    if not isinstance(item, dict):
        logger.warning("飞书考勤记录格式异常，跳过", extra={"item": item})
        return None
    employee_id = str(item.get("employee_id") or "")
    if not employee_id:
        logger.warning("飞书考勤记录缺少 employee_id，跳过")
        return None
    record_date = _parse_date(item.get("date"))
    if record_date is None:
        logger.warning("飞书考勤日期解析失败，跳过", extra={"employee_id": employee_id})
        return None
    check_in = parse_time(item.get("check_in"))
    check_out = parse_time(item.get("check_out"))
    work_hours = 0.0
    if check_in is not None and check_out is not None:
        work_hours = round((check_out - check_in).total_seconds() / 3600, 2)
    raw_status = str(item.get("status") or "")
    status = STATUS_MAP.get(raw_status)
    if status is None:
        status = raw_status or STATUS_MAP["normal"]
        logger.warning("飞书考勤状态未知，按原值保留",
                       extra={"employee_id": employee_id, "status": status})
    if check_in is not None and check_out is None:
        status = MISSING_CHECK_OUT_STATUS  # 签退缺失（Task 11 验收标准）
    return AttendanceRecord(employee_id=employee_id, date=record_date,
                            check_in=check_in, check_out=check_out,
                            work_hours=work_hours, status=status)


def _parse_date(value: Any) -> date | None:
    """解析考勤日期：支持 date、yyyyMMdd / yyyy-MM-dd；失败返回 None。"""
    if value is None or value == "":
        return None
    if isinstance(value, datetime):
        return value.date()
    if isinstance(value, date):
        return value
    text = str(value).strip()
    for fmt in ("%Y%m%d", "%Y-%m-%d"):
        try:
            return datetime.strptime(text, fmt).date()
        except ValueError:
            continue
    return None
