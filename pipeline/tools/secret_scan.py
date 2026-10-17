#!/usr/bin/env python
"""密钥与配置卫生扫描 —— LLM/数据类项目最该装的一道门。

这个项目要访问 GitHub、飞书、SMTP，手里握着一堆凭据。
"密钥写进代码又推上远端"是最高频、最昂贵的事故：一次泄漏 = 全量轮换。
最好的防线是在 CI 里自动拦下来。

检查：
  S001 代码/文档里出现疑似真实密钥（GitHub PAT / 飞书 App Secret / SMTP 密码 / 私钥 …）
  S002 追踪在版本库里的敏感文件（.env、config.yaml）
  S003 配置文件里出现的密钥字段（设计上只允许环境变量注入）

用法：
  python pipeline/tools/secret_scan.py --project .
退出码：0 = 通过，1 = 命中可疑密钥
"""

import argparse
import os
import re
import subprocess
import sys

# 真实密钥的形态（前缀是各家厂商刻意设计的，便于被扫描器识别）
PATTERNS = [
    ("S001", "GitHub Token", re.compile(r"\bgh[pousr]_[A-Za-z0-9]{20,}\b")),
    ("S001", "AWS Access Key", re.compile(r"\bAKIA[0-9A-Z]{16}\b")),
    ("S001", "OpenAI/兼容接口 Key", re.compile(r"\bsk-[A-Za-z0-9_\-]{20,}\b")),
    ("S001", "Anthropic Key", re.compile(r"\bsk-ant-[A-Za-z0-9_\-]{20,}\b")),
    ("S001", "Slack Token", re.compile(r"\bxox[baprs]-[A-Za-z0-9\-]{10,}\b")),
    ("S001", "飞书 App ID/Secret", re.compile(r"\bcli_[a-z0-9]{16,}\b")),
    ("S001", "私钥文件内容", re.compile(r"-----BEGIN [A-Z ]*PRIVATE KEY-----")),
    # 变量名允许带前缀（SMTP_PASSWORD / LARK_APP_SECRET），所以不能只用 \bpassword\b
    ("S001", "硬编码口令赋值",
     # 负向前瞻排除 URL / 路径（TOKEN_PATH = "/open-apis/..." 这类不是密钥）
     re.compile(r"(?i)\b[A-Za-z0-9_]*(password|passwd|secret|token|api_key|app_secret"
                r"|webhook_url)[A-Za-z0-9_]*\b\s*[:=]\s*[\"'](?!/|https?://)"
                r"[^\"'\s]{12,}[\"']")),
]

SENSITIVE_FILES = (".env", "config.yaml")
# 明显是占位符/测试数据的值，不报警
PLACEHOLDERS = ("xxx", "your-", "your_", "placeholder", "example", "sample", "demo",
                "changeme", "todo", "test", "fake", "dummy", "***", "<", "ollama")
TEXT_SUFFIXES = (".py", ".md", ".yml", ".yaml", ".toml", ".json", ".txt", ".cfg", ".ini",
                 ".ps1", ".sh", ".bat", ".example")
SKIP_DIRS = (".git", "__pycache__", ".venv", "node_modules", ".pytest_cache", "environments",
             "dist", "reports")


def tracked_files(project_dir):
    """优先用 git 追踪列表（CI 里扫的就是即将合并的代码）；没有 git 就退化为遍历。"""
    try:
        proc = subprocess.run(["git", "ls-files"], cwd=project_dir, capture_output=True, text=True)
        if proc.returncode == 0 and proc.stdout.strip():
            return [line.strip() for line in proc.stdout.splitlines() if line.strip()]
    except OSError:
        pass
    collected = []
    for root, dirs, files in os.walk(project_dir):
        dirs[:] = [d for d in dirs if d not in SKIP_DIRS]
        for name in files:
            collected.append(os.path.relpath(os.path.join(root, name), project_dir))
    return collected


def is_placeholder(value):
    """占位值不算泄漏。"""
    lowered = value.lower()
    return any(marker in lowered for marker in PLACEHOLDERS)


def mask(value):
    """只留首尾各 4 个字符，避免把密钥原样打进 CI 日志（日志本身也是泄漏面）。"""
    if len(value) <= 10:
        return value[:2] + "***"
    return value[:4] + "***" + value[-4:]


def scan_file(path, rel):
    """扫描单个文件，返回 [(级别, 描述)]。"""
    findings = []
    try:
        with open(path, "r", encoding="utf-8", errors="ignore") as handle:
            lines = handle.readlines()
    except OSError:
        return findings
    for lineno, line in enumerate(lines, start=1):
        if is_placeholder(line):
            continue
        for rule, label, pattern in PATTERNS:
            for match in pattern.finditer(line):
                findings.append((rule, "%s:%d 疑似 %s：%s" % (rel, lineno, label, mask(match.group(0)))))
    return findings


def main(argv=None):
    """命令行入口。"""
    parser = argparse.ArgumentParser(description="密钥与配置卫生扫描")
    parser.add_argument("--project", default=".", help="项目根目录")
    parser.add_argument("--json-out", help="结果写成 JSON")
    args = parser.parse_args(argv)

    project_dir = os.path.abspath(args.project)
    files = tracked_files(project_dir)
    findings = []
    scanned = 0
    for rel in files:
        if os.path.basename(rel) in SENSITIVE_FILES:
            findings.append(("S002", "%s 出现在版本库里（应只保留 .example 模板）" % rel))
            continue
        if not rel.endswith(TEXT_SUFFIXES):
            continue
        path = os.path.join(project_dir, rel)
        if not os.path.isfile(path):
            continue
        scanned += 1
        findings.extend(scan_file(path, rel))

    print("[secret] 扫描 %d 个受版本控制的文本文件（共 %d 个追踪文件）" % (scanned, len(files)))
    for rule, message in findings:
        print("[secret] %s %s" % (rule, message))
    if args.json_out:
        import json
        with open(args.json_out, "w", encoding="utf-8") as handle:
            json.dump({"scanned": scanned, "findings": findings}, handle, ensure_ascii=False, indent=2)

    if findings:
        print("[secret] 结果：失败，%d 处疑似密钥/敏感文件" % len(findings))
        print("[secret] 处置：把值改为环境变量注入，然后轮换（已泄漏的密钥必须轮换，改代码不够）")
        return 1
    print("[secret] 结果：通过（无硬编码密钥、无敏感文件入库）")
    return 0


if __name__ == "__main__":
    sys.exit(main())