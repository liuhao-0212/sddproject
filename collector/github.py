"""GitHub 采集模块：获取指定仓库时间范围内的 Commit 记录（design.md §4.1）。

接口契约: collect(repos, since, until) -> list[CommitRecord]

错误处理（design.md §6.1）:
- 超时/网络错误重试 3 次（间隔 5s），重试耗尽仍失败则返回空列表 + 错误日志
  （由生成层在日报中标注"数据获取失败"）
- 触发限流（HTTP 403 + X-RateLimit-* / Retry-After 头）时等待 reset 时间后重试
- 变更统计：commits 列表接口不含 stats，逐条调用 commit 详情接口补充；
  详情获取失败时保留记录（统计为 0）并记录告警日志，不静默丢弃
"""

from __future__ import annotations

import time
from dataclasses import dataclass
from datetime import datetime
from typing import Any

import httpx

from shared.config import env
from shared.errors import CollectorError
from shared.logger import get_logger

logger = get_logger("collector.github")

BASE_URL = "https://api.github.com"
DEFAULT_TIMEOUT = 30.0   # 单次请求超时（秒）
PER_PAGE = 100           # 每页条数（GitHub 默认 30，最大 100）
MAX_PAGES = 20           # 分页安全上限
MAX_RETRIES = 3          # 超时/限流后最多重试次数（不含首次尝试）
RETRY_INTERVAL = 5.0     # 超时重试间隔（秒）
RATE_LIMIT_BUFFER = 1.0  # 距 reset 时间的余量（秒）
DEFAULT_RATE_LIMIT_WAIT = 60.0

# 模块级别名，便于测试中替换，避免影响全局 time 模块
_sleep = time.sleep

# 最近一次 collect() 的失败原因；编排层据此区分“数据源失败”与“数据源为空”（design.md §6.1）
_last_error: str | None = None


def get_last_error() -> str | None:
    """返回最近一次 collect() 的失败原因，无失败时为 None。"""
    return _last_error


@dataclass
class CommitRecord:
    """代码提交记录（design.md §3.1）。"""

    author: str          # 提交者（GitHub 用户名）
    message: str         # 提交信息（Commit Message）
    timestamp: datetime  # 提交时间
    repo: str            # 仓库名称
    additions: int = 0       # 新增行数（详情接口补充，失败时为 0）
    deletions: int = 0       # 删除行数
    files_changed: int = 0   # 变更文件数


def collect(repos: list[str], since: datetime, until: datetime) -> list[CommitRecord]:
    """获取所有指定仓库在 [since, until] 内的 Commit 记录。

    单个仓库采集失败不影响其他仓库；失败的仓库返回空列表并记录错误日志。
    """
    global _last_error
    _last_error = None
    records: list[CommitRecord] = []
    client = _get_client()
    try:
        for repo in repos:
            records.extend(_collect_repo(client, repo, since, until))
    finally:
        client.close()
    return records


def _get_client() -> httpx.Client:
    """构建 GitHub API 客户端。密钥通过环境变量注入（design.md §6.2）。"""
    token = env("GITHUB_TOKEN")
    headers = {
        "Accept": "application/vnd.github+json",
        "X-GitHub-Api-Version": "2022-11-28",
        "User-Agent": "daily-report",
    }
    if token:
        headers["Authorization"] = f"Bearer {token}"
    return httpx.Client(headers=headers, timeout=DEFAULT_TIMEOUT)


def _collect_repo(client: httpx.Client, repo: str, since: datetime, until: datetime) -> list[CommitRecord]:
    """采集单个仓库的 Commit 记录；失败时记录错误日志并返回空列表。"""
    global _last_error
    try:
        raw_commits = _fetch_commits(client, repo, since, until)
    except CollectorError as exc:
        _last_error = str(exc)
        logger.error("github 数据源采集失败，返回空列表",
                     extra={"repo": repo, "reason": str(exc)}, exc_info=exc)
        return []

    records: list[CommitRecord] = []
    for raw in raw_commits:
        parsed = _parse_commit(raw, repo)
        if parsed is None:
            continue
        record, sha = parsed
        _enrich_stats(client, repo, sha, record)
        records.append(record)
    logger.info("github 采集完成", extra={"repo": repo, "count": len(records)})
    return records


def _fetch_commits(client: httpx.Client, repo: str, since: datetime, until: datetime) -> list[dict]:
    """分页拉取 [since, until] 内的 commits 列表。"""
    commits: list[dict] = []
    params: dict[str, Any] = {
        "since": since.isoformat(),
        "until": until.isoformat(),
        "per_page": PER_PAGE,
    }
    for page in range(1, MAX_PAGES + 1):
        resp = _request_with_retry(client, "GET", f"{BASE_URL}/repos/{repo}/commits",
                                   params={**params, "page": page})
        try:
            data = resp.json()
        except ValueError as exc:
            raise CollectorError(f"github commits 接口返回非 JSON: repo={repo}") from exc
        if not isinstance(data, list):
            raise CollectorError(f"github commits 接口返回格式异常: repo={repo}")
        commits.extend(data)
        if len(data) < PER_PAGE:
            return commits
    logger.warning("github commits 分页达到上限，停止拉取",
                   extra={"repo": repo, "max_pages": MAX_PAGES})
    return commits


def _parse_commit(raw: dict, repo: str) -> tuple[CommitRecord, str] | None:
    """解析单条 commit 为 CommitRecord；sha 缺失或时间非法时跳过并记录告警。"""
    sha = raw.get("sha")
    if not sha:
        logger.warning("github commit 缺少 sha，跳过", extra={"repo": repo})
        return None
    commit = raw.get("commit") if isinstance(raw.get("commit"), dict) else {}
    author_info = commit.get("author") if isinstance(commit.get("author"), dict) else {}
    top_author = raw.get("author") if isinstance(raw.get("author"), dict) else {}
    author = top_author.get("login") or author_info.get("name") or "unknown"
    try:
        timestamp = datetime.fromisoformat(author_info.get("date", ""))
    except (TypeError, ValueError):
        logger.warning("github commit 时间解析失败，跳过", extra={"repo": repo, "sha": sha})
        return None
    return CommitRecord(
        author=str(author),
        message=str(commit.get("message") or ""),
        timestamp=timestamp,
        repo=repo,
    ), str(sha)


def _enrich_stats(client: httpx.Client, repo: str, sha: str, record: CommitRecord) -> None:
    """调用 commit 详情接口补充变更统计；失败时保留 0 值并记录告警（优雅降级）。"""
    try:
        resp = _request_with_retry(client, "GET", f"{BASE_URL}/repos/{repo}/commits/{sha}")
    except CollectorError as exc:
        logger.warning("github commit 变更统计获取失败，统计置 0",
                       extra={"repo": repo, "sha": sha, "reason": str(exc)})
        return
    try:
        data = resp.json()
    except ValueError as exc:
        logger.warning("github commit 变更统计响应非 JSON，统计置 0",
                       extra={"repo": repo, "sha": sha, "reason": str(exc)})
        return
    if not isinstance(data, dict):
        return
    stats = data.get("stats") if isinstance(data.get("stats"), dict) else {}
    record.additions = int(stats.get("additions", 0))
    record.deletions = int(stats.get("deletions", 0))
    record.files_changed = len(data.get("files") or [])


def _request_with_retry(client: httpx.Client, method: str, url: str, **kwargs: Any) -> httpx.Response:
    """带重试的请求：超时/网络错误与限流各最多重试 MAX_RETRIES 次。"""
    attempt = 0
    while True:
        try:
            resp = client.request(method, url, **kwargs)
        except (httpx.TimeoutException, httpx.HTTPError) as exc:
            if attempt >= MAX_RETRIES:
                raise CollectorError(
                    f"github 请求失败（已重试 {MAX_RETRIES} 次）: {method} {url}: {exc}") from exc
            attempt += 1
            logger.warning("github 请求失败，稍后重试",
                           extra={"url": url, "attempt": attempt, "error": str(exc)})
            _sleep(RETRY_INTERVAL)
            continue
        if resp.status_code == 200:
            return resp
        if resp.status_code == 403 and _is_rate_limited(resp):
            if attempt >= MAX_RETRIES:
                raise CollectorError(
                    f"github 触发限流且重试耗尽（{MAX_RETRIES} 次）: {method} {url}")
            attempt += 1
            wait = _rate_limit_wait(resp)
            logger.warning("github 触发限流，等待后重试",
                           extra={"url": url, "attempt": attempt, "wait_seconds": wait})
            _sleep(wait)
            continue
        raise CollectorError(f"github 接口返回错误状态 {resp.status_code}: {method} {url}")


def _is_rate_limited(resp: httpx.Response) -> bool:
    """403 是否由限流引起：主限流（Remaining 耗尽）或次限流（Retry-After）。"""
    return resp.headers.get("X-RateLimit-Remaining") == "0" or "Retry-After" in resp.headers


def _rate_limit_wait(resp: httpx.Response) -> float:
    """计算限流等待时间：优先 Retry-After，其次 X-RateLimit-Reset，兜底默认值。"""
    retry_after = resp.headers.get("Retry-After")
    if retry_after:
        try:
            return max(1.0, float(retry_after))
        except ValueError:
            pass
    reset = resp.headers.get("X-RateLimit-Reset")
    if reset:
        try:
            return max(1.0, float(reset) - time.time() + RATE_LIMIT_BUFFER)
        except ValueError:
            pass
    return DEFAULT_RATE_LIMIT_WAIT
