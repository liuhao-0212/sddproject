"""飞书考勤采集模块：获取团队成员的签到、签退与工时数据（design.md §4.1、§6.9）。

接口契约: collect(since, until) -> CollectResult[AttendanceRecord]

与 v1.0 三个采集器不同，本模块返回 CollectResult 而非裸 list（ADR-003）：
- success=False：接口调用失败（error 携带原因），调用方记日志并以可读说明标注
- success=True + 空 data：接口正常但无数据（含该账号无打卡记录）
二者必须可明确区分，调用方据此决定降级处理。

采集范围（design.md §6.2）：仅采集 config.yaml members 映射表中列出的成员。
员工标识（design.md §6.9，v1.13 实测校准）：
- members[].attendance 显式配置的 employee_id 优先
- 否则用 members[].lark（open_id）调通讯录接口：
  data.user.employee_id → 缺失时回退 data.user.user_id
- 无法解析 → 按未接入处理；全部无法解析 → CollectResult(success=False)

API 流程（测试中以 httpx.MockTransport 打桩，见 tests/test_attendance.py）:
1. POST /auth/v3/tenant_access_token/internal 获取 tenant_access_token
   （凭据经环境变量 LARK_APP_ID / LARK_APP_SECRET 注入，design.md §6.2）
2. GET /contact/v3/users/{open_id}?user_id_type=open_id（仅在需要解析时）
3. POST /attendance/v1/user_tasks/query?employee_type=employee_id
   请求体: {"user_ids": ["<employee_id>", ...],
            "check_date_from": 20260929, "check_date_to": 20260929}
   响应: data.user_task_results[]，每项含 day / user_id / records[]
         每条 record 含 check_in_record.check_time / check_out_record.check_time /
         check_in_result / check_out_result
   同一天多条 record 合并：最早签到 + 最晚签退，状态取第一条非「正常」
   解析不出记录时记录原始响应（截断 2000 字符），便于排查契约差异

工时计算: work_hours = (check_out - check_in) 小时数，保留一位小数；
缺签到或签退时为 0；缺签退时状态标注为「签退缺失」。

错误处理（design.md §6.1）:
- 超时/网络错误重试 3 次（间隔 5s）
- Token 过期自动刷新后重试 1 次
- 任何失败返回 CollectResult(success=False, error=原因)，不抛出异常

Token 管理与重试逻辑见 collector/_lark.py（与 lark_task / lark_msg 共用）。
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from datetime import date, datetime, timezone
from typing import Any, Generic, TypeVar
from urllib.parse import quote

import httpx

from collector._lark import (
    BASE_URL,
    TokenManager,
    get_lark_client,
    request_with_token,
)
from shared.config import env, load_config
from shared.errors import CollectorError, ConfigError
from shared.logger import get_logger

logger = get_logger("collector.lark_attendance")

ATTENDANCE_URL = f"{BASE_URL}/attendance/v1/user_tasks/query"
# v1.13：employee_type 经 URL 查询参数传入，员工标识使用 employee_id（design.md §6.9）
EMPLOYEE_TYPE = "employee_id"
# 原始响应日志截断长度（design.md §6.9）
RAW_PAYLOAD_LIMIT = 2000

NORMAL_STATUS = "正常"
MISSING_CHECK_OUT_STATUS = "签退缺失"
UNKNOWN_STATUS = "未知"

# 最近一次采集的「成员配置标识（attendance / lark）→ 查询用员工 ID」映射，
# 供编排层把考勤记录归属到成员（通讯录回退 user_id 时两者不同）
_last_employee_ids: dict[str, str] = {}


def get_last_employee_ids() -> dict[str, str]:
    """返回最近一次采集的成员标识 → 员工 ID 映射（v1.13）。"""
    return dict(_last_employee_ids)


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

    employee_id: str              # 飞书员工 ID（employee_id / user_id）
    date: date                    # 考勤日期
    check_in: datetime | None     # 签到时间
    check_out: datetime | None    # 签退时间
    work_hours: float             # 工时（小时）
    status: str                   # 正常 / 迟到 / 早退 / 签退缺失 / 未知


def collect(since: date, until: date) -> CollectResult[AttendanceRecord]:
    """获取团队在 [since, until]（含两端）的考勤记录（design.md §4.1）。

    任何采集失败都不抛出异常：返回 CollectResult(success=False, error=原因)，
    由编排层记日志并在日报中以可读说明标注（design.md §6.1 优雅降级）。
    """
    global _last_employee_ids
    _last_employee_ids = {}
    try:
        return _collect_inner(since, until)
    except (CollectorError, ConfigError) as exc:
        reason = str(exc)
        # 404 表示路径错误或该租户没有开通「考勤」模块（v1.13 文案，design.md §6.9）
        if "404" in reason:
            reason = "飞书考勤接口 404（路径错误或考勤模块未开通）"
        logger.error("飞书考勤数据源采集失败", extra={"reason": reason}, exc_info=exc)
        return CollectResult(success=False, data=[], error=reason)
    except Exception as exc:  # 兜底：任何失败不得逃逸出 collect()（design.md §6.1 原则 1）
        logger.error("飞书考勤采集发生未预期异常", extra={"reason": str(exc)}, exc_info=exc)
        return CollectResult(success=False, data=[], error=str(exc))


def _collect_inner(since: date, until: date) -> CollectResult[AttendanceRecord]:
    app_id = env("LARK_APP_ID")
    app_secret = env("LARK_APP_SECRET")
    if not app_id or not app_secret:
        raise CollectorError("飞书应用凭据未配置（环境变量 LARK_APP_ID / LARK_APP_SECRET）")
    cfg = load_config()
    client = _get_client()
    try:
        tokens = TokenManager(client, app_id, app_secret)
        employee_ids = _resolve_employee_ids(cfg, client, tokens)
        records = _fetch_stats(client, tokens, employee_ids, since, until)
    finally:
        client.close()
    logger.info("飞书考勤采集完成", extra={"count": len(records)})
    return CollectResult(success=True, data=records, error=None)


def _get_client() -> httpx.Client:
    """构建飞书 API 客户端。"""
    return get_lark_client()


def _resolve_employee_ids(cfg: dict[str, Any], client: httpx.Client,
                          tokens: TokenManager) -> list[str]:
    """解析成员对应的员工 ID（design.md §6.9）。

    config.attendance 优先；否则用 lark（open_id）经通讯录接口解析。
    单个成员失败只告警并跳过；全部失败抛 CollectorError（按未接入处理）。
    """
    global _last_employee_ids
    employee_ids: list[str] = []
    mapping: dict[str, str] = {}
    for member in cfg.get("members", []):
        if not isinstance(member, dict):
            continue
        explicit = str(member.get("attendance") or "").strip()
        if explicit:
            employee_ids.append(explicit)
            mapping[explicit] = explicit
            continue
        open_id = str(member.get("lark") or "").strip()
        if not open_id:
            logger.warning("成员缺少 attendance 与 lark 标识，跳过",
                           extra={"member": member.get("name")})
            continue
        employee_id = _resolve_via_contact(client, tokens, open_id)
        if employee_id:
            employee_ids.append(employee_id)
            mapping[open_id] = employee_id
        else:
            logger.warning("无法解析飞书员工 ID，跳过该成员",
                           extra={"member": member.get("name"), "lark": open_id})
    _last_employee_ids = mapping
    if not employee_ids:
        raise CollectorError("缺少飞书员工 ID")
    # 去重并保持配置顺序，避免重复员工产生重复查询与记录
    return list(dict.fromkeys(employee_ids))


def _resolve_via_contact(client: httpx.Client, tokens: TokenManager,
                         open_id: str) -> str:
    """用通讯录接口把 open_id 解析为员工 ID；失败返回空串（不中断整体采集）。

    v1.13 实测：部分租户的通讯录响应没有 employee_id，此时 user_id
    （管理后台的「用户ID」）可直接作为考勤 employee_id 使用。
    """
    url = f"{BASE_URL}/contact/v3/users/{quote(open_id, safe='')}"
    try:
        data = request_with_token(client, tokens, "GET", url,
                                  params={"user_id_type": "open_id"})
    except CollectorError as exc:
        logger.warning("通讯录解析 employee_id 失败",
                       extra={"lark": open_id, "reason": str(exc)})
        return ""
    payload = data.get("data") if isinstance(data.get("data"), dict) else {}
    user = payload.get("user") if isinstance(payload.get("user"), dict) else {}
    employee_id = str(user.get("employee_id") or user.get("user_id") or "").strip()
    if not employee_id:
        logger.warning("通讯录响应缺少 employee_id 与 user_id", extra={"lark": open_id})
    return employee_id


def _fetch_stats(client: httpx.Client, tokens: TokenManager, employee_ids: list[str],
                 since: date, until: date) -> list[AttendanceRecord]:
    """拉取员工打卡流水并解析为 AttendanceRecord 列表（单次请求，v1.13 契约）。"""
    params = {"employee_type": EMPLOYEE_TYPE}
    body: dict[str, Any] = {
        "user_ids": employee_ids,
        "check_date_from": int(since.strftime("%Y%m%d")),
        "check_date_to": int(until.strftime("%Y%m%d")),
    }
    data = request_with_token(client, tokens, "POST", ATTENDANCE_URL,
                              params=params, json=body)
    payload = data.get("data") if isinstance(data.get("data"), dict) else {}
    items = payload.get("user_task_results")
    if not isinstance(items, list):
        items = []
    records: list[AttendanceRecord] = []
    for task in items:
        records.extend(_parse_user_task(task))
    if not records:
        # 解析不出记录时记录真实响应（截断 2000 字符）
        logger.info("考勤原始响应", extra={
            "payload": json.dumps(data, ensure_ascii=False, default=str)[:RAW_PAYLOAD_LIMIT],
        })
    return records


def _parse_user_task(task: Any) -> list[AttendanceRecord]:
    """解析一项 user_task（某员工某天的打卡记录）；同一天多条合并为一条。"""
    if not isinstance(task, dict):
        logger.warning("飞书考勤 user_task 格式异常，跳过", extra={"task": task})
        return []
    employee_id = str(task.get("user_id") or "")
    if not employee_id:
        logger.warning("飞书考勤记录缺少 user_id，跳过")
        return []
    record_date = _parse_date(task.get("day"))
    if record_date is None:
        logger.warning("飞书考勤日期解析失败，跳过", extra={"employee_id": employee_id})
        return []
    raw_records = task.get("records")
    if not isinstance(raw_records, list):
        logger.warning("飞书考勤 records 格式异常，跳过",
                       extra={"employee_id": employee_id, "day": task.get("day")})
        return []
    parsed = [record for record in
              (_parse_record(raw, employee_id, record_date) for raw in raw_records)
              if record is not None]
    if not parsed:
        return []
    check_ins = [record.check_in for record in parsed if record.check_in is not None]
    check_outs = [record.check_out for record in parsed if record.check_out is not None]
    check_in = min(check_ins) if check_ins else None
    check_out = max(check_outs) if check_outs else None
    # 同一天多条记录：状态取第一条非「正常」
    status = next((record.status for record in parsed
                   if record.status != NORMAL_STATUS), NORMAL_STATUS)
    return [AttendanceRecord(
        employee_id=employee_id,
        date=record_date,
        check_in=check_in,
        check_out=check_out,
        work_hours=_work_hours(check_in, check_out),
        status=status,
    )]


def _parse_record(raw: Any, employee_id: str, record_date: date) -> AttendanceRecord | None:
    """解析单条打卡流水。"""
    if not isinstance(raw, dict):
        logger.warning("飞书考勤打卡记录格式异常，跳过",
                       extra={"employee_id": employee_id})
        return None
    check_in_record = raw.get("check_in_record")
    check_out_record = raw.get("check_out_record")
    check_in_value = check_in_record.get("check_time") if isinstance(check_in_record, dict) else None
    check_out_value = check_out_record.get("check_time") if isinstance(check_out_record, dict) else None
    check_in = _parse_check_time(check_in_value)
    check_out = _parse_check_time(check_out_value)
    if check_in_value not in (None, "") and check_in is None:
        logger.warning("考勤签到时间解析失败",
                       extra={"employee_id": employee_id,
                              "value": str(check_in_value)[:100]})
    if check_out_value not in (None, "") and check_out is None:
        logger.warning("考勤签退时间解析失败",
                       extra={"employee_id": employee_id,
                              "value": str(check_out_value)[:100]})
    status = _record_status(
        check_in, check_out,
        raw.get("check_in_result"), raw.get("check_out_result"))
    return AttendanceRecord(employee_id=employee_id, date=record_date,
                            check_in=check_in, check_out=check_out,
                            work_hours=_work_hours(check_in, check_out),
                            status=status)


def _record_status(check_in: datetime | None, check_out: datetime | None,
                   check_in_result: Any, check_out_result: Any) -> str:
    """按打卡结果判定状态（design.md §6.9）。"""
    if check_in is not None and check_out is None:
        return MISSING_CHECK_OUT_STATUS
    in_text = str(check_in_result or "").strip().lower()
    out_text = str(check_out_result or "").strip().lower()
    if in_text == "normal" and out_text == "normal":
        return NORMAL_STATUS
    combined = f"{in_text} {out_text}"
    if "late" in combined:
        return "迟到"
    if "early" in combined:
        return "早退"
    return UNKNOWN_STATUS


def _work_hours(check_in: datetime | None, check_out: datetime | None) -> float:
    """工时 = 签退 - 签到（小时，保留一位小数）；缺任一时间返回 0。"""
    if check_in is None or check_out is None:
        return 0.0
    return round((check_out - check_in).total_seconds() / 3600, 1)


def _parse_check_time(value: Any) -> datetime | None:
    """解析 check_time：epoch 秒 / epoch 毫秒 / yyyy-MM-dd HH:mm:ss / ISO。"""
    if value is None or value == "":
        return None
    if isinstance(value, (int, float)):
        return _from_epoch(value)
    text = str(value).strip()
    if not text:
        return None
    try:
        numeric = float(text)
    except ValueError:
        numeric = None
    if numeric is not None:
        return _from_epoch(numeric)
    try:
        parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError:
        try:
            parsed = datetime.strptime(text, "%Y-%m-%d %H:%M:%S")
        except ValueError:
            return None
    return parsed if parsed.tzinfo is not None else parsed.replace(tzinfo=timezone.utc)


def _from_epoch(value: float) -> datetime | None:
    """把 epoch 秒/毫秒转为 UTC datetime（>= 1e11 视为毫秒）。"""
    seconds = value / 1000 if abs(value) >= 1e11 else value
    try:
        return datetime.fromtimestamp(seconds, tz=timezone.utc)
    except (OverflowError, OSError, ValueError):
        return None


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
