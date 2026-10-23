#!/usr/bin/env python
"""日报质量门禁 —— 生成物的"验收标准"，把规范里的排版/降级要求变成可执行断言。

SDD 项目里每一条需求都该有验收标准；日报这种"输出即产品"的系统，
验收标准就落在生成的 Markdown/HTML 上。这一步把 design.md 里的条款
（四段式、未接入不渲染、不可用必须显式标注、原因不进日报、时区统一）
翻译成机器能跑的检查。

真实项目里可以进一步做"快照对比"（把上一版日报作为黄金样本）。

用法：
  python pipeline/tools/report_check.py --markdown <文件> --html <文件> --expect-team 研发一组
退出码：0 = 通过，1 = 有失败项
"""

import argparse
import os
import re
import sys

# v1.12：协作沟通已从日报移除；仓库动态为可选板块（依赖仓库事件），不列为必备
SECTIONS = ("代码提交", "任务进展", "工时统计")
UNAVAILABLE_MARKERS = ("暂不可用", "不可用", "受限", "连接失败", "获取失败", "获取异常")
INTERNAL_LEAKS = ("凭据未配置", "未授权", "缺少飞书用户标识", "Traceback", "CollectorError",
                  "Error:", "Exception", "os.environ", "LARK_APP_SECRET")
PLACEHOLDERS = ("{{", "}}", "TODO", "{team_name}", "None", "nan", "xxx", "placeholder")


class Result:
    """一条检查结果。"""

    def __init__(self, rule, ok, message):
        self.rule = rule
        self.ok = ok
        self.message = message


def check_header(text, team, report_date):
    """K001 头部必须含团队名与日期。"""
    results = []
    if team and team in text:
        results.append(Result("K001", True, "头部含团队名「%s」" % team))
    else:
        results.append(Result("K001", False, "头部缺少团队名「%s」" % team))
    if report_date and report_date in text:
        results.append(Result("K001", True, "头部含日期 %s" % report_date))
    else:
        results.append(Result("K001", False, "头部缺少日期 %s（时区/格式可能有问题）" % report_date))
    return results


def check_sections(text):
    """K002 四段式板块名必须存在（至少出现一次）。"""
    return [Result("K002", name in text, "板块「%s」%s" % (name, "存在" if name in text else "缺失"))
            for name in SECTIONS]


def check_unavailable_marked(text):
    """K003 数据源不可用必须显式标注（design.md §6.1 禁止静默跳过）。"""
    if not re.search(r"(GitHub|飞书)", text):
        return [Result("K003", True, "本次无可降级数据源，跳过标注检查")]
    hit = [word for word in UNAVAILABLE_MARKERS if word in text]
    if hit:
        return [Result("K003", True, "不可用数据源已显式标注：%s" % "、".join(hit))]
    return [Result("K003", False, "未发现任何「数据源不可用」的可读标注，疑似静默跳过")]


def check_no_internal_leak(text):
    """K004 内部术语/异常文本不得出现在读者可见的日报里。"""
    leaks = [word for word in INTERNAL_LEAKS if word in text]
    if leaks:
        return [Result("K004", False, "日报泄漏内部细节：%s" % "、".join(leaks))]
    return [Result("K004", True, "无内部术语/异常文本泄漏")]


def check_no_placeholder(text):
    """K005 不得出现未替换的模板占位符。"""
    hits = [word for word in PLACEHOLDERS if word in text]
    if hits:
        return [Result("K005", False, "存在未替换占位符：%s" % "、".join(hits))]
    return [Result("K005", True, "无未替换占位符")]


def check_size(text, min_chars):
    """K006 体量下限：太短通常意味着某个环节被跳过。"""
    size = len(text)
    return [Result("K006", size >= min_chars, "日报长度 %d 字符（下限 %d）" % (size, min_chars))]


def main(argv=None):
    """命令行入口。"""
    parser = argparse.ArgumentParser(description="日报质量门禁")
    parser.add_argument("--markdown", required=True, help="要检查的 Markdown 日报")
    parser.add_argument("--html", help="同时检查 HTML 日报")
    parser.add_argument("--expect-team", default="", help="期望出现的团队名")
    parser.add_argument("--expect-date", default="", help="期望出现的日期（YYYY-MM-DD）")
    parser.add_argument("--min-chars", type=int, default=400, help="Markdown 最小字符数")
    parser.add_argument("--json-out", help="结果写成 JSON")
    args = parser.parse_args(argv)

    if not os.path.isfile(args.markdown):
        print("[check] 找不到日报文件：%s" % args.markdown)
        return 1
    with open(args.markdown, "r", encoding="utf-8") as handle:
        markdown = handle.read()

    results = []
    results.extend(check_header(markdown, args.expect_team, args.expect_date))
    results.extend(check_sections(markdown))
    results.extend(check_unavailable_marked(markdown))
    results.extend(check_no_internal_leak(markdown))
    results.extend(check_no_placeholder(markdown))
    results.extend(check_size(markdown, args.min_chars))

    if args.html:
        if os.path.isfile(args.html):
            with open(args.html, "r", encoding="utf-8") as handle:
                html = handle.read()
            ok = "<html" in html.lower() and "</html>" in html.lower()
            results.append(Result("K007", ok, "HTML 日报结构完整（%d 字符）" % len(html)))
        else:
            results.append(Result("K007", False, "HTML 日报不存在：%s" % args.html))

    for item in results:
        print("[check] %-4s %s %s" % ("PASS" if item.ok else "FAIL", item.rule, item.message))
    if args.json_out:
        import json
        with open(args.json_out, "w", encoding="utf-8") as handle:
            json.dump([{"rule": r.rule, "ok": r.ok, "message": r.message} for r in results],
                      handle, ensure_ascii=False, indent=2)

    failed = [r for r in results if not r.ok]
    if failed:
        print("[check] 结果：失败 %d 项 —— 日报不达标，不允许归档/推送" % len(failed))
        return 1
    print("[check] 结果：通过（%d 项检查全部达标）" % len(results))
    return 0


if __name__ == "__main__":
    sys.exit(main())