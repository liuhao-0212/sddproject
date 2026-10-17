#!/usr/bin/env python
"""轻量静态检查 —— CI 里最便宜、反馈最快的一道门。

真实项目请用 ruff / flake8 / pylint / mypy（本站点建议 `uv add --dev ruff`）。
这里用 ast 实现 5 条最要命的规则，好处是零依赖、离线可跑、规则一眼看穿：

  L001 单行超过限制（默认 100 字符；中文注释按字符数算）
  L002 模块级公共函数/类缺少 docstring（Test* 与 _ 私有名豁免）
  L003 命名不合规：类 CamelCase，函数/方法 snake_case
  L004 裸 except（会吞掉 Ctrl-C 与致命异常）
  L005 可变默认参数（def f(x, items=[]) 在调用之间共享状态）

用法：
  python pipeline/tools/lint_check.py --project .
  python pipeline/tools/lint_check.py --project . --report-only   # 只报告，不阻断流水线
退出码：0 = 通过（或 --report-only），1 = 有违规
"""

import argparse
import ast
import os
import re
import sys

SNAKE = re.compile(r"^_?_?[a-z][a-z0-9_]*$")
CAMEL = re.compile(r"^_?[A-Z][A-Za-z0-9]*$")
SKIP_DIRS = {".git", "__pycache__", ".venv", "node_modules", ".pytest_cache", "dist",
             "environments", "reports", "data", "output", ".claude", ".vscode"}
DEFAULT_DESTS = ("collector", "generator", "notifier", "shared", "scripts", "main.py",
                 "show_report.py")


class Finding:
    """一条违规记录。"""

    def __init__(self, path, lineno, rule, message):
        self.path = path
        self.lineno = lineno
        self.rule = rule
        self.message = message

    def __str__(self):
        return "%s:%d: %s %s" % (self.path, self.lineno, self.rule, self.message)


def iter_python_files(targets):
    """把文件/目录参数展开成 .py 文件列表。"""
    for target in targets:
        if os.path.isfile(target):
            yield target
            continue
        for root, dirs, files in os.walk(target):
            dirs[:] = [d for d in dirs if d not in SKIP_DIRS]
            for name in sorted(files):
                if name.endswith(".py"):
                    yield os.path.join(root, name)


def check_lines(path, text, max_line):
    """L001：行长度。"""
    findings = []
    for i, line in enumerate(text.splitlines(), start=1):
        if len(line) > max_line:
            findings.append(Finding(path, i, "L001", "行长度 %d > %d" % (len(line), max_line)))
    return findings


def check_ast(path, tree):
    """L002 ~ L005：结构化检查。"""
    findings = []

    def report(lineno, rule, message):
        findings.append(Finding(path, lineno, rule, message))

    for node in ast.walk(tree):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            is_public = node.col_offset == 0 and not node.name.startswith(("_", "test_"))
            if is_public and ast.get_docstring(node) is None:
                report(node.lineno, "L002", "公共函数 %s() 缺少 docstring" % node.name)
            if not SNAKE.match(node.name):
                report(node.lineno, "L003", "函数名 %s 不是 snake_case" % node.name)
            kw_defaults = [d for d in node.args.kw_defaults if d is not None]
            for default in list(node.args.defaults) + kw_defaults:
                literal = isinstance(default, (ast.List, ast.Dict, ast.Set))
                call_name = getattr(default.func, "id", "") if isinstance(default, ast.Call) else ""
                if literal or call_name in ("list", "dict", "set"):
                    report(node.lineno, "L005", "可变默认参数（%s()）" % node.name)
        if isinstance(node, ast.ClassDef):
            is_module_level = node.col_offset == 0
            fixture = node.name.startswith(("Test", "_"))
            if is_module_level and not fixture and ast.get_docstring(node) is None:
                report(node.lineno, "L002", "公共类 %s 缺少 docstring" % node.name)
            if not CAMEL.match(node.name):
                report(node.lineno, "L003", "类名 %s 不是 CamelCase" % node.name)
        if isinstance(node, ast.ExceptHandler) and node.type is None:
            report(node.lineno, "L004", "裸 except，请显式捕获异常类型")
    return findings


def main(argv=None):
    """命令行入口。"""
    parser = argparse.ArgumentParser(description="轻量静态检查（CI 早期门禁）")
    parser.add_argument("--project", default=".", help="项目根目录")
    parser.add_argument("--max-line", type=int, default=100, help="单行最大字符数")
    parser.add_argument("--report-only", action="store_true", help="只报告不阻断（退出码恒为 0）")
    parser.add_argument("--json-out", help="结果写成 JSON")
    args = parser.parse_args(argv)

    project_dir = os.path.abspath(args.project)
    targets = [os.path.join(project_dir, name) for name in DEFAULT_DESTS]
    findings = []
    checked = 0
    by_rule = {}
    for path in iter_python_files(targets):
        checked += 1
        with open(path, "r", encoding="utf-8") as handle:
            text = handle.read()
        rel = os.path.relpath(path, project_dir)
        file_findings = check_lines(rel, text, args.max_line)
        try:
            tree = ast.parse(text, filename=rel)
        except SyntaxError as exc:
            findings.append(Finding(rel, exc.lineno or 0, "L000", "语法错误：%s" % exc.msg))
            continue
        file_findings.extend(check_ast(rel, tree))
        for item in file_findings:
            by_rule[item.rule] = by_rule.get(item.rule, 0) + 1
        findings.extend(file_findings)

    print("[lint] 检查 %d 个文件（规则 L001-L005，行宽上限 %d）" % (checked, args.max_line))
    for item in findings:
        print("[lint] %s" % item)
    print("[lint] 分布：%s" % (", ".join("%s×%d" % (k, v) for k, v in sorted(by_rule.items())) or "无"))
    if args.json_out:
        import json
        with open(args.json_out, "w", encoding="utf-8") as handle:
            json.dump({"checked": checked, "by_rule": by_rule,
                       "findings": [str(f) for f in findings]}, handle, ensure_ascii=False, indent=2)

    if findings and not args.report_only:
        print("[lint] 结果：失败，共 %d 条违规" % len(findings))
        return 1
    if findings:
        print("[lint] 结果：发现 %d 条待改进项（--report-only，不阻断流水线）" % len(findings))
        return 0
    print("[lint] 结果：通过")
    return 0


if __name__ == "__main__":
    sys.exit(main())