#!/usr/bin/env python
"""构建制品 —— "build once, deploy many" 里的那个 build。

产出 dist/daily-report-<version>-<git_sha>.zip，内含：
  main.py / show_report.py / collector / generator / notifier / shared / scripts
  specs/（规范随制品走，便于生产上追溯"这份代码对应哪条需求"）
  pyproject.toml / uv.lock（锁定的依赖，保证环境可复现）
  manifest.json（版本、提交、构建时间、解释器版本、每个文件的 sha256）

之后所有环境部署的都是**同一个 zip、同一个 sha256**：
预发验证过的字节，就是生产上跑的字节。

用法：
  python pipeline/tools/build_artifact.py --project . --out dist
"""

import argparse
import hashlib
import json
import os
import re
import subprocess
import sys
import zipfile
from datetime import datetime, timezone

INCLUDE_DIRS = ("collector", "generator", "notifier", "shared", "scripts", "specs")
INCLUDE_FILES = ("main.py", "show_report.py", "pyproject.toml", "uv.lock",
                 "config.yaml.example", "README.md", "claude.md")
SKIP_DIRS = {".git", "__pycache__", ".venv", ".pytest_cache", ".claude", ".vscode",
             "node_modules", "dist", "environments", "reports", "data", "output"}
VERSION_PATTERN = re.compile(r'^version\s*=\s*"([^"]+)"', re.MULTILINE)


def sha256_file(path):
    """算文件 sha256，用于制品指纹与完整性校验。"""
    digest = hashlib.sha256()
    with open(path, "rb") as handle:
        for chunk in iter(lambda: handle.read(65536), b""):
            digest.update(chunk)
    return digest.hexdigest()


def run_git(project_dir, args):
    """执行一条 git 命令；失败返回 None。"""
    try:
        proc = subprocess.run(["git"] + args, cwd=project_dir, capture_output=True, text=True)
    except OSError:
        return None
    return proc.stdout.strip() if proc.returncode == 0 else None


def read_source_epoch(project_dir):
    """取最后一次提交的时间作为构建基准时间：同一提交再构建一次，字节也应当一致。"""
    raw = run_git(project_dir, ["log", "-1", "--format=%ct"])
    try:
        epoch = int(raw)
    except (TypeError, ValueError):
        return 0
    return epoch


def read_version(project_dir):
    """版本号来自 pyproject.toml —— 制品的身份证。"""
    path = os.path.join(project_dir, "pyproject.toml")
    with open(path, "r", encoding="utf-8") as handle:
        match = VERSION_PATTERN.search(handle.read())
    if not match:
        raise SystemExit("[build] pyproject.toml 里没有 version 字段")
    return match.group(1)


def collect_files(project_dir):
    """收集要打进制品的文件，返回 [(压缩包内路径, 磁盘路径)]。"""
    entries = []
    for name in INCLUDE_FILES:
        path = os.path.join(project_dir, name)
        if os.path.isfile(path):
            entries.append((name, path))
    for name in INCLUDE_DIRS:
        root_dir = os.path.join(project_dir, name)
        if not os.path.isdir(root_dir):
            continue
        for root, dirs, files in os.walk(root_dir):
            dirs[:] = [d for d in dirs if d not in SKIP_DIRS]
            for filename in sorted(files):
                if filename.endswith((".pyc", ".pyo")):
                    continue
                path = os.path.join(root, filename)
                entries.append((os.path.relpath(path, project_dir).replace("\\", "/"), path))
    return sorted(entries)


def main(argv=None):
    """命令行入口。"""
    parser = argparse.ArgumentParser(description="打包可部署制品（智能日报生成器）")
    parser.add_argument("--project", default=".", help="项目根目录")
    parser.add_argument("--out", default="dist", help="制品输出目录（相对项目根）")
    parser.add_argument("--name", default="daily-report", help="制品名")
    args = parser.parse_args(argv)

    project_dir = os.path.abspath(args.project)
    version = read_version(project_dir)
    sha = run_git(project_dir, ["rev-parse", "--short", "HEAD"]) or "nogit"
    branch = run_git(project_dir, ["rev-parse", "--abbrev-ref", "HEAD"]) or "nogit"
    dirty = bool(run_git(project_dir, ["status", "--porcelain"]))
    files = collect_files(project_dir)
    if not files:
        raise SystemExit("[build] 没有收集到任何文件，检查 --project 是否正确")

    built_at = datetime.now(timezone.utc).replace(microsecond=0).isoformat()
    source_epoch = read_source_epoch(project_dir)
    source_time = datetime.fromtimestamp(source_epoch, timezone.utc).replace(
        microsecond=0).isoformat() if source_epoch else "1970-01-01T00:00:00+00:00"
    # manifest 里只放"与源码有关"的信息：构建机、构建时刻都不进去，
    # 否则同一份源码换台机器构建，sha256 就会变。
    manifest = {
        "name": args.name,
        "version": version,
        "git_sha": sha,
        "git_branch": branch,
        "git_dirty": dirty,
        "source_time": source_time,
        "files": [{"path": rel, "sha256": sha256_file(path), "size": os.path.getsize(path)}
                  for rel, path in files],
    }

    out_dir = os.path.join(project_dir, args.out)
    os.makedirs(out_dir, exist_ok=True)
    artifact = os.path.join(out_dir, "%s-%s-%s.zip" % (args.name, version, sha))
    if os.path.exists(artifact):
        os.remove(artifact)
    # 固定 zip 内每个条目的时间戳：否则 mtime 不同 -> 字节不同 -> sha256 不同
    fixed_time = (1980, 1, 1, 0, 0, 0)
    with zipfile.ZipFile(artifact, "w", zipfile.ZIP_DEFLATED) as bundle:
        for rel, path in files:
            info = zipfile.ZipInfo(rel, date_time=fixed_time)
            info.compress_type = zipfile.ZIP_DEFLATED
            info.external_attr = 0o644 << 16
            with open(path, "rb") as handle:
                bundle.writestr(info, handle.read())
        manifest_info = zipfile.ZipInfo("manifest.json", date_time=fixed_time)
        manifest_info.compress_type = zipfile.ZIP_DEFLATED
        bundle.writestr(manifest_info, json.dumps(manifest, ensure_ascii=False, indent=2))

    info = {
        "artifact": os.path.relpath(artifact, project_dir).replace("\\", "/"),
        "artifact_abs": artifact,
        "artifact_sha256": sha256_file(artifact),
        "size_bytes": os.path.getsize(artifact),
        "name": args.name,
        "version": version,
        "git_sha": sha,
        "git_branch": branch,
        "git_dirty": dirty,
        "built_at": built_at,
        "source_time": source_time,
        "python": "%d.%d.%d" % sys.version_info[:3],
        "file_count": len(files),
    }
    info_path = os.path.join(out_dir, "build-info.json")
    with open(info_path, "w", encoding="utf-8") as handle:
        json.dump(info, handle, ensure_ascii=False, indent=2)

    print("[build] 版本 %s / 提交 %s（分支 %s%s）" % (
        version, sha, branch, "，工作区有未提交改动" if dirty else "，工作区干净"))
    print("[build] 打包 %d 个文件（%.1f KB）-> %s" % (
        len(files), info["size_bytes"] / 1024.0, info["artifact"]))
    print("[build] 制品 sha256: %s" % info["artifact_sha256"])
    print("[build] 可复现构建：zip 时间戳已固定，同一提交重复构建 sha256 相同")
    print("[build] build-info.json: %s" % os.path.relpath(info_path, project_dir))
    return 0


if __name__ == "__main__":
    sys.exit(main())