"""查看日报内容（v1.1：含工时统计）。"""
import os
import sys
from datetime import date, datetime, time

import main
from collector import lark_attendance

# GBK 控制台（Windows）无法输出 emoji：显式切换 stdout 为 UTF-8（与 scripts/*.py 一致）
if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8")

# 采集窗口必须带时区（design.md §6.4）：naive 时间会被 GitHub 按其他时区解释，
# 导致当天的提交被排除。这里与 main.py 的窗口构造保持一致。
LOCAL_TZ = datetime.now().astimezone().tzinfo

cfg = main.load_config()
today = date.today()
since = datetime.combine(today, time.min, tzinfo=LOCAL_TZ)
until = datetime.now(LOCAL_TZ)

commits = main.github.collect(cfg["collector"]["github"]["repos"], since, until)
tasks = main.lark_task.collect(cfg["collector"]["lark_task"]["project_id"], since, until)
messages = main.lark_msg.collect(cfg["collector"]["lark_msg"]["chat_id"],
                                 cfg["collector"]["lark_msg"]["keywords"], since, until)

# v1.1：考勤（CollectResult）
att_result = lark_attendance.collect(today, today)
attendance = att_result.data if att_result.success else []

source_errors = {
    "github": main.github.get_last_error(),
    "lark_task": main.lark_task.get_last_error(),
    "lark_msg": main.lark_msg.get_last_error(),
    "lark_attendance": None if att_result.success else att_result.error,
}

members = main._aggregate(cfg, commits, tasks, messages, attendance, source_errors)
report = main.generate(members, today, cfg["team_name"], window=(since, until))

os.makedirs("output", exist_ok=True)
with open("output/demo-report.md", "w", encoding="utf-8") as f:
    f.write(report.markdown)
with open("output/demo-report.html", "w", encoding="utf-8") as f:
    f.write(report.html)

print("=== 日报 Markdown 内容 ===")
print(report.markdown)