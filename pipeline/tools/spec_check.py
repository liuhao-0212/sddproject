#!/usr/bin/env python
"""规范完整性检查（本地版）—— 把 .github/workflows/spec-check.yml 的规则搬到本地。

SDD 项目的 CI 第一条不是跑测试，而是守住规范：三份规范文件必须在、
proposal 必须有"不做"章节、改了代码要同步规范。云端跑在 PR 上，
本地版让同一条规则在提交前就能跑一遍（左移）。

用法：
  python pipeline/tools/spec_check.py --project .
退出码：0 = 通过（允许 WARN），1 = 有硬错误
"""

import argparse
import os
import re
import subprocess
import sys

SPEC_FILES = ("specs/proposal.md", "specs/design.md", "specs/tasks.md")


def check_spec_files(project_dir):
    """R1：三份规范文件必须存在。"""
    findings = []
    for rel in SPEC_FILES:
        if os.path.isfile(os.path.join(project_dir, rel)):
            findings.append(("PASS", "R1 规范文件存在：%s" % rel))
        else:
            findings.append(("FAIL", "R1 缺少规范文件：%s" % rel))
    return findings


def check_proposal_scope(project_dir):
    """R2：proposal 必须写明"不做"（范围边界，SDD 的核心机制）。"""
    path = os.path.join(project_dir, "specs/proposal.md")
    if not os.path.isfile(path):
        return [("FAIL", "R2 proposal.md 不存在，无法检查范围边界")]
    with open(path, "r", encoding="utf-8") as handle:
        text = handle.read()
    if re.search(r"^#+.*不做", text, re.MULTILINE):
        return [("PASS", "R2 proposal.md 含「不做」章节")]
    return [("FAIL", "R2 proposal.md 缺少「不做」章节：范围没有边界，AI 会自由发挥")]


def check_task_acceptance(project_dir):
    """R3：每个 Task 都应有验收标准（警告级）。"""
    path = os.path.join(project_dir, "specs/tasks.md")
    if not os.path.isfile(path):
        return [("WARN", "R3 tasks.md 不存在，跳过验收标准检查")]
    with open(path, "r", encoding="utf-8") as handle:
        text = handle.read()
    tasks = len(re.findall(r"^##+ Task", text, re.MULTILINE))
    accepts = text.count("验收")
    if tasks and tasks > accepts:
        return [("WARN", "R3 任务数 %d 多于「验收」出现次数 %d，请确认每个任务都有验收标准"
                 % (tasks, accepts))]
    return [("PASS", "R3 %d 个任务均定义了验收标准" % tasks)]


def check_spec_sync(project_dir, base_ref):
    """R4：代码改了但规范没动（警告级，需要 git 历史）。"""
    try:
        proc = subprocess.run(["git", "diff", "--name-only", base_ref, "HEAD"],
                              cwd=project_dir, capture_output=True, text=True)
    except OSError:
        return [("WARN", "R4 本机没有 git，跳过「代码与规范同步」检查")]
    if proc.returncode != 0:
        return [("WARN", "R4 无法对比 %s..HEAD（浅克隆或缺基准提交）" % base_ref)]
    changed = [line for line in proc.stdout.splitlines() if line.strip()]
    code = [f for f in changed if f.endswith(".py")]
    spec = [f for f in changed if f.startswith("specs/")]
    if code and not spec:
        return [("WARN", "R4 本次改动含 %d 个 .py 但未改 specs/，请确认规范已同步" % len(code))]
    return [("PASS", "R4 代码与规范同步（改动 %d 个文件）" % len(changed))]


def main(argv=None):
    """命令行入口。"""
    parser = argparse.ArgumentParser(description="规范完整性检查（本地版）")
    parser.add_argument("--project", default=".", help="项目根目录")
    parser.add_argument("--base-ref", default="HEAD~1", help="对比基准提交")
    args = parser.parse_args(argv)

    project_dir = os.path.abspath(args.project)
    findings = []
    findings.extend(check_spec_files(project_dir))
    findings.extend(check_proposal_scope(project_dir))
    findings.extend(check_task_acceptance(project_dir))
    findings.extend(check_spec_sync(project_dir, args.base_ref))

    print("[spec] 项目目录 %s" % project_dir)
    for level, message in findings:
        print("[spec] %-4s %s" % (level, message))
    hard = [m for lv, m in findings if lv == "FAIL"]
    if hard:
        print("[spec] 结果：失败，硬错误 %d 个" % len(hard))
        return 1
    print("[spec] 结果：通过")
    return 0


if __name__ == "__main__":
    sys.exit(main())