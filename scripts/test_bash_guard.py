"""bash_guard.py 自测：把命令 JSON 经 stdin 管道喂给护栏，验证该拦的拦、该放的放。

直接运行：python scripts/test_bash_guard.py
退出码 0 = 全部通过；1 = 有失败（打印明细）。
"""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

GUARD = Path(__file__).with_name("bash_guard.py")

# 应当拦截：涉及 specs/ 且含写入迹象
BLOCK_CASES = [
    # 本次绕过场景：Bash 跑 python 写 specs/
    "python - <<'EOF'\nopen('specs/proposal.md', 'w').write('x')\nEOF",
    "python scripts/tool.py specs/proposal.md",
    "perl -pi -e 's/a/b/' specs/proposal.md",
    # 重定向写文件
    "echo x >> specs/proposal.md",
    "cat a.md > specs/proposal.md",
    "cat a.md 1>> specs/proposal.md",
    "echo hi > ./specs/x.md",
    "echo hi > specs\\proposal.md",
    # 写/删/改命令
    "tee specs/proposal.md <<< x",
    "mv /tmp/a.md specs/proposal.md",
    "cp /tmp/a.md specs/",
    "rm specs/proposal.md",
    "sudo rm -rf specs",
    "truncate -s 0 specs/proposal.md",
    "dd if=/tmp/a of=specs/proposal.md",
    "touch specs/new.md",
    "git checkout -- specs/proposal.md",
    "git reset --hard specs/",
    # 纯读命令带写入迹象同样拦
    "head specs/a.md | tee specs/b.md",
    "ls specs/ > /tmp/list.txt",
    "sed -i 's/a/b/' specs/proposal.md",
]

# 应当放行：不涉及 specs/，或纯读取 specs/
ALLOW_CASES = [
    # 纯读取类命令
    "cat specs/proposal.md",
    "cat specs/proposal.md 2>&1",  # fd 重定向不是写文件
    "grep -n 目标 specs/proposal.md",
    "grep -n '>' specs/proposal.md",  # 引号内的 > 不是重定向
    "rg 目标 specs/",
    "head -5 specs/proposal.md",
    "tail -5 specs/proposal.md",
    "ls specs/",
    "wc -l specs/proposal.md",
    "diff specs/a.md specs/b.md",
    "stat specs/proposal.md",
    # 无写入迹象的其它读取
    "find specs -name '*.md'",
    "git diff specs/proposal.md",
    "sed 's/a/b/' specs/proposal.md",  # sed 无 -i 只输出不改写
    # 不涉及 specs/ 的命令（含写入）
    "echo hello",
    "python scripts/gen_report.py",
    "cat /tmp/notes.md > /tmp/out.md",
    # specs.md 是文件名不是目录
    "cat specs.md",
]


def _run_guard(command: str) -> subprocess.CompletedProcess:
    payload = json.dumps(
        {"tool_name": "Bash", "tool_input": {"command": command}},
        ensure_ascii=False,
    )
    return subprocess.run(
        [sys.executable, str(GUARD)],
        input=payload,
        capture_output=True,
        text=True,
        encoding="utf-8",
    )


def main() -> int:
    # 与守卫脚本一致：显式切到 UTF-8，避免 Windows 控制台按 GBK 输出中文乱码
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")
    failures: list[tuple[str, subprocess.CompletedProcess]] = []

    for command in BLOCK_CASES:
        result = _run_guard(command)
        ok = result.returncode == 2 and '"decision": "block"' in result.stdout
        print(f"{'PASS' if ok else 'FAIL'} 拦: {command[:60]!r}")
        if not ok:
            failures.append((command, result))

    for command in ALLOW_CASES:
        result = _run_guard(command)
        ok = result.returncode == 0 and '"decision": "allow"' in result.stdout
        print(f"{'PASS' if ok else 'FAIL'} 放: {command[:60]!r}")
        if not ok:
            failures.append((command, result))

    print()
    if failures:
        print(f"失败 {len(failures)} 条：")
        for command, result in failures:
            print(f"  {command!r}\n"
                  f"    rc={result.returncode} out={result.stdout!r} err={result.stderr!r}")
        return 1
    print(f"全部通过：拦 {len(BLOCK_CASES)} / 放 {len(ALLOW_CASES)}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
