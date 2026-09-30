"""Claude Code PreToolUse Bash 护栏（由 .claude/settings.json 配置）。

从 stdin 读取 hook JSON（tool_input 含 command），检查命令是否试图通过
shell 写入 specs/ 目录；违规时输出 {"decision":"block","reason":...} 并以
退出码 2 中止工具调用，纯读取类命令与不涉及 specs/ 的命令放行。

规则（锚定规范）：
1. specs/ 目录：规范只能由人类修改，经 shell 的一切写入途径都拦
   （重定向 > >>、tee、sed -i、python/perl 等解释器、mv/cp/rm、
   truncate、dd 等），封堵「用 Bash 跑脚本绕过 Edit|Write 护栏」的路径
2. 纯读取类命令（cat/grep/rg/head/tail/ls/wc/diff/stat）放行，避免误伤
3. 不涉及 specs/ 的命令一律放行
"""

from __future__ import annotations

import json
import re
import sys

REASON = "禁止通过 shell 写入 specs/ 目录（规范只能由人类修改）"

# 纯读取类命令：作为命令行首个单词（跳过 sudo）出现且无任何写入迹象时直接放行
READ_COMMANDS = {"cat", "grep", "rg", "head", "tail", "ls", "wc", "diff", "stat"}

# 通用解释器：可自行读写任意文件，涉及 specs/ 时一律视为写入迹象
_INTERPRETERS = (
    r"\bpy(?:thon\d*)?(?:\.exe)?\b",  # py / python / python3 / python.exe
    r"\bperl(?:\.exe)?\b",
    r"\bnode(?:\.exe)?\b",
    r"\b(?:pwsh|powershell)(?:\.exe)?\b",
)

# 写/删/改文件命令（允许 sudo 前缀；含 Windows cmd 风格命令）
_WRITE_COMMANDS = (
    r"\b(?:sudo\s+)?tee\b",
    r"\b(?:sudo\s+)?mv\b",
    r"\b(?:sudo\s+)?cp\b",
    r"\b(?:sudo\s+)?rm\b",
    r"\b(?:sudo\s+)?truncate\b",
    r"\b(?:sudo\s+)?dd\b",
    r"\b(?:sudo\s+)?touch\b",
    r"\b(?:sudo\s+)?mkdir\b",
    r"\b(?:sudo\s+)?install\b",
    r"\b(?:sudo\s+)?unlink\b",
    r"\b(?:sudo\s+)?chmod\b",
    r"\b(?:sudo\s+)?chown\b",
    r"\b(?:sudo\s+)?shred\b",
    r"\b(?:sudo\s+)?copy\b",
    r"\b(?:sudo\s+)?del\b",
    r"\b(?:sudo\s+)?move\b",
    r"\b(?:sudo\s+)?ren\b",
)

# 原地编辑与改动工作区文件的 git 操作
_INPLACE_PATTERNS = (
    r"\bsed\s+(?:-\S*i\S*|--in-place)\b",
    r"\bawk\b[^;|&]*\s-i\s+inplace\b",
    r"\bgit\s+(?:checkout|restore|clean|mv|rm)\b",
    r"\bgit\s+reset\s+--hard\b",
)

_WRITE_PATTERNS = tuple(
    re.compile(pattern, re.IGNORECASE)
    for pattern in _INTERPRETERS + _WRITE_COMMANDS + _INPLACE_PATTERNS
)

# specs 目录路径：前接起始/空白/引号/路径分隔符，后接路径分隔符/空白/引号/结尾。
# 前后都限定边界，specs.md 这类普通文件名不命中
_SPECS_PATH_RE = re.compile(
    r"(?:^|[\s\"'`=/\\@])specs(?=[/\\]|[\s\"'`]|$)", re.IGNORECASE
)

# 引号内出现的 > 不是 shell 重定向（如 grep -n '>' specs/x），先摘除
_QUOTED_RE = re.compile(r"'[^']*'|\"[^\"]*\"")

# fd 重定向（2>&1、>&2 等）不是文件写入，先摘除
_FD_REDIRECT_RE = re.compile(r"\d?>>?&\d")

_REDIRECT_RE = re.compile(r">")


def _allow() -> int:
    print(json.dumps({"decision": "allow"}, ensure_ascii=False))
    return 0


def _block(reason: str) -> int:
    print(json.dumps({"decision": "block", "reason": reason}, ensure_ascii=False))
    return 2  # 退出码 2 = 中止工具调用


def _has_file_redirect(command: str) -> bool:
    """摘除引号内容与 fd 重定向后，仍出现 > 视为写文件。"""
    stripped = _QUOTED_RE.sub("", command)
    stripped = _FD_REDIRECT_RE.sub("", stripped)
    return bool(_REDIRECT_RE.search(stripped))


def _has_write_indicator(command: str) -> bool:
    return any(pattern.search(command) for pattern in _WRITE_PATTERNS)


def _check(command: str) -> str | None:
    """检查 Bash 命令，返回违规原因；通过返回 None。"""
    if not _SPECS_PATH_RE.search(command):
        return None  # 不涉及 specs/ 一律放行

    words = command.split()
    first = words[0] if words else ""
    if first == "sudo" and len(words) > 1:
        first = words[1]

    has_write = _has_write_indicator(command) or _has_file_redirect(command)

    # 纯读取类命令放行；但管道后段（head a | tee b）或重定向仍视为写入
    if first in READ_COMMANDS and not has_write:
        return None

    return REASON if has_write else None


def main() -> int:
    # 管道环境下 stdout 可能被 Python 按本地编码（如 GBK）输出，导致中文乱码；
    # 显式切到 UTF-8，保证 hook 读到合法 JSON
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")
    try:
        payload = json.loads(sys.stdin.buffer.read().decode("utf-8"))
    except (ValueError, UnicodeDecodeError):
        return _allow()  # 输入不可解析时不阻塞（fail-open），格式由 harness 保证
    if not isinstance(payload, dict):
        return _allow()
    tool_input = payload.get("tool_input")
    if not isinstance(tool_input, dict):
        return _allow()
    command = tool_input.get("command")
    if not isinstance(command, str) or not command.strip():
        return _allow()
    reason = _check(command)
    return _allow() if reason is None else _block(reason)


if __name__ == "__main__":
    sys.exit(main())
