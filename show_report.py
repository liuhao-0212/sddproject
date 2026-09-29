import main
from datetime import date, datetime, time

cfg = main.load_config()
today = date.today()
since = datetime.combine(today, time.min)
until = datetime.now()

commits = main.github.collect(cfg["collector"]["github"]["repos"], since, until)
tasks = main.lark_task.collect(cfg["collector"]["lark_task"]["project_id"], since, until)
messages = main.lark_msg.collect(cfg["collector"]["lark_msg"]["chat_id"], cfg["collector"]["lark_msg"]["keywords"], since, until)
source_errors = {"github": main.github.get_last_error(),
                 "lark_task": main.lark_task.get_last_error(),
                 "lark_msg": main.lark_msg.get_last_error()}
members = main._aggregate(cfg, commits, tasks, messages, source_errors)
report = main.generate(members, today, cfg["team_name"])

import os
os.makedirs("output", exist_ok=True)
with open("output/demo-report.md", "w", encoding="utf-8") as f:
    f.write(report.markdown)
with open("output/demo-report.html", "w", encoding="utf-8") as f:
    f.write(report.html)
print("=== 日报 Markdown 内容 ===")
print(report.markdown)