"""主编排入口：采集 → 聚合 → 生成 → 推送（design.md §2）。

用法:
    python main.py            # 完整流程：采集 + 聚合 + 生成 + 推送 + 落库
    python main.py --check    # 仅验证配置与 API 连接（design.md §6.3 健康检查）
    python main.py --dry-run  # 采集 + 生成，不推送、不落库
    python main.py --history [--days N]   # 只读：列出最近 N 天日报（v1.7）
    python main.py --show DATE            # 只读：打印指定日期日报 Markdown（v1.7）
    python main.py --export DATE          # 只读：导出指定日期日报到 output/（v1.7）

错误处理（design.md §6.1）:
- 单个数据源失败不阻断其他数据源；github/lark_task/lark_msg 经 get_last_error()
  探测（区分“失败”与“为空”），考勤经 CollectResult.success 探测（ADR-003），
  失败原因填入 MemberReport.source_errors 并记日志，生成层据此在日报中
  显示可读说明（v1.4 修订，design.md §6.5；原始异常只进日志）
- 所有数据源失败时不生成空日报，记录错误日志并发送告警邮件
- 推送渠道互为备份告警通道：邮件失败经飞书告警，飞书失败经邮件告警
- 推送渠道未配置时跳过（不推送、不告警），仅「已配置且发送失败」触发备份告警（v1.2）
- 采集源未配置/占位值时标记为「未接入」，跳过采集（不发无谓请求，v1.3）
"""

from __future__ import annotations

import argparse
import re
import sys
from datetime import date, datetime, time, timedelta
from pathlib import Path
from typing import Any

import collector.github as github
import collector.lark_attendance as lark_attendance
import collector.lark_msg as lark_msg
import collector.lark_task as lark_task
from collector.lark_attendance import AttendanceRecord
from generator import DailyReport, MemberReport, generate
from generator.formatter import UNASSIGNED_MEMBER_NAME
from notifier import email as email_notifier
from notifier import lark_bot as lark_bot_notifier
from shared.config import env, load_config
from shared.errors import ReportError
from shared.logger import get_logger, setup_logging
from shared.storage import Storage

logger = get_logger("main")

# 系统本地时区（design.md §6.4："当日"指本地自然日，采集窗口必须携带时区）
LOCAL_TZ = datetime.now().astimezone().tzinfo


def main(argv: list[str] | None = None) -> int:
    """命令行入口。"""
    setup_logging()
    # 管道/重定向下 stdout 可能按本地编码（如 GBK）输出，日报正文含 emoji 会
    # 抛 UnicodeEncodeError；显式切到 UTF-8（与 scripts/*_guard.py 一致）
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")
    parser = argparse.ArgumentParser(description="智能日报生成器")
    group = parser.add_mutually_exclusive_group()
    group.add_argument("--check", action="store_true", help="仅验证配置与 API 连接")
    group.add_argument("--dry-run", action="store_true", help="采集并生成日报，但不推送")
    group.add_argument("--history", action="store_true", help="列出最近 N 天日报（只读，v1.7）")
    group.add_argument("--show", metavar="DATE", help="打印指定日期日报 Markdown（只读，v1.7）")
    group.add_argument("--export", metavar="DATE", help="导出指定日期日报为文件（只读，v1.7）")
    parser.add_argument("--days", default="30",
                        help="--history 的查询天数（正整数，默认 30）")
    args = parser.parse_args(argv)
    if args.check:
        return check()
    if args.history:
        return history(args.days)
    if args.show:
        return show(args.show)
    if args.export:
        return export(args.export)
    return run(dry_run=args.dry_run)


def run(dry_run: bool = False) -> int:
    """完整日报流程：采集 → 聚合 → 生成 → 推送 → 落库。"""
    start = datetime.now()
    logger.info("日报流程开始", extra={"start": start.isoformat()})
    try:
        return _run(dry_run)
    except Exception as exc:  # 全局兜底：任何失败都必须有日志（§6.1）
        logger.error("日报流程异常终止", exc_info=exc)
        return 1
    finally:
        end = datetime.now()
        logger.info("日报流程结束", extra={
            "end": end.isoformat(),
            "elapsed_seconds": round((end - start).total_seconds(), 2),
        })


def _run(dry_run: bool) -> int:
    cfg = load_config()
    today = date.today()
    # 采集窗口：本地时区当日 00:00 → 执行时刻，必须携带时区（design.md §6.4）
    since = datetime.combine(today, time.min, tzinfo=LOCAL_TZ)
    until = datetime.now(LOCAL_TZ)

    # 1) 采集：四个数据源相互独立，单源失败不阻断其他源
    # v1.3：未配置/占位值的源标记为“未接入”，跳过采集（不发无谓请求）
    commits = github.collect(cfg["collector"]["github"]["repos"], since, until)
    logger.info("数据源采集完成", extra={"source": "github", "count": len(commits)})

    tasks_collected = _is_configured(cfg["collector"]["lark_task"]["project_id"])
    if tasks_collected:
        tasks = lark_task.collect(cfg["collector"]["lark_task"]["project_id"], since, until)
        logger.info("数据源采集完成", extra={"source": "lark_task", "count": len(tasks)})
    else:
        tasks = []
        logger.info("飞书任务未配置，跳过采集", extra={"source": "lark_task"})

    messages_collected = _is_configured(cfg["collector"]["lark_msg"]["chat_id"])
    if messages_collected:
        messages = lark_msg.collect(cfg["collector"]["lark_msg"]["chat_id"],
                                    cfg["collector"]["lark_msg"]["keywords"], since, until)
        logger.info("数据源采集完成", extra={"source": "lark_msg", "count": len(messages)})
    else:
        messages = []
        logger.info("飞书消息未配置，跳过采集", extra={"source": "lark_msg"})

    # 考勤（v1.1）：失败经 CollectResult.error 显式返回（ADR-003）；凭据缺失跳过采集
    attendance_collected = bool(env("LARK_APP_ID")) and bool(env("LARK_APP_SECRET"))
    if attendance_collected:
        attendance_result = lark_attendance.collect(today, today)
        if attendance_result.success:
            logger.info("数据源采集完成",
                        extra={"source": "lark_attendance", "count": len(attendance_result.data)})
        else:
            logger.error("飞书考勤采集失败，日报将以可读说明标注该数据源",
                         extra={"reason": attendance_result.error})
    else:
        attendance_result = None
        logger.info("飞书考勤未配置，跳过采集", extra={"source": "lark_attendance"})

    attendance_error = None
    if attendance_collected:
        attendance_error = None if attendance_result.success else attendance_result.error
    else:
        attendance_error = "飞书应用凭据未配置（环境变量 LARK_APP_ID / LARK_APP_SECRET）"
    source_errors = {
        "github": github.get_last_error(),
        "lark_task": lark_task.get_last_error() if tasks_collected
                     else "飞书任务未配置（collector.lark_task.project_id 为空或占位值）",
        "lark_msg": lark_msg.get_last_error() if messages_collected
                    else "飞书消息未配置（collector.lark_msg.chat_id 为空或占位值）",
        "lark_attendance": attendance_error,
    }

    # 2) 所有数据源均不可用：不生成空日报，记录错误日志 + 告警邮件（§6.1）
    if all(source_errors.values()):
        failed = {key: value for key, value in source_errors.items() if value}
        logger.error("所有数据源均不可用，不生成日报", extra={"errors": failed})
        _alert_email(cfg, today, f"所有数据源均不可用，日报未生成：{failed}")
        return 1

    # 3) 聚合为成员报告（§3.3 成员身份映射）
    members = _aggregate(cfg, commits, tasks, messages,
                         attendance_result.data if attendance_result else [], source_errors)

    # 4) 生成日报（window 为采集窗口，用于头部“数据范围”，v1.3；
    #    v1.6：先读历史统计组装 history，读不到时传 None，不影响日报生成）
    history = _load_history(cfg, today, members)
    report = generate(members, today, cfg["team_name"], window=(since, until), history=history)

    if dry_run:
        logger.info("dry-run 模式：跳过推送与落库")
        return 0

    # 5) 落库（日报历史 + 当日统计，ADR-002 / design.md §6.5 v1.6；失败不影响推送）
    try:
        db_path = _db_path(cfg)
        stats = _today_stats(report.members)
        storage = Storage(db_path)
        storage.save_report(today, cfg["team_name"], report.markdown, report.html)
        storage.save_stats(today, cfg["team_name"], stats["commits"], stats["additions"],
                           stats["deletions"], stats["messages"])
        logger.info("日报已写入本地存储", extra={"db_path": db_path})
    except ReportError as exc:
        logger.error("日报写入存储失败", extra={"reason": str(exc)}, exc_info=exc)

    # 6) 推送 + 互为备份告警（§6.1）
    # v1.2：未配置的渠道跳过（不推送、不告警）；仅「已配置且发送失败」触发备份告警
    email_recipients = _email_recipients(cfg)
    if email_recipients:
        email_ok: bool | None = email_notifier.send(report, email_recipients)
    else:
        email_ok = None
        logger.info("邮件渠道未配置，跳过推送与备份告警", extra={"channel": "email"})
    if _lark_webhook_configured(cfg):
        lark_ok: bool | None = lark_bot_notifier.send(
            report, cfg["collector"]["lark_msg"]["chat_id"])
    else:
        lark_ok = None
        logger.info("飞书渠道未配置，跳过推送与备份告警", extra={"channel": "lark_bot"})
    logger.info("推送结果", extra={"email": email_ok, "lark": lark_ok})
    if email_ok is False:
        _alert_lark(cfg, today, "邮件推送失败，请检查邮件配置")
    if lark_ok is False:
        _alert_email(cfg, today, "飞书推送失败，请检查飞书机器人配置")
    return 0


def check() -> int:
    """--check：验证配置与各外部依赖连通性（design.md §6.3 健康检查）。"""
    logger.info("健康检查开始")
    try:
        cfg = load_config()
    except ReportError as exc:
        logger.error("配置校验失败", exc_info=exc)
        return 1
    logger.info("配置校验通过", extra={"team_name": cfg["team_name"],
                                       "members": len(cfg["members"])})

    now = datetime.now(LOCAL_TZ)
    probe_since = now - timedelta(minutes=1)  # 探测窗口仅 1 分钟，避免拉取全量数据
    results: dict[str, bool] = {}

    github.collect(cfg["collector"]["github"]["repos"], probe_since, now)
    results["github"] = github.get_last_error() is None
    _log_probe("GitHub API", results["github"], github.get_last_error())

    lark_task.collect(cfg["collector"]["lark_task"]["project_id"], probe_since, now)
    results["lark_task"] = lark_task.get_last_error() is None
    _log_probe("飞书任务 API", results["lark_task"], lark_task.get_last_error())

    lark_msg.collect(cfg["collector"]["lark_msg"]["chat_id"],
                     cfg["collector"]["lark_msg"]["keywords"], probe_since, now)
    results["lark_msg"] = lark_msg.get_last_error() is None
    _log_probe("飞书消息 API", results["lark_msg"], lark_msg.get_last_error())

    attendance_result = lark_attendance.collect(probe_since.date(), now.date())
    results["lark_attendance"] = attendance_result.success
    _log_probe("飞书考勤 API", attendance_result.success, attendance_result.error)

    results["email"] = _probe_smtp(cfg["notifier"]["email"])
    try:
        lark_bot_notifier._webhook_url()
        results["lark_bot"] = True
    except Exception as exc:
        results["lark_bot"] = False
        logger.error("飞书 webhook 检查失败", extra={"reason": str(exc)})
    if results["lark_bot"]:
        logger.info("连接正常", extra={"target": "飞书 webhook"})

    failed = [name for name, ok in results.items() if not ok]
    if failed:
        logger.error("健康检查失败", extra={"failed": failed})
        return 1
    logger.info("健康检查通过")
    return 0


# ---------- v1.7：日报历史查询与导出（只读 CLI，design.md §6.3 / §6.5） ----------

# --history 表头：列宽与 _history_row 对齐（日期 10 / 提交 4 / 变更行数 11）
HISTORY_HEADER = "日期        提交  变更行数       生成时间"
DATE_FORMAT_HINT = "日期格式错误，应为 YYYY-MM-DD"
DAYS_HINT = "天数必须是正整数"
NO_HISTORY_TEXT = "暂无历史日报记录"
STORAGE_READ_ERROR_TEXT = "读取本地日报存储失败"

_DATE_RE = re.compile(r"\d{4}-\d{2}-\d{2}")


def _parse_date(value: str) -> date | None:
    """仅接受 YYYY-MM-DD（v1.7）；非法输入返回 None。"""
    if not _DATE_RE.fullmatch(value):
        return None
    try:
        return datetime.strptime(value, "%Y-%m-%d").date()
    except ValueError:
        return None


def _open_storage() -> Storage | None:
    """只读命令共用的存储句柄（v1.7）；配置/存储打开失败时打印提示并返回 None。"""
    try:
        return Storage(_db_path(load_config()))
    except ReportError as exc:
        logger.error("打开本地日报存储失败", extra={"reason": str(exc)}, exc_info=exc)
        print(STORAGE_READ_ERROR_TEXT)
        return None


def _generated_str(value: str) -> str:
    """把 ISO 格式的 generated_at 转成「YYYY-MM-DD HH:MM」用于展示。"""
    try:
        return datetime.fromisoformat(value).strftime("%Y-%m-%d %H:%M")
    except ValueError:
        return str(value)


def _history_row(row: dict) -> str:
    """--history 的一行：日期 / 提交 / 变更行数 / 生成时间；无统计时计数列为「—」。"""
    date_s = str(row["date"])
    if row["commits_count"] is None:
        commits_s, changes_s = "—", "—"
    else:
        commits_s = str(row["commits_count"])
        changes_s = f"+{row['additions']}/-{row['deletions']}"
    return f"{date_s:<10}{commits_s:>4} {changes_s:<11}{_generated_str(row['generated_at'])}"


def history(days: str) -> int:
    """--history：列出最近 N 天日报（只读：不采集/不生成/不推送/不落库）。"""
    try:
        count = int(days)
    except (TypeError, ValueError):
        print(DAYS_HINT)
        return 1
    if count < 1:
        print(DAYS_HINT)
        return 1
    storage = _open_storage()
    if storage is None:
        return 1
    try:
        rows = storage.list_recent_reports(count)
    except ReportError as exc:
        logger.error("读取日报历史失败", extra={"reason": str(exc)}, exc_info=exc)
        print(STORAGE_READ_ERROR_TEXT)
        return 1
    if not rows:
        print(NO_HISTORY_TEXT)
        return 0
    print(HISTORY_HEADER)
    for row in rows:
        print(_history_row(row))
    return 0


def _latest_report(storage: Storage, date_str: str) -> dict | None:
    """读取该日最新一条日报（get_reports 按 id 倒序即插入顺序倒序）；
    读取失败或无记录时打印提示并返回 None。"""
    try:
        records = storage.get_reports(_parse_date(date_str))
    except ReportError as exc:
        logger.error("读取日报失败", extra={"reason": str(exc)}, exc_info=exc)
        print(STORAGE_READ_ERROR_TEXT)
        return None
    if not records:
        print(f"未找到 {date_str} 的日报记录")
        return None
    return records[0]


def show(date_str: str) -> int:
    """--show：原样打印该日最新一条日报 Markdown，末尾附导出提示（只读，v1.7）。"""
    if _parse_date(date_str) is None:
        print(DATE_FORMAT_HINT)
        return 1
    storage = _open_storage()
    if storage is None:
        return 1
    latest = _latest_report(storage, date_str)
    if latest is None:
        return 1
    markdown = str(latest["markdown"])
    print(markdown, end="")
    if not markdown.endswith("\n"):
        print()
    print()  # 空一行后接导出提示
    print(f"提示：可执行 python main.py --export {date_str} 导出为文件")
    return 0


def export(date_str: str) -> int:
    """--export：导出该日最新一条日报为 output/report-<日期>.md 与 .html（v1.7）。

    仅此命令写文件（导出产物）；不采集、不生成、不推送、不写库。
    """
    if _parse_date(date_str) is None:
        print(DATE_FORMAT_HINT)
        return 1
    storage = _open_storage()
    if storage is None:
        return 1
    latest = _latest_report(storage, date_str)
    if latest is None:
        return 1
    out_dir = Path("output")
    md_path = out_dir / f"report-{date_str}.md"
    html_path = out_dir / f"report-{date_str}.html"
    try:
        out_dir.mkdir(parents=True, exist_ok=True)
        md_path.write_text(str(latest["markdown"]), encoding="utf-8")
        html_path.write_text(str(latest["html"]), encoding="utf-8")
    except OSError as exc:
        logger.error("导出日报文件失败", extra={"reason": str(exc)}, exc_info=exc)
        print("导出文件失败")
        return 1
    print(f"已导出：{md_path.as_posix()}")
    print(f"已导出：{html_path.as_posix()}")
    return 0


def _aggregate(cfg: dict, commits: list, tasks: list, messages: list,
               attendance: list[AttendanceRecord],
               source_errors: dict) -> list[MemberReport]:
    """按 config 的 members 映射表把各类记录聚合成成员报告（design.md §3.3）。

    GitHub 记录按 github 用户名匹配；飞书记录按 lark 用户名或成员姓名匹配；
    考勤记录按 employee_id 与 lark 用户名匹配。
    v1.9：未匹配到任何成员的提交/任务/消息不再丢弃，统一归入
    「其他贡献」伪成员（design.md §6.5）；考勤按人-日维度无法归属，
    仍只记告警、不归集。
    """
    reports: list[MemberReport] = []
    # 已归属记录的对象 id 集合（v1.9）：按对象身份判定未归属，避免值相等误判
    matched_commits: set[int] = set()
    matched_tasks: set[int] = set()
    matched_messages: set[int] = set()
    assigned_attendance = 0
    attendance_by_employee: dict[str, AttendanceRecord] = {}
    for record in attendance:
        attendance_by_employee[record.employee_id] = record  # 同员工多条取最后一条
    for member_cfg in cfg["members"]:
        name = str(member_cfg.get("name") or "")
        github_name = str(member_cfg.get("github") or "")
        lark_name = str(member_cfg.get("lark") or "")
        member_commits = [c for c in commits if github_name and c.author == github_name]
        member_tasks = [t for t in tasks if _matches(t.assignee, lark_name, name)]
        member_messages = [m for m in messages if _matches(m.sender, lark_name, name)]
        member_attendance = attendance_by_employee.get(lark_name) if lark_name else None
        matched_commits.update(id(c) for c in member_commits)
        matched_tasks.update(id(t) for t in member_tasks)
        matched_messages.update(id(m) for m in member_messages)
        assigned_attendance += 1 if member_attendance is not None else 0
        reports.append(MemberReport(
            name=name,
            github_username=github_name,
            commits=member_commits,
            tasks=member_tasks,
            messages=member_messages,
            source_errors={key: value for key, value in source_errors.items() if value},
            attendance=member_attendance,
        ))
    # v1.9：未归属记录归入「其他贡献」伪成员（= 采集到的总数 - 已分配数）
    unassigned_commits = [c for c in commits if id(c) not in matched_commits]
    unassigned_tasks = [t for t in tasks if id(t) not in matched_tasks]
    unassigned_messages = [m for m in messages if id(m) not in matched_messages]
    if unassigned_commits or unassigned_tasks or unassigned_messages:
        logger.info("未归属记录归入「其他贡献」",
                    extra={"commits": len(unassigned_commits),
                           "tasks": len(unassigned_tasks),
                           "messages": len(unassigned_messages)})
        reports.append(MemberReport(
            name=UNASSIGNED_MEMBER_NAME,
            github_username="",
            commits=unassigned_commits,
            tasks=unassigned_tasks,
            messages=unassigned_messages,
            source_errors={},
            attendance=None,
            is_unassigned=True,
        ))
    if len(attendance) > assigned_attendance:
        logger.warning("存在未映射到任何成员的考勤记录，已跳过",
                       extra={"count": len(attendance) - assigned_attendance})
    return reports


def _matches(value: str, *keys: str) -> bool:
    """value 是否等于任一非空 key。"""
    return any(key and value == key for key in keys)


def _db_path(cfg: dict) -> str:
    """SQLite 路径（读历史与落库共用；缺省 data/report.db）。"""
    return str(cfg.get("storage", {}).get("db_path", "data/report.db"))


def _today_stats(members: list[MemberReport]) -> dict[str, int]:
    """当日统计（v1.6）：提交/增删行数/消息数，写入 daily_stats 并计入本周概况。"""
    return {
        "commits": sum(len(m.commits) for m in members),
        "additions": sum(c.additions for m in members for c in m.commits),
        "deletions": sum(c.deletions for m in members for c in m.commits),
        "messages": sum(len(m.messages) for m in members),
    }


def _stats_totals(rows: list[dict]) -> dict[str, int]:
    """汇总 daily_stats 查询结果（v1.6）；活跃天数 = 有提交或消息的天数。"""
    return {
        "commits": sum(r["commits_count"] for r in rows),
        "additions": sum(r["additions"] for r in rows),
        "deletions": sum(r["deletions"] for r in rows),
        "active_days": sum(1 for r in rows if r["commits_count"] or r["messages_count"]),
    }


def _load_history(cfg: dict, today: date, members: list[MemberReport]) -> dict | None:
    """v1.6：读取历史统计并组装 history（design.md §6.5）。

    本周 = 周一 ~ 当日：周一 ~ 昨日读 daily_stats，当日统计由 members 现算
    （当日统计在生成后才写入，且避免同日重复运行时重复计数），
    因此本周必然包含今天（design.md §6.5「周一 ~ 当日」）；
    上周 = 完整的上一自然周（上周一 ~ 上周日）；昨日单独成行。
    仅当历史为空且当日无任何数据时返回 None（本周概况显示「暂无历史数据」）；
    读取失败同样返回 None，不阻断日报流程。
    """
    try:
        storage = Storage(_db_path(cfg))
        week_start = today - timedelta(days=today.weekday())
        week_rows = storage.get_stats_between(week_start, today - timedelta(days=1))
        last_rows = storage.get_stats_between(week_start - timedelta(days=7),
                                              week_start - timedelta(days=1))
        y_rows = storage.get_stats_between(today - timedelta(days=1),
                                           today - timedelta(days=1))
    except ReportError as exc:
        logger.error("读取历史统计失败，本周概况将显示「暂无历史数据」",
                     extra={"reason": str(exc)}, exc_info=exc)
        return None
    today_stats = _today_stats(members)
    today_active = bool(today_stats["commits"] or today_stats["messages"])
    if not (week_rows or last_rows or y_rows) and not today_active:
        return None  # 历史为空且当日无数据：没有可展示的趋势
    week = _stats_totals(week_rows)
    week["commits"] += today_stats["commits"]
    week["additions"] += today_stats["additions"]
    week["deletions"] += today_stats["deletions"]
    if today_active:
        week["active_days"] += 1
    week["start"] = week_start
    week["end"] = today
    last_week = _stats_totals(last_rows)
    last_week["start"] = week_start - timedelta(days=7)
    last_week["end"] = week_start - timedelta(days=1)
    yesterday = None
    if y_rows:
        yesterday_rows = _stats_totals(y_rows)
        yesterday = {"commits": yesterday_rows["commits"],
                     "additions": yesterday_rows["additions"],
                     "deletions": yesterday_rows["deletions"]}
    return {"week": week, "last_week": last_week, "yesterday": yesterday}


def _alert_report(today: date, team_name: str, message: str) -> DailyReport:
    """构造告警用最小日报对象（§6.1：告警通道复用推送层）。"""
    markdown = f"# {team_name} 告警（{today.isoformat()}）\n\n- {message}\n"
    html = f"<h1>{team_name} 告警（{today.isoformat()}）</h1><p>{message}</p>"
    return DailyReport(date=today, team_name=team_name, members=[],
                       generated_at=datetime.now(), markdown=markdown, html=html)


def _alert_email(cfg: dict, today: date, message: str) -> None:
    """经邮件发送告警；告警本身失败仅记录日志。"""
    ok = email_notifier.send(_alert_report(today, cfg["team_name"], message),
                             cfg["notifier"]["email"]["recipients"])
    if not ok:
        logger.error("告警邮件发送失败", extra={"alert_message": message})


def _alert_lark(cfg: dict, today: date, message: str) -> None:
    """经飞书发送告警；告警本身失败仅记录日志。"""
    ok = lark_bot_notifier.send(_alert_report(today, cfg["team_name"], message),
                                cfg["collector"]["lark_msg"]["chat_id"])
    if not ok:
        logger.error("飞书告警发送失败", extra={"alert_message": message})


_PLACEHOLDER_ALL_X = re.compile(r"[xX_]+")
_PLACEHOLDER_OC_X = re.compile(r"oc_[xX]+")


def _is_configured(value: Any) -> bool:
    """数据源配置值是否有效（v1.3）：空/占位值视为未接入，跳过采集不发无谓请求。

    占位值形态：空串、全 x（xxxx / oc_xxxxxxxx）、your-*/example 前缀、
    含 placeholder/todo/none/null。
    """
    text = str(value or "").strip()
    if not text:
        return False
    lower = text.lower()
    if any(word in lower for word in ("placeholder", "todo", "none", "null")):
        return False
    if _PLACEHOLDER_ALL_X.fullmatch(text) or _PLACEHOLDER_OC_X.fullmatch(text):
        return False
    if text.startswith("your-") or text.startswith("example"):
        return False
    return True


def _email_recipients(cfg: dict) -> list[str]:
    """邮件推送渠道是否已配置（v1.2）：notifier.email.recipients 非空。返回收件人列表。"""
    email_cfg = cfg.get("notifier", {}).get("email") or {}
    return [str(r) for r in (email_cfg.get("recipients") or [])]


def _lark_webhook_configured(cfg: dict) -> bool:
    """飞书推送渠道是否已配置（v1.2）：notifier.lark_bot.webhook_url 或
    环境变量 LARK_BOT_WEBHOOK_URL 任一非空（回退规则与 notifier/lark_bot.py 一致）。"""
    lark_cfg = cfg.get("notifier", {}).get("lark_bot") or {}
    return bool(str(lark_cfg.get("webhook_url") or "").strip()) \
        or bool(env("LARK_BOT_WEBHOOK_URL").strip())


def _probe_smtp(email_cfg: dict) -> bool:
    """SMTP 连通性探测：连接 + ehlo（不登录、不发信）。"""
    try:
        server = email_notifier._create_smtp(email_cfg["smtp_host"], email_cfg["smtp_port"])
    except Exception as exc:
        logger.error("SMTP 连接检查失败", extra={"reason": str(exc)})
        return False
    try:
        server.ehlo()
        ok = True
    except Exception as exc:
        logger.error("SMTP 连接检查失败", extra={"reason": str(exc)})
        ok = False
    try:
        server.quit()
    except Exception:
        pass  # 断开失败不影响检查结论
    if ok:
        logger.info("连接正常", extra={"target": "SMTP", "host": email_cfg["smtp_host"]})
    return ok


def _log_probe(name: str, ok: bool, reason: str | None) -> None:
    if ok:
        logger.info("连接正常", extra={"target": name})
    else:
        logger.error("连接检查失败", extra={"target": name, "reason": reason})


if __name__ == "__main__":
    sys.exit(main())
