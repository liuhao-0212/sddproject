#!/usr/bin/env python
"""依赖漏洞扫描（SCA）—— 读 uv.lock 的真实锁定版本，对照漏洞库。

为什么读 uv.lock 而不是 pyproject.toml：pyproject 只写范围（httpx>=0.27），
真正的构建结果由 lock 文件锁定。CI 里要扫"实际会装上的版本"。

真实项目请用 pip-audit / Trivy / OSV Scanner / Dependabot：
它们对接实时漏洞库，并能自动开升级 PR。这里内置一个极小的离线样例库，
让离线环境也能演示"扫到高危 -> 流水线拦下 -> 不允许打包"。

用法：
  python pipeline/tools/dep_scan.py --project .
退出码：0 = 通过，1 = 命中 fail-on 级别以上
"""

import argparse
import json
import os
import re
import sys

ADVISORIES = [
    {"package": "pyyaml", "fixed": "5.4", "cve": "CVE-2020-14343", "severity": "HIGH",
     "note": "yaml.load 反序列化可执行任意代码"},
    {"package": "jinja2", "fixed": "3.1.3", "cve": "CVE-2024-22195", "severity": "MEDIUM",
     "note": "xmlattr 过滤器可注入额外属性，导致 XSS"},
    {"package": "httpx", "fixed": "0.28.0", "cve": "CVE-2024-44413", "severity": "LOW",
     "note": "IDNA 处理不当可能导致 URL 校验绕过（教学样例）"},
    {"package": "requests", "fixed": "2.32.0", "cve": "CVE-2024-35195", "severity": "HIGH",
     "note": "Session 首次请求后 verify=False 会持续生效"},
    {"package": "urllib3", "fixed": "2.2.2", "cve": "CVE-2024-37891", "severity": "MEDIUM",
     "note": "代理场景下可能泄漏认证头"},
    {"package": "certifi", "fixed": "2024.7.4", "cve": "CVE-2024-39689", "severity": "MEDIUM",
     "note": "根证书被移除后仍被信任"},
    {"package": "idna", "fixed": "3.7", "cve": "CVE-2024-3651", "severity": "HIGH",
     "note": "恶意构造的 IDN 输入可造成 CPU 拒绝服务"},
]

SEVERITY_ORDER = {"LOW": 1, "MEDIUM": 2, "HIGH": 3}
NAME_LINE = re.compile(r'^\s*name\s*=\s*"([^"]+)"')
VERSION_LINE = re.compile(r'^\s*version\s*=\s*"([^"]+)"')


def version_tuple(text):
    """把 "3.1.6" 转成 (3, 1, 6) 便于比较。"""
    parts = re.findall(r"\d+", str(text) or "")
    return tuple(int(p) for p in parts) or (0,)


def parse_lock(lock_path):
    """从 uv.lock 里解析 [[package]] 的 name/version（逐行扫描，够用且零依赖）。"""
    packages = {}
    current = None
    in_package = False
    with open(lock_path, "r", encoding="utf-8") as handle:
        for line in handle:
            stripped = line.strip()
            if stripped.startswith("[["):
                if current and current.get("name") and current.get("version"):
                    packages[current["name"].lower()] = current["version"]
                in_package = stripped == "[[package]]"
                current = {} if in_package else None
                continue
            if not in_package or current is None:
                continue
            match = NAME_LINE.match(line)
            if match and "name" not in current:
                current["name"] = match.group(1)
                continue
            match = VERSION_LINE.match(line)
            if match and "version" not in current:
                current["version"] = match.group(1)
    if current and current.get("name") and current.get("version"):
        packages[current["name"].lower()] = current["version"]
    return packages


def main(argv=None):
    """命令行入口。"""
    parser = argparse.ArgumentParser(description="依赖漏洞扫描（读 uv.lock）")
    parser.add_argument("--project", default=".", help="项目根目录")
    parser.add_argument("--lock", default="uv.lock", help="锁文件路径")
    parser.add_argument("--fail-on", choices=["low", "medium", "high"], default="high",
                        help="达到该级别即判定失败")
    parser.add_argument("--json-out", help="结果写成 JSON")
    args = parser.parse_args(argv)

    project_dir = os.path.abspath(args.project)
    lock_path = os.path.join(project_dir, args.lock)
    if not os.path.isfile(lock_path):
        print("[dep] 找不到锁文件 %s：请先 uv lock（禁止用未锁定的依赖构建制品）" % lock_path)
        return 1

    packages = parse_lock(lock_path)
    findings = []
    for adv in ADVISORIES:
        version = packages.get(adv["package"])
        if version and version_tuple(version) < version_tuple(adv["fixed"]):
            findings.append((adv["severity"], "%s %s < %s 命中 %s：%s" % (
                adv["package"], version, adv["fixed"], adv["cve"], adv["note"])))

    print("[dep] 锁文件 %s 解析到 %d 个依赖" % (args.lock, len(packages)))
    print("[dep] 漏洞库 %d 条（离线样例，真实项目请用 pip-audit / Trivy / OSV）" % len(ADVISORIES))
    for severity, message in findings:
        print("[dep] %-6s %s" % (severity, message))

    threshold = SEVERITY_ORDER[args.fail_on.upper()]
    blocking = [f for f in findings if SEVERITY_ORDER[f[0]] >= threshold]
    if args.json_out:
        payload = {"packages": packages, "findings": findings, "blocking": len(blocking)}
        with open(args.json_out, "w", encoding="utf-8") as handle:
            json.dump(payload, handle, ensure_ascii=False, indent=2)

    if blocking:
        print("[dep] 结果：失败，%d 项达到 %s 及以上" % (len(blocking), args.fail_on.upper()))
        return 1
    print("[dep] 结果：通过（无 %s 及以上漏洞）" % args.fail_on.upper())
    return 0


if __name__ == "__main__":
    sys.exit(main())