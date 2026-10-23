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
from datetime import date, datetime, timedelta


def build_members(payload, app_dir):
    """把 fixture 数据装配成生成层需要的对象（跨模块导入按部署目录解析）。

    v1.9：记录按身份路由到成员（对齐 main._aggregate 的匹配口径——提交按
    github_username，任务/消息按成员姓名或 github_username，夹具无 lark 字段），
    未匹配到任何成员的记录归入「其他贡献」伪成员（is_unassigned=True），
    不再挂在条目所属成员名下；考勤不参与归集，保留在其所属成员条目。
    """
    sys.path.insert(0, app_dir)
    from collector.github import CommitRecord
    from collector.lark_attendance import AttendanceRecord
    from collector.lark_msg import MessageRecord
    from collector.lark_task import TaskRecord
    from generator.formatter import UNASSIGNED_MEMBER_NAME, MemberReport

    def parse(value):
        return datetime.fromisoformat(value) if value else None

    def build_commit(c):
        return CommitRecord(author=c["author"], message=c["message"],
                            timestamp=parse(c["timestamp"]), repo=c["repo"],
                            additions=c.get("additions", 0),
                            deletions=c.get("deletions", 0),
                            files_changed=c.get("files_changed", 0))

    def build_task(t):
        return TaskRecord(assignee=t["assignee"], title=t["title"],
                          status_from=t["status_from"], status_to=t["status_to"],
                          updated_at=parse(t["updated_at"]))

    def build_message(m):
        return MessageRecord(sender=m["sender"], content=m["content"],
                             timestamp=parse(m["timestamp"]), chat_name=m["chat_name"])

    raw_members = payload["members"]

    def owner(value, github_only):
        """记录归属的成员条目；未匹配返回 None。
        github_only=True 时只按 github_username 匹配（提交），
        否则按姓名或 github_username 匹配（任务/消息）。"""
        for raw in raw_members:
            if github_only:
                if value and value == raw.get("github_username", ""):
                    return raw
            elif value == raw["name"] or value == raw.get("github_username", ""):
                return raw
        return None

    # v1.9：按身份路由各条记录；未匹配任何成员的进入未归属集合
    routed = {raw["name"]: {"commits": [], "tasks": [], "messages": []}
              for raw in raw_members}
    unassigned = {"commits": [], "tasks": [], "messages": []}
    for raw in raw_members:
        for c in raw.get("commits", []):
            target = owner(c["author"], github_only=True)
            bucket = routed[target["name"]]["commits"] if target else unassigned["commits"]
            bucket.append(build_commit(c))
        for t in raw.get("tasks", []):
            target = owner(t["assignee"], github_only=False)
            bucket = routed[target["name"]]["tasks"] if target else unassigned["tasks"]
            bucket.append(build_task(t))
        for m in raw.get("messages", []):
            target = owner(m["sender"], github_only=False)
            bucket = routed[target["name"]]["messages"] if target else unassigned["messages"]
            bucket.append(build_message(m))

    members = []
    for raw in raw_members:
        attendance = raw.get("attendance")
        members.append(MemberReport(
            name=raw["name"],
            github_username=raw.get("github_username", ""),
            commits=routed[raw["name"]]["commits"],
            tasks=routed[raw["name"]]["tasks"],
            messages=routed[raw["name"]]["messages"],
            source_errors=dict(raw.get("source_errors", {})),
            attendance=AttendanceRecord(employee_id=attendance["employee_id"],
                                        date=date.fromisoformat(attendance["date"]),
                                        check_in=parse(attendance.get("check_in")),
                                        check_out=parse(attendance.get("check_out")),
                                        work_hours=attendance.get("work_hours", 0.0),
                                        status=attendance.get("status", "未知"))
            if attendance else None,
        ))
    if unassigned["commits"] or unassigned["tasks"] or unassigned["messages"]:
        members.append(MemberReport(
            name=UNASSIGNED_MEMBER_NAME,
            github_username="",
            commits=unassigned["commits"],
            tasks=unassigned["tasks"],
            messages=unassigned["messages"],
            source_errors={},
            attendance=None,
            is_unassigned=True,
        ))
    return members


def build_repo_events(payload, app_dir):
    """把 fixture 的 repo_events 装配成生成层对象（v1.12）。

    缺失该字段时返回 None——生成层据此不渲染「仓库动态」板块（保持原行为）；
    空列表表示已采集但当天无动态（显示「今日无仓库动态」）。
    """
    raw_events = payload.get("repo_events")
    if raw_events is None:
        return None
    sys.path.insert(0, app_dir)
    from collector.github_activity import RepoEvent

    return [
        RepoEvent(
            kind=raw["kind"],
            number=raw.get("number"),
            title=raw.get("title", ""),
            timestamp=datetime.fromisoformat(raw["timestamp"]),
            repo=raw.get("repo", ""),
            author=raw.get("author", ""),
            url=raw.get("url", ""),
        )
        for raw in raw_events
    ]


def build_history(payload, report_date, members):
    """把 fixture 的 trend 装配成生成层 history（v1.12）。

    缺失 trend 时返回 None——本周概况显示「暂无历史数据 / 暂无趋势数据」（原行为）；
    trend 支持 [1, 0, 3, ...] 与 [{"date": "...", "commits": n}] 两种写法。
    """
    raw_trend = payload.get("trend")
    if raw_trend is None:
        return None
    week_start = report_date - timedelta(days=report_date.weekday())
    commits = sum(len(member.commits) for member in members)
    history = {
        "week": {
            "start": week_start,
            "end": report_date,
            "commits": commits,
            "additions": sum(c.additions for m in members for c in m.commits),
            "deletions": sum(c.deletions for m in members for c in m.commits),
            "active_days": 1 if commits else 0,
        },
        "last_week": {"start": week_start - timedelta(days=7),
                      "end": week_start - timedelta(days=1),
                      "commits": 0, "additions": 0, "deletions": 0, "active_days": 0},
        "yesterday": None,
        "trend": [],
    }
    total = len(raw_trend)
    for index, item in enumerate(raw_trend):
        if isinstance(item, dict):
            day = item.get("date") or (report_date - timedelta(days=total - 1 - index)).isoformat()
            value = item.get("commits", 0)
        else:
            day = (report_date - timedelta(days=total - 1 - index)).isoformat()
            value = item
        history["trend"].append({"date": str(day), "commits": int(value)})
    return history


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
        repo_events = build_repo_events(payload, app_dir)
        from generator.formatter import generate  # noqa: PLC0415 - 需在 sys.path 就绪后导入
    except Exception as exc:
        print("[smoke] FAIL 导入部署代码失败：%s" % exc)
        traceback.print_exc()
        return 1

    report_date = date.fromisoformat(payload["date"])
    window = (datetime.combine(report_date, datetime.min.time()),
              datetime.combine(report_date, datetime.max.time()))
    history = build_history(payload, report_date, members)
    try:
        report = generate(members, report_date, payload["team_name"], window=window,
                          history=history, repo_events=repo_events)
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