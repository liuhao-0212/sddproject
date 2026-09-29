"""查看日报内容（v1.1：含工时统计）。"""
import os
from datetime import date, datetime, time

import main
from collector import lark_attendance

cfg = main.load_config()
today = date.today()
since = datetime.combine(today, time.min)
until = datetime.now()

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
report = main.generate(members, today, cfg["team_name"])

os.makedirs("output", exist_ok=True)
with open("output/demo-report.md", "w", encoding="utf-8") as f:
    f.write(report.markdown)
with open("output/demo-report.html", "w", encoding="utf-8") as f:
    f.write(report.html)

print("=== 日报 Markdown 内容 ===")
print(report.markdown)