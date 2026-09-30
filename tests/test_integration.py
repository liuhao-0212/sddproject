"""端到端集成测试：验证 main.py 完整流程在各场景下的行为（tasks.md Task 10）。

- 打桩采集/推送/存储模块（monkeypatch），零真实网络调用
- 聚合（_aggregate）与生成（generate）走真实实现，保证端到端语义
"""

import logging
import time
from datetime import date, datetime, time as dtime, timezone

import pytest

import generator.formatter as formatter
import main
from collector.github import CommitRecord
from collector.lark_attendance import AttendanceRecord, CollectResult
from collector.lark_msg import MessageRecord
from collector.lark_task import TaskRecord

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
            "lark_msg": {"chat_id": "oc_test", "keywords": ["评审"]},
        },
        "notifier": {"email": {"recipients": ["leader@company.com"]}},
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
                 attendance_err=None):
    """打桩采集模块：返回固定记录；v1.0 三个数据源经 get_last_error 注入失败原因，
    考勤经 CollectResult 注入（ADR-003）。"""
    monkeypatch.setattr(main.github, "collect",
                        lambda repos, since, until: list(commits or []))
    monkeypatch.setattr(main.github, "get_last_error", lambda: github_err)
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
    state = {"emails": [], "larks": [], "alerts_email": [], "alerts_lark": [], "saved": []}

    monkeypatch.setattr(main, "date", _FakeDate)
    monkeypatch.setattr(main, "load_config", make_cfg)
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

    monkeypatch.setattr(main, "Storage", FakeStorage)
    return state


# ---------- 1. 正常场景 ----------

def test_normal_flow_generates_report_and_pushes(monkeypatch, env):
    stub_sources(monkeypatch,
                 commits=[make_commit()],
                 tasks=[make_task()],
                 messages=[make_message()],
                 attendance=[make_attendance()])

    rc = main.run(dry_run=False)

    assert rc == 0
    assert len(env["emails"]) == 1
    assert len(env["larks"]) == 1
    assert len(env["saved"]) == 1  # 日报已落库

    report = env["emails"][0][0]
    assert report.date == REPORT_DATE
    assert report.team_name == "研发一组"
    assert "## 张三" in report.markdown
    for title in ("代码提交", "任务进展", "工时统计", "协作沟通"):
        assert f"### {title}" in report.markdown
    assert "feat: 登录页" in report.markdown
    assert "实现登录页：未开始 → 进行中" in report.markdown
    assert "[团队群] zhangsan@company.com：评审文档已上传" in report.markdown
    # 推送成功，无任何告警
    assert env["alerts_email"] == []
    assert env["alerts_lark"] == []


# ---------- 2. 降级场景 ----------

def test_degraded_source_timeout_is_annotated(monkeypatch, env):
    stub_sources(monkeypatch,
                 tasks=[make_task()],
                 messages=[make_message()],
                 github_err="请求超时")

    rc = main.run(dry_run=False)

    assert rc == 0
    assert len(env["emails"]) == 1
    report = env["emails"][0][0]
    assert "数据获取失败" in report.markdown
    assert "GitHub采集异常：请求超时" in report.markdown
    assert "<strong>数据获取失败</strong>" in report.html
    # 其他数据源正常渲染
    assert "实现登录页：未开始 → 进行中" in report.markdown
    assert "评审文档已上传" in report.markdown


# ---------- 3. 空数据场景 ----------

def test_empty_member_shows_no_records_today(monkeypatch, env):
    stub_sources(monkeypatch, commits=[make_commit()])  # 李四无任何记录

    rc = main.run(dry_run=False)

    assert rc == 0
    report = env["emails"][0][0]
    assert "今日无记录" in report.markdown  # 李四段落
    assert "feat: 登录页" in report.markdown  # 张三正常
    assert "## 李四" in report.markdown


# ---------- 4. 全部失败场景 ----------

def test_all_sources_failed_no_report(monkeypatch, env, caplog):
    stub_sources(monkeypatch, github_err="a", task_err="b", msg_err="c",
                 attendance_err="d")

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
    assert "### 工时统计" in report.markdown
    assert "签到 09:00 / 签退 18:00，工时 9 小时（正常）" in report.markdown
    # 无考勤记录的成员同样标注"考勤数据暂不可用"（李四）
    assert report.markdown.count("考勤数据暂不可用") == 1


def test_attendance_failure_shows_unavailable(monkeypatch, env, caplog):
    stub_sources(monkeypatch, commits=[make_commit()], attendance_err="考勤接口超时")

    with caplog.at_level(logging.ERROR):
        rc = main.run(dry_run=False)

    assert rc == 0
    report = env["emails"][0][0]
    assert "考勤数据暂不可用" in report.markdown
    assert "考勤数据暂不可用" in report.html
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
    assert "考勤数据暂不可用" in env["emails"][0][0].markdown


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

    def github_collect(repos, since, until):
        windows["github"] = (since, until)
        return []

    def task_collect(project_id, since, until):
        windows["lark_task"] = (since, until)
        return []

    def msg_collect(chat_id, keywords, since, until):
        windows["lark_msg"] = (since, until)
        return []

    stub_sources(monkeypatch)
    monkeypatch.setattr(main.github, "collect", github_collect)
    monkeypatch.setattr(main.lark_task, "collect", task_collect)
    monkeypatch.setattr(main.lark_msg, "collect", msg_collect)

    rc = main.run(dry_run=False)

    assert rc == 0
    assert set(windows) == {"github", "lark_task", "lark_msg"}
    for since, until in windows.values():
        assert since.tzinfo is not None and since.utcoffset() is not None  # 禁止裸 naive datetime
        assert until.tzinfo is not None and until.utcoffset() is not None
        assert since < until
    # 窗口起点 = 本地时区当日 00:00（design.md §6.4："当日"指本地自然日）
    since = windows["github"][0]
    assert since.hour == 0 and since.minute == 0
    assert since == datetime.combine(REPORT_DATE, dtime.min, tzinfo=since.tzinfo)
