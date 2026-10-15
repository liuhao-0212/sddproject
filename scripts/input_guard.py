"""Claude Code PreToolUse 输入守卫（Edit|Write，由 .claude/settings.json 配置）。

从 stdin 读取 hook JSON（tool_input 含 file_path / content / new_string 等），
逐条检查硬规则；放行时输出 hookSpecificOutput（permissionDecision=allow），
违规时输出 permissionDecision=deny + 原因并以退出码 2 中止工具调用。

规则（锚定规范）：
1. specs/ 目录：规范只能由人类修改；存在放行令牌 specs/.ai-edit-allow 时
   本次放行（打印一行提示），令牌由人类创建
2. 路径含 summarizer 或 nlp：proposal.md §2.2 已排除智能摘要
3. 新增内容含密钥硬编码赋值：design.md §6.2 禁止（经 env 读取的合法写法放行）
"""

from __future__ import annotations

import json
import re
import sys
from pathlib import Path, PurePath

REPO_ROOT = Path(__file__).resolve().parent.parent

# specs/ 修改放行令牌：人类创建 specs/.ai-edit-allow 后，本次放行 AI 修改 specs/
_SPECS_TOKEN = REPO_ROOT / "specs" / ".ai-edit-allow"
_TOKEN_NOTICE = "检测到 spec 放行令牌 specs/.ai-edit-allow，本次允许 AI 修改 specs/"
_SPECS_BLOCK_REASON = ("禁止修改 specs/（规范只能由人类修改）。"
                       "如需本次放行：创建 specs/.ai-edit-allow 后重试")

# 密钥名清单（design.md §6.2：API 密钥一律经环境变量注入）
SECRET_NAMES = ("GITHUB_TOKEN", "LARK_APP_ID", "LARK_APP_SECRET", "SMTP_PASSWORD")

# 硬编码赋值：NAME = 值；值为 env(...) / os.environ / os.getenv(...) 时视为合法读取放行
_SECRET_RE = re.compile(
    r"(?P<name>" + "|".join(SECRET_NAMES)
    + r")\s*=\s*(?!\s*env\s*\(|\s*os\.environ|\s*os\.getenv\s*\()",
    re.IGNORECASE,
)

# 路径敏感词（proposal.md §2.2：智能摘要不在采集范围内）
_BLOCKED_PATH_WORDS = ("summarizer", "nlp")


def _allow() -> int:
    print(json.dumps({"hookSpecificOutput": {
        "hookEventName": "PreToolUse",
        "permissionDecision": "allow",
    }}, ensure_ascii=False))
    return 0


def _block(reason: str) -> int:
    print(json.dumps({"hookSpecificOutput": {
        "hookEventName": "PreToolUse",
        "permissionDecision": "deny",
        "permissionDecisionReason": reason,
    }}, ensure_ascii=False))
    return 2  # 退出码 2 = 中止工具调用


def _check(payload: dict) -> str | None:
    """逐条检查规则，返回违规原因；全部通过返回 None。"""
    tool_input = payload.get("tool_input")
    if not isinstance(tool_input, dict):
        return None

    file_path = str(tool_input.get("file_path") or "")
    normalized = file_path.replace("\\", "/")
    if "specs" in PurePath(normalized).parts:
        if _SPECS_TOKEN.exists():
            print(_TOKEN_NOTICE, file=sys.stderr)  # 令牌生效：放行并提示
            return None
        return _SPECS_BLOCK_REASON
    path_lower = normalized.lower()
    for word in _BLOCKED_PATH_WORDS:
        if word in path_lower:
            return f"禁止操作含 {word} 的文件（proposal.md §2.2 已排除智能摘要）: {file_path}"

    # 只检查新增内容（Write 的 content / Edit 的 new_string），删除密钥不应被拦截
    new_content = tool_input.get("content")
    if new_content is None:
        new_content = tool_input.get("new_string")
    if isinstance(new_content, str):
        match = _SECRET_RE.search(new_content)
        if match:
            return (f"禁止硬编码密钥 {match.group('name').upper()} "
                    f"（design.md §6.2：必须经环境变量注入）")
    return None


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
    reason = _check(payload)
    return _allow() if reason is None else _block(reason)


if __name__ == "__main__":
    sys.exit(main())
