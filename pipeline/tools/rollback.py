#!/usr/bin/env python
"""回滚 —— 把环境切回上一个发布。

回滚靠的是"不可变制品 + 发布历史"，不是重新构建、不是临时改代码。
发布前就准备好回滚路径，是 CD 与"手动上传服务器"最本质的区别之一。

用法：
  python pipeline/tools/rollback.py --project . --env prod
"""

import argparse
import json
import os
import sys
from datetime import datetime, timezone


def read_history(env_dir):
    """读发布/回滚审计日志。"""
    path = os.path.join(env_dir, "history.jsonl")
    records = []
    if not os.path.isfile(path):
        return records
    with open(path, "r", encoding="utf-8") as handle:
        for line in handle:
            line = line.strip()
            if line:
                records.append(json.loads(line))
    return records


def main(argv=None):
    """命令行入口。"""
    parser = argparse.ArgumentParser(description="回滚到上一个发布")
    parser.add_argument("--project", default=".", help="项目根目录")
    parser.add_argument("--env", required=True, choices=["staging", "prod"], help="目标环境")
    parser.add_argument("--root", default="environments", help="环境根目录（相对项目根）")
    args = parser.parse_args(argv)

    project_dir = os.path.abspath(args.project)
    env_dir = os.path.join(project_dir, args.root, args.env)
    marker = os.path.join(env_dir, "current.txt")
    if not os.path.isfile(marker):
        print("[rollback] 环境 %s 还没有任何发布记录" % args.env)
        return 1
    with open(marker, "r", encoding="utf-8") as handle:
        current = handle.read().strip()

    history = read_history(env_dir)
    if len(history) < 2:
        print("[rollback] 环境 %s 只有一次发布，没有可回滚的版本" % args.env)
        return 1

    target = history[-2]["release"]
    if target == current:
        print("[rollback] 上一个发布与当前相同（%s），无需回滚" % current)
        return 1

    with open(marker, "w", encoding="utf-8") as handle:
        handle.write(target + "\n")
    record = dict(history[-1])
    record.update({
        "action": "rollback",
        "release": target,
        "rolled_back_from": current,
        "at": datetime.now(timezone.utc).replace(microsecond=0).isoformat(),
    })
    with open(os.path.join(env_dir, "history.jsonl"), "a", encoding="utf-8") as handle:
        handle.write(json.dumps(record, ensure_ascii=False) + "\n")

    print("[rollback] 环境 %s：%s -> %s" % (args.env, current, target))
    print("[rollback] 只改了一个指针，耗时几乎为 0：这就是「制品不可变 + 发布历史」的价值")
    return 0


if __name__ == "__main__":
    sys.exit(main())