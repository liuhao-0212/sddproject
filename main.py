"""主编排入口：采集 → 聚合 → 生成 → 推送（design.md §2）。

用法:
    python main.py            # 完整流程：采集 + 聚合 + 生成 + 推送 + 落库
    python main.py --check    # 仅验证配置与 API 连接（design.md §6.3 健康检查）
    python main.py --dry-run  # 采集 + 生成，不推送、不落库

错误处理（design.md §6.1）:
- 单个数据源失败不阻断其他数据源；github/lark_task/lark_msg 经 get_last_error()
  探测（区分“失败”与“为空”），考勤经 CollectResult.success 探测（ADR-003），
  失败原因填入 MemberReport.source_errors，由生成层在日报中标注
  “数据获取失败”/“考勤数据暂不可用”
- 所有数据源失败时不生成空日报，记录错误日志并发送告警邮件
- 推送渠道互为备份告警通道：邮件失败经飞书告警，飞书失败经邮件告警
"""

from __future__ import annotations

import argparse
import sys
from datetime import date, datetime, time, timedelta

import collector.github as github
import collector.lark_attendance as lark_attendance
import collector.lark_msg as lark_msg
import collector.lark_task as lark_task
from collector.lark_attendance import AttendanceRecord
from generator import DailyReport, MemberReport, generate
from notifier import email as email_notifier
from notifier import lark_bot as lark_bot_notifier
from shared.config import load_config
from shared.errors import ReportError
from shared.logger import get_logger, setup_logging
from shared.storage import Storage

logger = get_logger("main")


def main(argv: list[str] | None = None) -> int:
    """命令行入口。"""
    setup_logging()
    parser = argparse.ArgumentParser(description="智能日报生成器")
    group = parser.add_mutually_exclusive_group()
    group.add_argument("--check", action="store_true", help="仅验证配置与 API 连接")
    group.add_argument("--dry-run", action="store_true", help="采集并生成日报，但不推送")
    args = parser.parse_args(argv)
    if args.check:
        return check()
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
    since = datetime.combine(today, time.min)  # 采集窗口：当日 00:00 至执行时刻（proposal §4）
    until = datetime.now()

    # 1) 采集：四个数据源相互独立，单源失败不阻断其他源
    commits = github.collect(cfg["collector"]["github"]["repos"], since, until)
    logger.info("数据源采集完成", extra={"source": "github", "count": len(commits)})
    tasks = lark_task.collect(cfg["collector"]["lark_task"]["project_id"], since, until)
    logger.info("数据源采集完成", extra={"source": "lark_task", "count": len(tasks)})
    messages = lark_msg.collect(cfg["collector"]["lark_msg"]["chat_id"],
                                cfg["collector"]["lark_msg"]["keywords"], since, until)
    logger.info("数据源采集完成", extra={"source": "lark_msg", "count": len(messages)})
    # 考勤（v1.1）：失败经 CollectResult.error 显式返回（ADR-003），异常不得吞掉
    attendance_result = lark_attendance.collect(today, today)
    if attendance_result.success:
        logger.info("数据源采集完成",
                    extra={"source": "lark_attendance", "count": len(attendance_result.data)})
    else:
        logger.error("飞书考勤采集失败，日报将标注“考勤数据暂不可用”",
                     extra={"reason": attendance_result.error})

    source_errors = {
        "github": github.get_last_error(),
        "lark_task": lark_task.get_last_error(),
        "lark_msg": lark_msg.get_last_error(),
        "lark_attendance": None if attendance_result.success else attendance_result.error,
    }

    # 2) 所有数据源均不可用：不生成空日报，记录错误日志 + 告警邮件（§6.1）
    if all(source_errors.values()):
        failed = {key: value for key, value in source_errors.items() if value}
        logger.error("所有数据源均不可用，不生成日报", extra={"errors": failed})
        _alert_email(cfg, today, f"所有数据源均不可用，日报未生成：{failed}")
        return 1

    # 3) 聚合为成员报告（§3.3 成员身份映射）
    members = _aggregate(cfg, commits, tasks, messages,
                         attendance_result.data, source_errors)

    # 4) 生成日报
    report = generate(members, today, cfg["team_name"])

    if dry_run:
        logger.info("dry-run 模式：跳过推送与落库")
        return 0

    # 5) 落库（日报历史，ADR-002；失败不影响推送）
    try:
        db_path = cfg.get("storage", {}).get("db_path", "data/report.db")
        Storage(db_path).save_report(today, cfg["team_name"], report.markdown, report.html)
        logger.info("日报已写入本地存储", extra={"db_path": db_path})
    except ReportError as exc:
        logger.error("日报写入存储失败", extra={"reason": str(exc)}, exc_info=exc)

    # 6) 推送 + 互为备份告警（§6.1）
    email_ok = email_notifier.send(report, cfg["notifier"]["email"]["recipients"])
    lark_ok = lark_bot_notifier.send(report, cfg["collector"]["lark_msg"]["chat_id"])
    logger.info("推送结果", extra={"email": email_ok, "lark": lark_ok})
    if not email_ok:
        _alert_lark(cfg, today, "邮件推送失败，请检查邮件配置")
    if not lark_ok:
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

    now = datetime.now()
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


def _aggregate(cfg: dict, commits: list, tasks: list, messages: list,
               attendance: list[AttendanceRecord],
               source_errors: dict) -> list[MemberReport]:
    """按 config 的 members 映射表把各类记录聚合成成员报告（design.md §3.3）。

    GitHub 记录按 github 用户名匹配；飞书记录按 lark 用户名或成员姓名匹配；
    考勤记录按 employee_id 与 lark 用户名匹配。
    未映射到任何成员的记录跳过并记录告警（不静默丢弃）。
    """
    reports: list[MemberReport] = []
    assigned_commits = assigned_tasks = assigned_messages = assigned_attendance = 0
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
        assigned_commits += len(member_commits)
        assigned_tasks += len(member_tasks)
        assigned_messages += len(member_messages)
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
    if len(commits) > assigned_commits:
        logger.warning("存在未映射到任何成员的提交记录，已跳过",
                       extra={"count": len(commits) - assigned_commits})
    if len(tasks) > assigned_tasks:
        logger.warning("存在未映射到任何成员的任务记录，已跳过",
                       extra={"count": len(tasks) - assigned_tasks})
    if len(messages) > assigned_messages:
        logger.warning("存在未映射到任何成员的消息记录，已跳过",
                       extra={"count": len(messages) - assigned_messages})
    if len(attendance) > assigned_attendance:
        logger.warning("存在未映射到任何成员的考勤记录，已跳过",
                       extra={"count": len(attendance) - assigned_attendance})
    return reports


def _matches(value: str, *keys: str) -> bool:
    """value 是否等于任一非空 key。"""
    return any(key and value == key for key in keys)


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
        logger.error("告警邮件发送失败", extra={"message": message})


def _alert_lark(cfg: dict, today: date, message: str) -> None:
    """经飞书发送告警；告警本身失败仅记录日志。"""
    ok = lark_bot_notifier.send(_alert_report(today, cfg["team_name"], message),
                                cfg["collector"]["lark_msg"]["chat_id"])
    if not ok:
        logger.error("飞书告警发送失败", extra={"message": message})


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
