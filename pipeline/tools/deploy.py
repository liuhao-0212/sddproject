#!/usr/bin/env python
"""部署 —— 把同一个制品解压到某个环境的发布目录，并留下可审计的发布历史。

目录约定（本机模拟真实环境；生产上这一层是虚机 / 容器 / 定时任务主机）：

  environments/<env>/releases/<release_id>/   一次发布的不可变快照
  environments/<env>/current.txt              当前生效的发布（回滚只改这个指针）
  environments/<env>/history.jsonl            发布/回滚审计日志（append-only）

要点：
  1) 部署的是制品而不是源码目录 —— 各环境跑的字节完全一致；
  2) 同一制品重复部署到同一环境是幂等的（重放不炸）；
  3) --expect-sha 校验制品完整性，供应链被篡改就拒绝部署；
  4) 每次发布会生成 release.json + 当日运行命令示例（定时任务怎么接）。

用法：
  python pipeline/tools/deploy.py --project . --env staging --artifact dist/x.zip
"""

import argparse
import hashlib
import json
import os
import shutil
import sys
import zipfile
from datetime import datetime, timezone

RUN_HINTS = {
    "staging": "预发环境：用来验证「这次改动能不能跑通」，可以随便折腾",
    "prod": "生产环境：每天 08:00 由定时任务调用 main.py 生成并推送日报",
}


def sha256_file(path):
    """算制品 sha256。"""
    digest = hashlib.sha256()
    with open(path, "rb") as handle:
        for chunk in iter(lambda: handle.read(65536), b""):
            digest.update(chunk)
    return digest.hexdigest()


def read_manifest(artifact):
    """读取制品内的 manifest.json。"""
    with zipfile.ZipFile(artifact) as bundle:
        return json.loads(bundle.read("manifest.json").decode("utf-8"))


def read_current(env_dir):
    """读当前生效的发布号。"""
    marker = os.path.join(env_dir, "current.txt")
    if os.path.isfile(marker):
        with open(marker, "r", encoding="utf-8") as handle:
            return handle.read().strip() or None
    return None


def main(argv=None):
    """命令行入口。"""
    parser = argparse.ArgumentParser(description="把制品部署到指定环境")
    parser.add_argument("--project", default=".", help="项目根目录")
    parser.add_argument("--env", required=True, choices=["staging", "prod"], help="目标环境")
    parser.add_argument("--artifact", required=True, help="制品 zip（相对项目根或绝对路径）")
    parser.add_argument("--root", default="environments", help="环境根目录（相对项目根）")
    parser.add_argument("--expect-sha", help="期望的制品 sha256，不一致则拒绝部署")
    parser.add_argument("--json-out", help="部署结果写成 JSON，供流水线下游读取")
    args = parser.parse_args(argv)

    project_dir = os.path.abspath(args.project)
    artifact = args.artifact
    if not os.path.isabs(artifact):
        artifact = os.path.join(project_dir, artifact)
    if not os.path.isfile(artifact):
        print("[deploy] 找不到制品 %s" % artifact)
        return 2

    artifact_sha = sha256_file(artifact)
    if args.expect_sha and artifact_sha != args.expect_sha:
        print("[deploy] 制品校验失败：期望 %s，实际 %s" % (args.expect_sha, artifact_sha))
        print("[deploy] 拒绝部署 —— 供应链完整性是发布前的最后一道闸门")
        return 2

    manifest = read_manifest(artifact)
    release_id = "%s-%s" % (manifest["version"], manifest["git_sha"])
    env_dir = os.path.join(project_dir, args.root, args.env)
    release_dir = os.path.join(env_dir, "releases", release_id)
    previous = read_current(env_dir)
    redeployed = os.path.isdir(release_dir)

    if redeployed:
        shutil.rmtree(release_dir)
    os.makedirs(release_dir, exist_ok=True)
    with zipfile.ZipFile(artifact) as bundle:
        bundle.extractall(release_dir)
    if os.path.isfile(os.path.join(project_dir, "config.yaml")):
        shutil.copy2(os.path.join(project_dir, "config.yaml"),
                     os.path.join(release_dir, "config.yaml"))

    record = {
        "action": "deploy",
        "env": args.env,
        "release": release_id,
        "version": manifest["version"],
        "git_sha": manifest["git_sha"],
        "artifact": os.path.relpath(artifact, project_dir).replace("\\", "/"),
        "artifact_sha256": artifact_sha,
        "previous_release": previous,
        "at": datetime.now(timezone.utc).replace(microsecond=0).isoformat(),
        "by": os.environ.get("USERNAME") or os.environ.get("USER") or "unknown",
    }
    os.makedirs(env_dir, exist_ok=True)
    with open(os.path.join(release_dir, "release.json"), "w", encoding="utf-8") as handle:
        json.dump(record, handle, ensure_ascii=False, indent=2)
    with open(os.path.join(env_dir, "current.txt"), "w", encoding="utf-8") as handle:
        handle.write(release_id + "\n")
    with open(os.path.join(env_dir, "history.jsonl"), "a", encoding="utf-8") as handle:
        handle.write(json.dumps(record, ensure_ascii=False) + "\n")
    with open(os.path.join(release_dir, "deploy-notes.md"), "w", encoding="utf-8") as handle:
        handle.write("# %s 环境发布说明\n\n" % args.env)
        handle.write("- 发布号：%s\n- 制品：%s\n- 制品 sha256：%s\n- 发布人：%s\n- 发布时间：%s\n\n"
                     % (release_id, record["artifact"], artifact_sha, record["by"], record["at"]))
        handle.write("## 这个环境怎么跑\n\n%s\n\n" % RUN_HINTS[args.env])
        handle.write("```powershell\n# 单次运行（定时任务里也是这一条）\n"
                     "& <python> main.py\n# 只采集+生成、不推送（演练用）\n& <python> main.py --dry-run\n"
                     "# 健康检查\n& <python> main.py --check\n```\n\n")
        handle.write("## 定时任务（Windows 任务计划）\n\n```powershell\n"
                     "schtasks /Create /TN \"daily-report\" /SC DAILY /ST 08:00 /TR \"<上面的命令>\"\n```\n")
    if args.json_out:
        with open(args.json_out, "w", encoding="utf-8") as handle:
            json.dump(record, handle, ensure_ascii=False, indent=2)

    print("[deploy] 环境 %s：%s -> %s%s" % (
        args.env, previous or "(首次发布)", release_id, "（幂等重放）" if redeployed else ""))
    print("[deploy] 制品 %s" % record["artifact"])
    print("[deploy] 制品 sha256 %s" % artifact_sha)
    print("[deploy] 生效目录 %s/%s/current.txt = %s" % (args.root, args.env, release_id))
    return 0


if __name__ == "__main__":
    sys.exit(main())