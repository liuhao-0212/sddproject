"""提交归类（v1.8，design.md §6.5）：按 Conventional Commits 前缀把提交标题
映射为展示类别（新功能/修复/性能/重构/文档/测试/工程/样式/其他）。

纯函数、无外部依赖，便于单元测试（tests/test_classify.py）。
"""

from __future__ import annotations

import re
from dataclasses import dataclass


@dataclass(frozen=True)
class CommitCategory:
    """展示类别：key 为类别标识；priority 越小在「今日亮点」中越优先。"""

    key: str
    label: str
    icon: str
    priority: int


# 类别定义（design.md §6.5 v1.8 映射表）；CATEGORY_ORDER 为固定展示顺序，
# priority 与之单调一致（feat > fix > perf > refactor > docs > test > ci > style > other）。
CATEGORIES: tuple[CommitCategory, ...] = (
    CommitCategory("feat", "新功能", "✨", 0),
    CommitCategory("fix", "修复", "🐛", 1),
    CommitCategory("perf", "性能", "⚡", 2),
    CommitCategory("refactor", "重构", "♻️", 3),
    CommitCategory("docs", "文档", "📝", 4),
    CommitCategory("test", "测试", "✅", 5),
    CommitCategory("ci", "工程", "🔧", 6),
    CommitCategory("style", "样式", "🎨", 7),
    CommitCategory("other", "其他", "📦", 8),
)
CATEGORY_ORDER: tuple[str, ...] = tuple(category.key for category in CATEGORIES)
CATEGORY_BY_KEY: dict[str, CommitCategory] = {category.key: category
                                              for category in CATEGORIES}

# 前缀别名 → 类别 key（design.md §6.5 v1.8 映射表：feat/feature、fix/bugfix/hotfix、
# test/tests、ci/build/chore 共用类别）；未列出的前缀归「其他」。
_TYPE_ALIASES = {
    "feat": "feat",
    "feature": "feat",
    "fix": "fix",
    "bugfix": "fix",
    "hotfix": "fix",
    "perf": "perf",
    "refactor": "refactor",
    "docs": "docs",
    "test": "test",
    "tests": "test",
    "ci": "ci",
    "build": "ci",
    "chore": "ci",
    "style": "style",
}

# 解析正则（design.md §6.5 v1.8）：只解析消息第一行；前缀为行首单词（字母），
# 可带 scope（`(…)`）与重大变更标记（`!`），后跟冒号，如 `feat(report)!: …`
_TYPE_RE = re.compile(r"^\s*(?P<type>[a-zA-Z]+)(\([^)]*\))?!?:")


def classify_commit(title: str) -> CommitCategory:
    """把提交标题（只取第一行）解析为展示类别。

    无 Conventional 前缀或前缀未列入映射表时归「其他」，不报错、不丢弃。
    前缀不区分大小写（Conventional Commits 约定小写，这里兼容 `Fix:` 等写法）。
    """
    first_line = title.splitlines()[0].strip() if title else ""
    match = _TYPE_RE.match(first_line)
    if match is None:
        return CATEGORY_BY_KEY["other"]
    key = _TYPE_ALIASES.get(match.group("type").lower(), "other")
    return CATEGORY_BY_KEY[key]


def classify_commits(commits: list) -> dict[str, list]:
    """按类别分组提交，返回 key → 提交列表的字典；key 依 CATEGORY_ORDER 固定顺序，
    组内保持输入顺序（时间排序由调用方决定）。"""
    groups: dict[str, list] = {key: [] for key in CATEGORY_ORDER}
    for commit in commits:
        title = commit.message.splitlines()[0].strip() if commit.message else ""
        groups[classify_commit(title).key].append(commit)
    return groups
