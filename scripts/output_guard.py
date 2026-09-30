"""Claude Code PostToolUse 输出守卫（Edit|Write，由 .claude/settings.json 配置）。

每次文件写入后运行全量测试：通过输出 {"decision":"allow"}；
失败输出 {"decision":"block","reason":"测试未通过…"} 并以退出码 2 阻止继续。
"""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

# 仓库根目录（脚本位于 <repo>/scripts/ 下），确保 hook 从任意 cwd 触发都找得到 tests/
REPO_ROOT = Path(__file__).resolve().parent.parent

TIMEOUT_SECONDS = 120
_REASON_TAIL_CHARS = 400  # reason 只保留输出尾部，避免巨型 JSON


def _run_tests(cmd: list[str]) -> subprocess.CompletedProcess:
    return subprocess.run(cmd, capture_output=True, text=True,
                          encoding="utf-8", errors="replace",
                          cwd=REPO_ROOT, timeout=TIMEOUT_SECONDS)


def main() -> int:
    # 管道环境下 stdout 可能被 Python 按本地编码（如 GBK）输出，导致中文乱码；
    # 显式切到 UTF-8，保证 hook 读到合法 JSON
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")
    cmd = [sys.executable, "-m", "pytest", "tests/", "-q", "--tb=short"]
    try:
        result = _run_tests(cmd)
    except (OSError, subprocess.TimeoutExpired) as exc:
        print(json.dumps({"decision": "block",
                          "reason": f"测试未通过（pytest 无法执行）: {exc}"},
                         ensure_ascii=False))
        return 2
    if result.returncode == 0:
        print(json.dumps({"decision": "allow"}, ensure_ascii=False))
        return 0
    output = (result.stdout or "") + (result.stderr or "")
    tail = " ".join(output.split())[-_REASON_TAIL_CHARS:]
    print(json.dumps({"decision": "block",
                      "reason": f"测试未通过（exit {result.returncode}）: {tail}"},
                     ensure_ascii=False))
    return 2


if __name__ == "__main__":
    sys.exit(main())
