"""端到端集成测试：验证 main.py 完整流程在各场景下的行为（tasks.md Task 10）。

- 打桩采集/推送/存储模块（monkeypatch），零真实网络调用
- 聚合（_aggregate）与生成（generate）走真实实现，保证端到端语义
"""

import logging
import re
import time
from datetime import date, datetime, time as dtime, timezone

import pytest

import generator.formatter as formatter
import main
from collector.github import CommitRecord
from collector.github_activity import RepoEvent
from collector.lark_attendance import AttendanceRecord, CollectResult
from collector.lark_msg import MessageRecord
from collector.lark_task import TaskRecord
from shared.errors import CollectorError, StorageError

REPORT_DATE = date(2026, 9, 29)
TZ = timezone.utc


def make_cfg() -> dict:
    return {
        "team_name": "研发一组",
        "members": [
            {"name": "张三", "github": "zhangsan", "lark": "zhangsan@company.com"},
            {"name": "李四", "github": "lisi", "lark": "lisi@company.com"},
        ],
        "collector": {
            "github": {"repos": ["org/repo"]},
            "lark_task": {"project_id": "p1"},
            # v1.12：飞书消息采集默认关闭（显式写出，避免隐式依赖）
            "lark_msg": {"chat_id": "oc_test", "keywords": ["评审"], "enabled": False},
        },
        "notifier": {"email": {"recipients": ["leader@company.com"]},
                     "lark_bot": {"webhook_url": "https://open.feishu.cn/open-apis/bot/v2/hook/abc"}},
        "storage": {"db_path": "tmp/report.db"},
    }


def make_commit(author="zhangsan", message="feat: 登录页"):
    return CommitRecord(author=author, message=message,
                        timestamp=datetime(2026, 9, 29, 8, 30, tzinfo=TZ),
                        repo="org/repo", additions=10, deletions=3, files_changed=2)


def make_task(assignee="zhangsan@company.com", title="实现登录页"):
    return TaskRecord(assignee=assignee, title=title, status_from="未开始",
                      status_to="进行中",
                      updated_at=datetime(2026, 9, 29, 10, 0, tzinfo=TZ))


def make_message(sender="zhangsan@company.com", content="评审文档已上传"):
    return MessageRecord(sender=sender, content=content,
                         timestamp=datetime(2026, 9, 29, 10, 5, tzinfo=TZ),
                         chat_name="团队群")


def make_attendance(employee_id="zhangsan@company.com", work_hours=9.0, status="正常"):
    return AttendanceRecord(employee_id=employee_id, date=REPORT_DATE,
                            check_in=datetime(2026, 9, 29, 9, 0, tzinfo=TZ),
                            check_out=datetime(2026, 9, 29, 18, 0, tzinfo=TZ),
                            work_hours=work_hours, status=status)


class _FakeDate:
    @staticmethod
    def today():
        return REPORT_DATE


def stub_sources(monkeypatch, commits=None, tasks=None, messages=None,
                 attendance=None, github_err=None, task_err=None, msg_err=None,
                 attendance_err=None, repo_events=None, repo_err=None):
    """打桩采集模块：返回固定记录；v1.0 三个数据源经 get_last_error 注入失败原因，
    考勤经 CollectResult 注入（ADR-003）；v1.12 仓库动态默认返回空列表
    （日报显示「今日无仓库动态」），repo_err 时抛 CollectorError（板块隐藏）。"""
    monkeypatch.setattr(main.github, "collect",
                        lambda repos, since, until: list(commits or []))
    monkeypatch.setattr(main.github, "get_last_error", lambda: github_err)

    def repo_collect(repos, since, until):
        if repo_err:
            raise CollectorError(repo_err)
        return list(repo_events or [])

    monkeypatch.setattr(main.github_activity, "collect", repo_collect)
    monkeypatch.setattr(main.lark_task, "collect",
                        lambda project_id, since, until: list(tasks or []))
    monkeypatch.setattr(main.lark_task, "get_last_error", lambda: task_err)
    monkeypatch.setattr(main.lark_msg, "collect",
                        lambda chat_id, keywords, since, until: list(messages or []))
    monkeypatch.setattr(main.lark_msg, "get_last_error", lambda: msg_err)
    monkeypatch.setattr(main.lark_attendance, "collect",
                        lambda since, until: CollectResult(
                            success=attendance_err is None,
                            data=list(attendance or []),
                            error=attendance_err))


@pytest.fixture(autouse=True)
def fixed_local_tz(monkeypatch):
    """固定生成层展示时区为 UTC，保证既有断言与机器时区无关
    （§6.4 转换逻辑由 test_generator.py 专项测试覆盖）。"""
    monkeypatch.setattr(formatter, "LOCAL_TZ", timezone.utc)


@pytest.fixture
def env(monkeypatch):
    """打桩 main 的配置、日期、推送、告警与存储，并记录各调用。"""
    state = {"emails": [], "larks": [], "alerts_email": [], "alerts_lark": [],
             "saved": [], "stats_saved": [], "stats_rows": []}

    monkeypatch.setattr(main, "date", _FakeDate)
    monkeypatch.setattr(main, "load_config", make_cfg)
    # v1.3：飞书凭据存在时考勤才会采集；默认视为已配置（跳过采集的场景专项测试覆盖）
    monkeypatch.setenv("LARK_APP_ID", "cli_test")
    monkeypatch.setenv("LARK_APP_SECRET", "sec_test")
    monkeypatch.setattr(main.email_notifier, "send",
                        lambda report, recipients: (state["emails"].append(
                            (report, recipients)), True)[1])
    monkeypatch.setattr(main.lark_bot_notifier, "send",
                        lambda report, chat_id: (state["larks"].append(
                            (report, chat_id)), True)[1])
    monkeypatch.setattr(main, "_alert_email",
                        lambda cfg, today, msg: state["alerts_email"].append(msg))
    monkeypatch.setattr(main, "_alert_lark",
                        lambda cfg, today, msg: state["alerts_lark"].append(msg))

    class FakeStorage:
        def __init__(self, path):
            self.path = path

        def save_report(self, *args):
            state["saved"].append(args)

        def save_stats(self, *args):
            state["stats_saved"].append(args)

        def get_stats_between(self, start, end):
            """按 [start, end] 闭区间过滤预置统计行（与真实 Storage 行为一致）。"""
            if state.get("stats_read_error"):
                raise StorageError("读取统计失败（模拟）")
            start_s = start.isoformat() if isinstance(start, date) else str(start)
            end_s = end.isoformat() if isinstance(end, date) else str(end)
            return [row for row in state["stats_rows"]
                    if start_s <= row["date"] <= end_s]

    monkeypatch.setattr(main, "Storage", FakeStorage)
    return state


# ---------- 1. 正常场景 ----------

def test_normal_flow_generates_report_and_pushes(monkeypatch, env):
    stub_sources(monkeypatch,
                 commits=[make_commit()],
                 tasks=[make_task()],
                 attendance=[make_attendance()])

    rc = main.run(dry_run=False)

    assert rc == 0
    assert len(env["emails"]) == 1
    assert len(env["larks"]) == 1
    assert len(env["saved"]) == 1  # 日报已落库

    report = env["emails"][0][0]
    assert report.date == REPORT_DATE
    assert report.team_name == "研发一组"
    # v1.3 结构：头部（含数据范围）→ 今日概览 → ▎成员 → ▪板块 → 落款
    assert report.markdown.startswith("# 📊 研发一组 智能日报")
    assert "📅 2026-09-29 周二" in report.markdown
    assert re.search(r"数据范围 \d{2}:\d{2}-\d{2}:\d{2}", report.markdown)  # window 已传入
    assert "## ▎今日概览" in report.markdown
    assert "## ▎仓库动态" in report.markdown  # v1.12：采集成功但无动态
    assert "今日无仓库动态" in report.markdown
    assert "## ▎张三" in report.markdown
    for title in ("代码提交", "任务进展", "工时统计"):
        assert f"### ▪ {title}" in report.markdown
    assert "### ▪ 协作沟通" not in report.markdown  # v1.12：已移除
    assert "feat: 登录页" in report.markdown
    assert "实现登录页：未开始 → 进行中" in report.markdown
    assert "评审文档已上传" not in report.markdown  # 消息默认关闭，不再渲染
    assert "🤖 由「智能日报生成器」自动生成" in report.markdown
    # 推送成功，无任何告警
    assert env["alerts_email"] == []
    assert env["alerts_lark"] == []


# ---------- 2. 降级场景 ----------

def test_degraded_source_shows_friendly_line(monkeypatch, env):
    """v1.4 修订：单源失败不阻断其他源；失败源板块保留并显示可读说明，
    原始原因不进正文（§6.1 的「不静默」由日志与告警保证）。"""
    stub_sources(monkeypatch,
                 tasks=[make_task()],
                 github_err="请求超时")

    rc = main.run(dry_run=False)

    assert rc == 0
    assert len(env["emails"]) == 1
    report = env["emails"][0][0]
    assert "▪ 代码提交" in report.markdown
    assert "· 飞书接口连接失败" in report.markdown
    assert "飞书接口连接失败" in report.html
    assert "请求超时" not in report.markdown
    # 其他数据源正常渲染
    assert "实现登录页：未开始 → 进行中" in report.markdown


# ---------- 3. 空数据场景 ----------

def test_empty_member_shows_no_records_today(monkeypatch, env):
    stub_sources(monkeypatch, commits=[make_commit()])  # 李四无任何记录

    rc = main.run(dry_run=False)

    assert rc == 0
    report = env["emails"][0][0]
    assert "今日无记录" in report.markdown  # 李四段落
    assert "feat: 登录页" in report.markdown  # 张三正常
    assert "## ▎李四" in report.markdown


# ---------- 4. 全部失败场景 ----------

def test_all_sources_failed_no_report(monkeypatch, env, caplog):
    # v1.12：消息默认关闭，不参与全失败判定；三个启用源全部失败即不生成日报
    stub_sources(monkeypatch, github_err="a", task_err="b", attendance_err="d")

    with caplog.at_level(logging.ERROR):
        rc = main.run(dry_run=False)

    assert rc == 1
    assert env["emails"] == []      # 不生成、不推送日报
    assert env["larks"] == []
    assert env["saved"] == []       # 不落库
    assert len(env["alerts_email"]) == 1  # 告警邮件（§6.1）
    assert "所有数据源均不可用" in caplog.text


# ---------- v1.1：考勤采集接入 ----------

def test_attendance_rendered_in_report(monkeypatch, env):
    stub_sources(monkeypatch, commits=[make_commit()],
                 attendance=[make_attendance()])

    rc = main.run(dry_run=False)

    assert rc == 0
    report = env["emails"][0][0]
    assert "### ▪ 工时统计" in report.markdown
    assert "工时 9 小时（正常）" in report.markdown
    # 张三任务无记录（1 条）+ 李四三个板块均无记录（3 条）；v1.12 移除消息板块
    assert report.markdown.count("· 今日无记录") == 4


def test_attendance_matched_via_resolved_employee_id(monkeypatch, env):
    """v1.13：通讯录回退 user_id 时，编排层仍能把考勤归属到成员。"""
    stub_sources(monkeypatch, commits=[make_commit()],
                 attendance=[make_attendance(employee_id="c9916d91")])
    monkeypatch.setattr(main.lark_attendance, "get_last_employee_ids",
                        lambda: {"zhangsan@company.com": "c9916d91"})

    rc = main.run(dry_run=False)

    assert rc == 0
    assert "工时 9 小时（正常）" in env["emails"][0][0].markdown


def test_attendance_failure_shows_friendly_and_logs(monkeypatch, env, caplog):
    """v1.4 修订：考勤源不可用 → 工时统计板块保留 + 可读说明；失败经日志显式记录。"""
    stub_sources(monkeypatch, commits=[make_commit()], attendance_err="考勤接口超时")

    with caplog.at_level(logging.ERROR):
        rc = main.run(dry_run=False)

    assert rc == 0
    report = env["emails"][0][0]
    assert "▪ 工时统计" in report.markdown
    assert "· 飞书接口连接失败" in report.markdown
    assert "飞书接口连接失败" in report.html
    assert "考勤接口超时" not in report.markdown
    assert "考勤数据暂不可用" not in report.markdown
    assert "飞书考勤采集失败" in caplog.text  # 异常经日志显式记录，未被吞掉
    # 其他数据源正常渲染，单源失败不阻断日报生成
    assert "feat: 登录页" in report.markdown


def test_unmapped_attendance_record_warns(monkeypatch, env, caplog):
    stub_sources(monkeypatch, commits=[make_commit()],
                 attendance=[make_attendance(employee_id="unknown@company.com")])

    with caplog.at_level(logging.WARNING):
        rc = main.run(dry_run=False)

    assert rc == 0
    assert "未映射到任何成员的考勤记录" in caplog.text
    assert "· 今日无记录" in env["emails"][0][0].markdown


# ---------- 5. 性能（Mock 环境） ----------

def test_execution_within_60_seconds(monkeypatch, env):
    stub_sources(monkeypatch,
                 commits=[make_commit()],
                 tasks=[make_task()],
                 messages=[make_message()])

    start = time.perf_counter()
    rc = main.run(dry_run=False)
    elapsed = time.perf_counter() - start

    assert rc == 0
    assert elapsed < 60


# ---------- 补充：推送失败互为备份告警、dry-run、执行日志 ----------

def test_push_failure_triggers_cross_channel_alert(monkeypatch, env):
    stub_sources(monkeypatch, commits=[make_commit()])
    monkeypatch.setattr(main.email_notifier, "send", lambda report, recipients: False)

    rc = main.run(dry_run=False)

    assert rc == 0
    assert len(env["alerts_lark"]) == 1  # 邮件失败 → 飞书告警（§6.1 备份通道）
    assert env["alerts_email"] == []     # 飞书推送成功，无反向告警


def test_lark_push_failure_triggers_email_alert(monkeypatch, env):
    """飞书已配置但发送失败 → 触发邮件备份告警（§6.1 备份通道）。"""
    stub_sources(monkeypatch, commits=[make_commit()])
    monkeypatch.setattr(main.lark_bot_notifier, "send", lambda report, chat_id: False)

    rc = main.run(dry_run=False)

    assert rc == 0
    assert len(env["alerts_email"]) == 1
    assert env["alerts_lark"] == []     # 邮件推送成功，无反向告警


def test_both_channels_fail_trigger_cross_alerts(monkeypatch, env):
    """两渠道均已配置且都失败 → 互为备份告警各一次。"""
    stub_sources(monkeypatch, commits=[make_commit()])
    monkeypatch.setattr(main.email_notifier, "send", lambda report, recipients: False)
    monkeypatch.setattr(main.lark_bot_notifier, "send", lambda report, chat_id: False)

    rc = main.run(dry_run=False)

    assert rc == 0
    assert len(env["alerts_email"]) == 1
    assert len(env["alerts_lark"]) == 1


# ---------- v1.2：未配置的推送渠道跳过（不推送、不告警） ----------

def test_unconfigured_channels_skip_push_and_alert(monkeypatch, env, caplog):
    """未配置的渠道不推送、不触发备份告警，仅记一条 INFO 日志。"""
    cfg = make_cfg()
    cfg["notifier"]["email"]["recipients"] = []
    cfg["notifier"].pop("lark_bot", None)
    monkeypatch.setattr(main, "load_config", lambda: cfg)
    monkeypatch.setenv("LARK_BOT_WEBHOOK_URL", "")
    stub_sources(monkeypatch, commits=[make_commit()])

    with caplog.at_level(logging.INFO):
        rc = main.run(dry_run=False)

    assert rc == 0
    assert env["emails"] == []       # 未配置：不推送
    assert env["larks"] == []
    assert env["alerts_email"] == []  # 未配置：不触发备份告警
    assert env["alerts_lark"] == []
    assert "邮件渠道未配置" in caplog.text
    assert "飞书渠道未配置" in caplog.text


def test_email_unconfigured_lark_configured_pushes_only_lark(monkeypatch, env, caplog):
    cfg = make_cfg()
    cfg["notifier"]["email"]["recipients"] = []
    monkeypatch.setattr(main, "load_config", lambda: cfg)
    stub_sources(monkeypatch, commits=[make_commit()])

    rc = main.run(dry_run=False)

    assert rc == 0
    assert env["emails"] == []
    assert len(env["larks"]) == 1   # 飞书已配置：正常推送
    assert env["alerts_email"] == []
    assert env["alerts_lark"] == []


def test_dry_run_skips_push_and_storage(monkeypatch, env):
    stub_sources(monkeypatch, commits=[make_commit()])

    rc = main.run(dry_run=True)

    assert rc == 0
    assert env["emails"] == []
    assert env["larks"] == []
    assert env["saved"] == []


def test_execution_logs_contain_required_fields(monkeypatch, env, caplog):
    stub_sources(monkeypatch, commits=[make_commit()])

    with caplog.at_level(logging.INFO):
        main.run(dry_run=False)

    text = caplog.text
    assert "日报流程开始" in text     # 开始时间
    assert "数据源采集完成" in text   # 各数据源采集条数
    assert "推送结果" in text         # 推送结果
    assert "日报流程结束" in text     # 结束时间


# ---------- §6.4 时区约定：采集窗口必须携带时区 ----------

def test_collection_windows_are_tz_aware(monkeypatch, env):
    windows: dict = {}

    cfg = make_cfg()
    cfg["collector"]["lark_msg"]["enabled"] = True  # v1.12：显式启用才采集消息
    monkeypatch.setattr(main, "load_config", lambda: cfg)

    def github_collect(repos, since, until):
        windows["github"] = (since, until)
        return []

    def repo_collect(repos, since, until):
        windows["github_activity"] = (since, until)
        return []

    def task_collect(project_id, since, until):
        windows["lark_task"] = (since, until)
        return []

    def msg_collect(chat_id, keywords, since, until):
        windows["lark_msg"] = (since, until)
        return []

    stub_sources(monkeypatch)
    monkeypatch.setattr(main.github, "collect", github_collect)
    monkeypatch.setattr(main.github_activity, "collect", repo_collect)
    monkeypatch.setattr(main.lark_task, "collect", task_collect)
    monkeypatch.setattr(main.lark_msg, "collect", msg_collect)

    rc = main.run(dry_run=False)

    assert rc == 0
    assert set(windows) == {"github", "github_activity", "lark_task", "lark_msg"}
    for since, until in windows.values():
        assert since.tzinfo is not None and since.utcoffset() is not None  # 禁止裸 naive datetime
        assert until.tzinfo is not None and until.utcoffset() is not None
        assert since < until
    # 窗口起点 = 本地时区当日 00:00（design.md §6.4："当日"指本地自然日）
    since = windows["github"][0]
    assert since.hour == 0 and since.minute == 0
    assert since == datetime.combine(REPORT_DATE, dtime.min, tzinfo=since.tzinfo)


# ---------- 告警兜底：真实实现不得崩溃（extra 禁用保留字段 "message"） ----------

def test_alert_email_logs_failure_without_crash(monkeypatch, caplog):
    """邮件告警本身失败仅记录日志，不抛异常（直接测 _alert_email 真实实现）。"""
    cfg = make_cfg()
    sent: list = []
    monkeypatch.setattr(main.email_notifier, "send",
                        lambda report, recipients: (sent.append((report, recipients)),
                                                    False)[1])
    msg = "邮件推送失败，请检查邮件配置"

    with caplog.at_level(logging.ERROR):
        main._alert_email(cfg, REPORT_DATE, msg)  # 修复前：KeyError 覆盖保留字段

    record = next(r for r in caplog.records if r.message == "告警邮件发送失败")
    assert record.levelno == logging.ERROR
    assert record.alert_message == msg  # extra 落在非保留字段，日志内容完整
    assert len(sent) == 1
    assert msg in sent[0][0].markdown    # 告警日报内容正确


def test_alert_lark_logs_failure_without_crash(monkeypatch, caplog):
    """飞书告警本身失败仅记录日志，不抛异常（直接测 _alert_lark 真实实现）。"""
    cfg = make_cfg()
    sent: list = []
    monkeypatch.setattr(main.lark_bot_notifier, "send",
                        lambda report, chat_id: (sent.append((report, chat_id)),
                                                 False)[1])
    msg = "飞书推送失败，请检查飞书机器人配置"

    with caplog.at_level(logging.ERROR):
        main._alert_lark(cfg, REPORT_DATE, msg)  # 修复前：KeyError 覆盖保留字段

    record = next(r for r in caplog.records if r.message == "飞书告警发送失败")
    assert record.levelno == logging.ERROR
    assert record.alert_message == msg  # extra 落在非保留字段，日志内容完整
    assert len(sent) == 1
    assert msg in sent[0][0].markdown    # 告警日报内容正确


# ---------- v1.3：未配置/占位值的源跳过采集，板块不渲染 ----------

def test_unconfigured_lark_sources_skipped_and_hidden(monkeypatch, env):
    """占位值配置的源跳过采集（不发无谓请求），日报中该板块不渲染。"""
    cfg = make_cfg()
    cfg["collector"]["lark_task"]["project_id"] = "your-lark-project-id"
    cfg["collector"]["lark_msg"]["chat_id"] = "oc_xxxxxxxx"
    monkeypatch.setattr(main, "load_config", lambda: cfg)
    calls = {"lark_task": 0, "lark_msg": 0, "lark_attendance": 0}
    monkeypatch.setattr(main.github, "collect", lambda repos, since, until: [make_commit()])
    monkeypatch.setattr(main.github, "get_last_error", lambda: None)
    monkeypatch.setattr(main.github_activity, "collect",
                        lambda repos, since, until: [])
    monkeypatch.setattr(main.lark_task, "collect",
                        lambda project_id, since, until: (calls.__setitem__("lark_task", 1), [])[1])
    monkeypatch.setattr(main.lark_task, "get_last_error", lambda: None)
    monkeypatch.setattr(main.lark_msg, "collect",
                        lambda chat_id, keywords, since, until: (calls.__setitem__("lark_msg", 1), [])[1])
    monkeypatch.setattr(main.lark_msg, "get_last_error", lambda: None)
    monkeypatch.setattr(main.lark_attendance, "collect",
                        lambda since, until: (calls.__setitem__("lark_attendance", 1),
                                              CollectResult(True, [], None))[1])

    rc = main.run(dry_run=False)

    assert rc == 0
    assert calls == {"lark_task": 0, "lark_msg": 0, "lark_attendance": 1}  # 占位源不发请求
    report = env["emails"][0][0]
    assert "▪ 代码提交" in report.markdown
    assert "▪ 任务进展" not in report.markdown
    assert "▪ 协作沟通" not in report.markdown  # v1.12：板块已移除
    assert "未接入" not in report.markdown
    assert "- · 任务进展" not in report.markdown          # 概览同样剔除未接入的源
    assert "- · 工时统计 0 人，共 0 小时" in report.markdown  # 已接入但为 0 仍显示
    assert "数据来源：GitHub、飞书考勤" in report.markdown  # 落款剔除未接入的源


def test_unconfigured_attendance_skipped_and_hidden(monkeypatch, env):
    """飞书凭据缺失时跳过考勤采集（不发请求），工时统计板块不渲染。"""
    monkeypatch.setenv("LARK_APP_ID", "")
    monkeypatch.setenv("LARK_APP_SECRET", "")
    calls = {"lark_attendance": 0}
    monkeypatch.setattr(main.lark_attendance, "collect",
                        lambda since, until: (calls.__setitem__("lark_attendance", 1),
                                              CollectResult(True, [], None))[1])
    stub_sources(monkeypatch, commits=[make_commit()])

    rc = main.run(dry_run=False)

    assert rc == 0
    assert calls["lark_attendance"] == 0  # 凭据缺失：不发请求
    report = env["emails"][0][0]
    assert "▪ 工时统计" not in report.markdown
    assert "考勤数据暂不可用" not in report.markdown
    assert "- · 工时统计" not in report.markdown  # 概览同样剔除未接入的源
    assert "数据来源：GitHub、飞书任务" in report.markdown
    assert "飞书消息" not in report.markdown  # v1.12：来源不再列出消息


# ---------- v1.6：趋势与周报（design.md §6.5） ----------

def test_today_stats_written_to_storage(monkeypatch, env):
    """生成日报后把当天统计写入 daily_stats（结构化字段，不解析 Markdown）。

    v1.12：messages_count 字段保留但固定写 0（协作沟通已移除）。
    """
    stub_sources(monkeypatch, commits=[make_commit()])

    rc = main.run(dry_run=False)

    assert rc == 0
    assert len(env["stats_saved"]) == 1
    stats_date, team, commits, additions, deletions, messages = env["stats_saved"][0]
    assert stats_date == REPORT_DATE
    assert team == "研发一组"
    assert commits == 1
    assert additions == 10
    assert deletions == 3
    assert messages == 0


def test_history_from_storage_feeds_week_and_yesterday(monkeypatch, env):
    """有历史统计时：本周累计（含较上周）与今日概览「较昨日」进入日报。"""
    env["stats_rows"] = [{
        "date": "2026-09-28", "team_name": "研发一组", "commits_count": 3,
        "additions": 30, "deletions": 9, "messages_count": 2,
    }]  # 昨日（周一）统计；今日 2026-09-29（周二）
    stub_sources(monkeypatch, commits=[make_commit()], messages=[make_message()])

    rc = main.run(dry_run=False)

    assert rc == 0
    report = env["emails"][0][0]
    assert "## ▎本周累计" in report.markdown
    assert report.markdown.index("## ▎本周累计") < report.markdown.index("## ▎今日概览")
    # 本周 = 昨日库内统计 + 当日内存统计
    assert "· 提交 4 次 ｜ 变更 +40 / -12 行" in report.markdown
    assert "· 活跃 2 天 ｜ 📈 较上周：提交 +4 次" in report.markdown  # 上周无数据
    assert "· 代码提交 1 次，共 +10 / -3 行 ｜ 📈 较昨日 -2 次" in report.markdown
    assert '<span class="stat-label">较昨日提交</span>' in report.html


def test_today_only_data_shows_week_summary(monkeypatch, env):
    """v1.6：历史为空但今天有数据 → 本周累计显示今日统计（活跃天数 ≥ 1），
    而非「暂无历史数据」（本周必须包含今天，design.md §6.5「周一 ~ 当日」）。"""
    stub_sources(monkeypatch, commits=[make_commit()], messages=[make_message()])

    rc = main.run(dry_run=False)

    assert rc == 0
    report = env["emails"][0][0]
    assert "· 提交 1 次 ｜ 变更 +10 / -3 行" in report.markdown
    assert "· 活跃 1 天 ｜ 📈 较上周：提交 +1 次" in report.markdown  # 上周无数据
    assert "暂无历史数据" not in report.markdown
    assert "较昨日" not in report.markdown  # 无昨日统计


def test_no_data_today_and_no_history_shows_placeholder(monkeypatch, env):
    """v1.6：历史为空且今天无任何数据 → 本周累计显示「暂无历史数据」，不报错。"""
    stub_sources(monkeypatch)  # 各源均正常但无数据

    rc = main.run(dry_run=False)

    assert rc == 0
    report = env["emails"][0][0]
    assert "## ▎本周累计" in report.markdown
    assert "· 暂无历史数据" in report.markdown
    assert "- · 近 7 天：暂无趋势数据" in report.markdown  # v1.12 占位
    assert "较昨日" not in report.markdown
    assert "暂无历史数据" in report.html  # HTML 同步显示占位文案


def test_last_week_covers_full_natural_week(monkeypatch, env):
    """v1.6：上周 = 完整的上一自然周（上周一 ~ 上周日），较上周按整周对比。"""
    env["stats_rows"] = [{
        "date": "2026-09-27", "team_name": "研发一组", "commits_count": 5,
        "additions": 50, "deletions": 10, "messages_count": 1,
    }]  # 上周日（上一自然周 2026-09-21 ~ 09-27 内）
    stub_sources(monkeypatch, commits=[make_commit()])

    rc = main.run(dry_run=False)

    assert rc == 0
    report = env["emails"][0][0]
    assert "· 提交 1 次 ｜ 变更 +10 / -3 行" in report.markdown  # 本周只有今天
    assert "· 活跃 1 天 ｜ 📈 较上周：提交 -4 次" in report.markdown  # 1 - 5


def test_history_read_failure_does_not_block_report(monkeypatch, env, caplog):
    """读取历史统计失败仅记日志，日报照常生成（history 传 None，v1.6 不阻断流程）。"""
    env["stats_read_error"] = True
    stub_sources(monkeypatch, commits=[make_commit()])

    with caplog.at_level(logging.ERROR):
        rc = main.run(dry_run=False)

    assert rc == 0
    assert len(env["emails"]) == 1
    assert "· 暂无历史数据" in env["emails"][0][0].markdown
    assert "读取历史统计失败" in caplog.text


# ---------- v1.9：未归属记录归集（design.md §6.5） ----------

def test_unassigned_records_become_other_contributions(monkeypatch, env, caplog):
    """未匹配到任何成员的提交/消息归入「其他贡献」成员段落，记录完整、不再丢弃。

    v1.12：消息采集需显式启用；消息仍参与归集与日志计数，但不再渲染到日报。
    """
    cfg = make_cfg()
    cfg["collector"]["lark_msg"]["enabled"] = True
    monkeypatch.setattr(main, "load_config", lambda: cfg)
    stub_sources(monkeypatch,
                 commits=[make_commit(),
                          make_commit(author="newbie", message="hotfix typo in report")],
                 messages=[make_message(),
                           make_message(sender="newbie", content="文案已修正")])

    with caplog.at_level(logging.INFO):
        rc = main.run(dry_run=False)

    assert rc == 0
    report = env["emails"][0][0]
    assert "## ▎其他贡献" in report.markdown
    # 伪成员段落排在所有成员之后
    assert report.markdown.index("## ▎李四") < report.markdown.index("## ▎其他贡献")
    block = report.markdown.split("## ▎其他贡献")[1]
    assert "hotfix typo in report" in block    # 未归属提交完整保留
    assert "文案已修正" not in report.markdown  # v1.12：消息不再渲染
    assert "▪ 代码提交" in block
    assert "▪ 协作沟通" not in block            # v1.12：板块已移除
    assert "▪ 任务进展" not in block            # 只渲染有数据的板块
    assert "▪ 工时统计" not in block            # 考勤不参与归集
    assert "今日无记录" not in block
    # 日志带计数，不再使用「已跳过」措辞
    record = next(r for r in caplog.records if r.message == "未归属记录归入「其他贡献」")
    assert record.commits == 1 and record.tasks == 0 and record.messages == 1
    assert "已跳过" not in caplog.text
    # 今日概览计入未归属记录（v1.12 起不再有协作沟通行）
    assert "· 代码提交 2 次，共 +20 / -6 行" in report.markdown
    assert "· 协作沟通" not in report.markdown


def test_no_unassigned_member_when_all_records_matched(monkeypatch, env, caplog):
    """全部记录都匹配到成员时，不生成「其他贡献」段落、不打归集日志。"""
    stub_sources(monkeypatch, commits=[make_commit()], tasks=[make_task()],
                 messages=[make_message()])

    with caplog.at_level(logging.INFO):
        rc = main.run(dry_run=False)

    assert rc == 0
    assert "其他贡献" not in env["emails"][0][0].markdown
    assert "未归属记录归入" not in caplog.text


def test_unassigned_attendance_only_logs_warning(monkeypatch, env, caplog):
    """只有未归属考勤：不生成「其他贡献」段落，仍打原有告警（人-日维度无法归属）。"""
    stub_sources(monkeypatch, commits=[make_commit()],
                 attendance=[make_attendance(employee_id="unknown@company.com")])

    with caplog.at_level(logging.WARNING):
        rc = main.run(dry_run=False)

    assert rc == 0
    assert "存在未映射到任何成员的考勤记录，已跳过" in caplog.text
    assert "其他贡献" not in env["emails"][0][0].markdown


# ---------- v1.12：仓库动态 / 近 7 天趋势 / 飞书消息默认关闭 ----------

def _repo_event(kind, number, title, hour=14):
    return RepoEvent(kind=kind, number=number, title=title,
                     timestamp=datetime(2026, 9, 29, hour, 0, tzinfo=TZ),
                     repo="org/repo", author="zhangsan",
                     url=f"https://github.com/org/repo/pull/{number}")


def test_lark_msg_disabled_by_default_skips_collection(monkeypatch, env, caplog):
    """v1.12：collector.lark_msg.enabled=false 时不调用采集，记录 INFO 日志。"""
    calls = {"lark_msg": 0}
    stub_sources(monkeypatch, commits=[make_commit()])

    def msg_collect(chat_id, keywords, since, until):
        calls["lark_msg"] += 1
        return []

    monkeypatch.setattr(main.lark_msg, "collect", msg_collect)

    with caplog.at_level(logging.INFO):
        rc = main.run(dry_run=False)

    assert rc == 0
    assert calls["lark_msg"] == 0
    assert "飞书消息采集未启用，跳过" in caplog.text
    report = env["emails"][0][0]
    assert "协作沟通" not in report.markdown
    assert "飞书消息" not in report.markdown


def test_lark_msg_enabled_still_collects_but_not_rendered(monkeypatch, env):
    """v1.12：采集代码保留——显式启用后正常采集，但展示层不再渲染消息。"""
    cfg = make_cfg()
    cfg["collector"]["lark_msg"]["enabled"] = True
    monkeypatch.setattr(main, "load_config", lambda: cfg)
    calls = {"count": 0}
    stub_sources(monkeypatch, commits=[make_commit()])

    def msg_collect(chat_id, keywords, since, until):
        calls["count"] += 1
        return [make_message()]

    monkeypatch.setattr(main.lark_msg, "collect", msg_collect)

    rc = main.run(dry_run=False)

    assert rc == 0
    assert calls["count"] == 1
    report = env["emails"][0][0]
    assert "评审文档已上传" not in report.markdown
    assert "协作沟通" not in report.markdown


def test_repo_events_rendered_in_report(monkeypatch, env):
    """v1.12：仓库动态进入日报（Markdown + HTML）。"""
    event = _repo_event("pr_merged", 4, "feat: 日报趋势展示")
    stub_sources(monkeypatch, commits=[make_commit()], repo_events=[event])

    rc = main.run(dry_run=False)

    assert rc == 0
    report = env["emails"][0][0]
    assert "## ▎仓库动态" in report.markdown
    assert "- · 合并 PR（1）" in report.markdown
    assert "  - #4 feat: 日报趋势展示（09-29）" in report.markdown
    assert "▎仓库动态" in report.html
    assert "feat: 日报趋势展示" in report.html


def test_repo_collection_failure_hides_section(monkeypatch, env, caplog):
    """v1.12：仓库动态采集失败不阻断日报，板块隐藏且原因只进日志。"""
    stub_sources(monkeypatch, commits=[make_commit()], repo_err="GitHub 限流")

    with caplog.at_level(logging.ERROR):
        rc = main.run(dry_run=False)

    assert rc == 0
    report = env["emails"][0][0]
    assert "仓库动态" not in report.markdown
    assert "GitHub 限流" not in report.markdown
    assert "github 仓库动态采集失败" in caplog.text
    assert "feat: 登录页" in report.markdown  # 其他数据源不受影响


def test_trend_built_from_stats_rows_with_zero_fill(monkeypatch, env):
    """v1.12/v1.14：近 7 天趋势取 daily_stats，缺失按 0 补齐；最后一天用本次采集的当日提交数。"""
    env["stats_rows"] = [
        {"date": "2026-09-23", "team_name": "研发一组", "commits_count": 1,
         "additions": 10, "deletions": 0, "messages_count": 0},
        {"date": "2026-09-25", "team_name": "研发一组", "commits_count": 3,
         "additions": 10, "deletions": 0, "messages_count": 0},
        {"date": "2026-09-26", "team_name": "研发一组", "commits_count": 2,
         "additions": 10, "deletions": 0, "messages_count": 0},
        {"date": "2026-09-28", "team_name": "研发一组", "commits_count": 4,
         "additions": 10, "deletions": 0, "messages_count": 0},
    ]
    stub_sources(monkeypatch, commits=[make_commit()])

    rc = main.run(dry_run=False)

    assert rc == 0
    report = env["emails"][0][0]
    # 09-23..09-29（含今天 1 次提交）→ 1,0,3,2,0,4,1；峰值 4
    assert "- · 近 7 天：▂▁▆▄▁█▂（峰值 4 次）" in report.markdown
    assert "▂▁▆▄▁█▂" in report.html
