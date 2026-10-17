#!/usr/bin/env python
"""环境自检 —— 流水线的第 2 步，也是最容易被忽略的一步。

CI 里最常见的"玄学失败"都来自环境：解释器版本不对、依赖没装、
本地配置缺失、密钥文件被误提交。这一步在跑测试之前把它们问清楚，
把"看不懂的报错"变成"一句话结论"。

检查项：
  E001 解释器版本是否满足 pyproject.toml 的 requires-python
  E002 运行期依赖是否可导入（httpx / yaml / jinja2）
  E003 测试框架是否可用（pytest）
  E004 config.yaml 是否存在、可解析、关键字段齐全
  E005 .env / config.yaml 是否被 .gitignore 覆盖（密钥不许进仓库）

用法：
  python pipeline/tools/env_check.py --project .
  python pipeline/tools/env_check.py --project . --allow-version-mismatch
退出码：0 = 通过（允许 WARN），1 = 有 FAIL
"""

import argparse
import importlib
import os
import re
import subprocess
import sys

REQUIRES_PYTHON = re.compile(r'requires-python\s*=\s*"\s*>=\s*(\d+)\.(\d+)')
RUN_DEPENDENCIES = ("httpx", "yaml", "jinja2")
CONFIG_FIELDS = ("team_name", "members", "collector", "notifier", "storage")


def check_python_version(project_dir, allow_mismatch):
    """E001：解释器版本 vs 项目声明。"""
    pyproject = os.path.join(project_dir, "pyproject.toml")
    if not os.path.isfile(pyproject):
        return "FAIL", "E001 找不到 pyproject.toml，无法确定 requires-python"
    with open(pyproject, "r", encoding="utf-8") as handle:
        match = REQUIRES_PYTHON.search(handle.read())
    if not match:
        return "WARN", "E001 pyproject.toml 未声明 requires-python，建议补上"
    wanted = (int(match.group(1)), int(match.group(2)))
    current = sys.version_info[:2]
    if current >= wanted:
        return "PASS", "E001 解释器 %d.%d 满足 requires-python >= %d.%d" % (
            current[0], current[1], wanted[0], wanted[1])
    detail = "E001 解释器 %d.%d 低于 requires-python >= %d.%d" % (
        current[0], current[1], wanted[0], wanted[1])
    if allow_mismatch:
        return "WARN", detail + "（--allow-version-mismatch：本次仅用于教学演示）"
    return "FAIL", detail + "：请用 uv sync 创建的项目环境运行，否则会出现假失败"


def check_imports():
    """E002/E003：依赖与测试框架可导入性。"""
    results = []
    missing = [name for name in RUN_DEPENDENCIES if not _importable(name)]
    if missing:
        results.append(("FAIL", "E002 运行期依赖缺失：%s（先执行 uv sync）" % ", ".join(missing)))
    else:
        results.append(("PASS", "E002 运行期依赖齐全：%s" % ", ".join(RUN_DEPENDENCIES)))
    if _importable("pytest"):
        version = importlib.import_module("pytest").__version__
        results.append(("PASS", "E003 pytest 可用（%s）" % version))
    else:
        results.append(("FAIL", "E003 pytest 不可用：无法执行测试阶段"))
    return results


def _importable(name):
    try:
        importlib.import_module(name)
    except Exception:
        return False
    return True


def check_config(project_dir):
    """E004：本地配置是否存在且结构完整。"""
    path = os.path.join(project_dir, "config.yaml")
    example = os.path.join(project_dir, "config.yaml.example")
    if not os.path.isfile(path):
        hint = "cat config.yaml.example > config.yaml" if os.path.isfile(example) else "缺少模板"
        return "FAIL", "E004 缺少 config.yaml（本地配置不入库，需要自己生成：%s）" % hint
    try:
        data = importlib.import_module("yaml").safe_load(open(path, "r", encoding="utf-8"))
    except Exception as exc:
        return "FAIL", "E004 config.yaml 解析失败：%s" % exc
    if not isinstance(data, dict):
        return "FAIL", "E004 config.yaml 顶层不是映射结构"
    missing = [key for key in CONFIG_FIELDS if key not in data]
    if missing:
        return "FAIL", "E004 config.yaml 缺少字段：%s" % ", ".join(missing)
    members = data.get("members") or []
    return "PASS", "E004 config.yaml 可用（团队 %s，成员 %d 人）" % (data.get("team_name"), len(members))


def check_secret_ignored(project_dir):
    """E005：密钥/本地配置必须被 .gitignore 覆盖。"""
    problems = []
    for name in (".env", "config.yaml"):
        path = os.path.join(project_dir, name)
        if not os.path.exists(path):
            continue
        try:
            proc = subprocess.run(["git", "check-ignore", "-q", name], cwd=project_dir,
                                  capture_output=True)
        except OSError:
            return "WARN", "E005 本机没有 git，跳过密钥文件忽略检查"
        if proc.returncode != 0:
            problems.append(name)
    if problems:
        return "FAIL", "E005 以下文件未被 .gitignore 忽略，有泄漏风险：%s" % ", ".join(problems)
    return "PASS", "E005 .env 与 config.yaml 已被 .gitignore 覆盖"


def main(argv=None):
    """命令行入口。"""
    parser = argparse.ArgumentParser(description="环境与依赖自检（CI 第 2 步）")
    parser.add_argument("--project", default=".", help="项目根目录")
    parser.add_argument("--allow-version-mismatch", action="store_true",
                        help="版本不匹配降级为 WARN（仅教学演示，真实 CI 不要用）")
    parser.add_argument("--json-out", help="结果写成 JSON")
    args = parser.parse_args(argv)

    project_dir = os.path.abspath(args.project)
    checks = [check_python_version(project_dir, args.allow_version_mismatch)]
    checks.extend(check_imports())
    checks.append(check_config(project_dir))
    checks.append(check_secret_ignored(project_dir))

    print("[env] 项目目录 %s" % project_dir)
    print("[env] 解释器   %s (%d.%d.%d)" % (sys.executable, *sys.version_info[:3]))
    for level, message in checks:
        print("[env] %-4s %s" % (level, message))

    if args.json_out:
        import json
        with open(args.json_out, "w", encoding="utf-8") as handle:
            json.dump([{"level": lv, "message": msg} for lv, msg in checks], handle,
                      ensure_ascii=False, indent=2)

    failed = [msg for lv, msg in checks if lv == "FAIL"]
    if failed:
        print("[env] 结果：失败，%d 项亟待修复" % len(failed))
        return 1
    print("[env] 结果：通过")
    return 0


if __name__ == "__main__":
    sys.exit(main())