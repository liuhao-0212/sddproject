#!/usr/bin/env python
"""部署后冒烟测试 —— 用固定数据集，跑"已经部署到环境里的那份代码"。

为什么不能用真实数据源：CI 里没有 GitHub/飞书的凭据，也不该依赖外网。
所以冒烟测试喂一份固定数据集（pipeline/fixtures/sample-day.json），
调用**部署目录里**的生成层，产出真实日报，再交给 report_check 做质量门禁。

这样既能验证"制品解压完整、依赖齐全、能跑通"，又是完全确定、可复现的。

用法：
  python pipeline/tools/report_smoke.py --app-dir environments/staging/releases/x --fixture ... --out ...
退出码：0 = 冒烟通过，1 = 冒烟失败
"""

import argparse
import json
import os
import sys
import traceback
from datetime import date, datetime


def build_members(payload, app_dir):
    """把 fixture 数据装配成生成层需要的对象（跨模块导入按部署目录解析）。"""
    sys.path.insert(0, app_dir)
    from collector.github import CommitRecord
    from collector.lark_attendance import AttendanceRecord
    from collector.lark_msg import MessageRecord
    from collector.lark_task import TaskRecord
    from generator.formatter import MemberReport

    def parse(value):
        return datetime.fromisoformat(value) if value else None

    members = []
    for raw in payload["members"]:
        attendance = raw.get("attendance")
        members.append(MemberReport(
            name=raw["name"],
            github_username=raw.get("github_username", ""),
            commits=[CommitRecord(author=c["author"], message=c["message"],
                                  timestamp=parse(c["timestamp"]), repo=c["repo"],
                                  additions=c.get("additions", 0),
                                  deletions=c.get("deletions", 0),
                                  files_changed=c.get("files_changed", 0))
                     for c in raw.get("commits", [])],
            tasks=[TaskRecord(assignee=t["assignee"], title=t["title"],
                              status_from=t["status_from"], status_to=t["status_to"],
                              updated_at=parse(t["updated_at"]))
                   for t in raw.get("tasks", [])],
            messages=[MessageRecord(sender=m["sender"], content=m["content"],
                                    timestamp=parse(m["timestamp"]), chat_name=m["chat_name"])
                      for m in raw.get("messages", [])],
            source_errors=dict(raw.get("source_errors", {})),
            attendance=AttendanceRecord(employee_id=attendance["employee_id"],
                                        date=date.fromisoformat(attendance["date"]),
                                        check_in=parse(attendance.get("check_in")),
                                        check_out=parse(attendance.get("check_out")),
                                        work_hours=attendance.get("work_hours", 0.0),
                                        status=attendance.get("status", "未知"))
            if attendance else None,
        ))
    return members


def main(argv=None):
    """命令行入口。"""
    parser = argparse.ArgumentParser(description="部署后冒烟测试（固定数据集）")
    parser.add_argument("--app-dir", required=True, help="已部署的发布目录（含 collector/ generator/）")
    parser.add_argument("--fixture", required=True, help="固定数据集 JSON")
    parser.add_argument("--out", required=True, help="日报输出目录")
    parser.add_argument("--json-out", help="冒烟结果写成 JSON")
    args = parser.parse_args(argv)

    app_dir = os.path.abspath(args.app_dir)
    out_dir = os.path.abspath(args.out)
    if not os.path.isfile(os.path.join(app_dir, "main.py")):
        print("[smoke] 部署目录不完整（缺少 main.py）：%s" % app_dir)
        return 1
    if not os.path.isfile(args.fixture):
        print("[smoke] 找不到数据集：%s" % args.fixture)
        return 1

    with open(args.fixture, "r", encoding="utf-8") as handle:
        payload = json.load(handle)

    print("[smoke] 目标环境目录：%s" % app_dir)
    print("[smoke] 数据集：%s（成员 %d 人）" % (args.fixture, len(payload["members"])))
    try:
        members = build_members(payload, app_dir)
        from generator.formatter import generate  # noqa: PLC0415 - 需在 sys.path 就绪后导入
    except Exception as exc:
        print("[smoke] FAIL 导入部署代码失败：%s" % exc)
        traceback.print_exc()
        return 1

    report_date = date.fromisoformat(payload["date"])
    window = (datetime.combine(report_date, datetime.min.time()),
              datetime.combine(report_date, datetime.max.time()))
    try:
        report = generate(members, report_date, payload["team_name"], window=window)
    except Exception as exc:
        print("[smoke] FAIL 生成日报抛异常：%s" % exc)
        traceback.print_exc()
        return 1

    os.makedirs(out_dir, exist_ok=True)
    base = "daily-report-%s" % report_date.isoformat()
    md_path = os.path.join(out_dir, base + ".md")
    html_path = os.path.join(out_dir, base + ".html")
    with open(md_path, "w", encoding="utf-8") as handle:
        handle.write(report.markdown)
    with open(html_path, "w", encoding="utf-8") as handle:
        handle.write(report.html)

    print("[smoke] PASS 生成层可用，产出 Markdown %d 字符 / HTML %d 字符"
          % (len(report.markdown), len(report.html)))
    print("[smoke] 日报：%s" % os.path.relpath(md_path))
    if args.json_out:
        with open(args.json_out, "w", encoding="utf-8") as handle:
            json.dump({"app_dir": app_dir, "markdown": md_path, "html": html_path,
                       "markdown_chars": len(report.markdown),
                       "html_chars": len(report.html),
                       "team_name": payload["team_name"], "date": payload["date"]},
                      handle, ensure_ascii=False, indent=2)
    return 0


if __name__ == "__main__":
    sys.exit(main())