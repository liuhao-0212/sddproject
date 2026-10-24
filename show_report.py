"""预览当前日报（v1.16）：采集真实数据生成 Markdown / HTML，不推送、不落库。

与 main._run 的采集与生成路径保持一致（含历史趋势与仓库动态），
用于发邮件/飞书前检查日报内容。
"""
import os
import sys
from datetime import date, datetime, time

import main
from collector import lark_attendance

# GBK 控制台（Windows）无法输出 emoji：显式切换 stdout 为 UTF-8
if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8")

# 采集窗口必须带时区（design.md §6.4）：与 main.py 的窗口构造保持一致
LOCAL_TZ = datetime.now().astimezone().tzinfo

cfg = main.load_config()
today = date.today()
since = datetime.combine(today, time.min, tzinfo=LOCAL_TZ)
until = datetime.now(LOCAL_TZ)

# 1) 采集（与 main._run 一致）
commits = main.github.collect(cfg["collector"]["github"]["repos"], since, until)

try:
    repo_events = main.github_activity.collect(cfg["collector"]["github"]["repos"],
                                               since, until)
except Exception as exc:  # 单源失败不阻断预览，板块隐藏
    repo_events = None
    print(f"[preview] 仓库动态采集失败（板块将隐藏）：{exc}")

tasks_collected = main._is_configured(cfg["collector"]["lark_task"]["project_id"])
tasks = (main.lark_task.collect(cfg["collector"]["lark_task"]["project_id"], since, until)
         if tasks_collected else [])

msg_cfg = cfg["collector"]["lark_msg"]
messages_enabled = bool(msg_cfg.get("enabled", False))
messages = (main.lark_msg.collect(msg_cfg["chat_id"], msg_cfg["keywords"], since, until)
            if messages_enabled else [])

att_result = lark_attendance.collect(today, today)
attendance = att_result.data if att_result.success else []

source_errors = {
    "github": main.github.get_last_error(),
    "lark_task": main.lark_task.get_last_error() if tasks_collected
                 else "飞书任务未配置（collector.lark_task.project_id 为空或占位值）",
    "lark_attendance": None if att_result.success else att_result.error,
}
if messages_enabled:
    source_errors["lark_msg"] = main.lark_msg.get_last_error()

# 2) 聚合 + 生成（含历史趋势与仓库动态，v1.16）
members = main._aggregate(cfg, commits, tasks, messages, attendance, source_errors)
history = main._load_history(cfg, today, members)
report = main.generate(members, today, cfg["team_name"], window=(since, until),
                       history=history, repo_events=repo_events)

# 3) 只写预览文件，不推送、不落库
os.makedirs("output", exist_ok=True)
with open(os.path.join("output", "demo-report.md"), "w", encoding="utf-8") as handle:
    handle.write(report.markdown)
with open(os.path.join("output", "demo-report.html"), "w", encoding="utf-8") as handle:
    handle.write(report.html)

print("=== 日报预览（未推送、未落库） ===")
print(report.markdown)
