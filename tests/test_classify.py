"""generator/classify 单元测试（v1.8：Conventional Commits 提交归类）。"""

from datetime import datetime, timezone

from collector.github import CommitRecord
from generator.classify import (CATEGORY_BY_KEY, CATEGORY_ORDER, classify_commit,
                                classify_commits)

TZ = timezone.utc


def test_standard_prefixes_map_to_expected_categories():
    """design.md §6.5 v1.8 映射表：前缀/别名 → 类别（key/label/icon）。"""
    cases = {
        "feat: 登录页": ("feat", "新功能", "✨"),
        "feature: 首页": ("feat", "新功能", "✨"),
        "fix: 修复超时": ("fix", "修复", "🐛"),
        "bugfix: 修复超时": ("fix", "修复", "🐛"),
        "hotfix: 紧急修复": ("fix", "修复", "🐛"),
        "perf: 优化查询": ("perf", "性能", "⚡"),
        "refactor: 重构缓存": ("refactor", "重构", "♻️"),
        "docs: 更新 README": ("docs", "文档", "📝"),
        "test: 补充单测": ("test", "测试", "✅"),
        "tests: 补充单测": ("test", "测试", "✅"),
        "ci: 调整流水线": ("ci", "工程", "🔧"),
        "build: 升级依赖": ("ci", "工程", "🔧"),
        "chore: 清理脚本": ("ci", "工程", "🔧"),
        "style: 统一缩进": ("style", "样式", "🎨"),
    }
    for title, (key, label, icon) in cases.items():
        category = classify_commit(title)
        assert (category.key, category.label, category.icon) == (key, label, icon)


def test_scope_and_breaking_marker_supported():
    """带 scope 与 `!` 的写法（如 `feat(report)!: ...`）正常归类。"""
    assert classify_commit("feat(report)!: 支持导出").key == "feat"
    assert classify_commit("feat(report): 支持导出").key == "feat"
    assert classify_commit("fix!: 紧急修复").key == "fix"
    assert classify_commit("feat(): 空 scope").key == "feat"


def test_no_prefix_falls_back_to_other():
    category = classify_commit("更新依赖版本")
    assert category.key == "other"
    assert category.label == "其他"
    assert category.icon == "📦"


def test_unknown_prefix_falls_back_to_other():
    """前缀未列入映射表（或近似前缀）归「其他」，不报错、不丢弃。"""
    assert classify_commit("update: 升级依赖").key == "other"
    assert classify_commit("wip: 未完成").key == "other"
    assert classify_commit("featx: 近似前缀").key == "other"  # 前缀必须完整匹配
    assert classify_commit("fix (scope): 冒号前有空格").key == "other"


def test_prefix_case_insensitive():
    """Conventional Commits 约定小写前缀，兼容 `Fix:` 等大小写写法。"""
    assert classify_commit("Fix: 修复").key == "fix"
    assert classify_commit("FEAT: 新功能").key == "feat"
    assert classify_commit("Refactor!: 重构").key == "refactor"


def test_leading_whitespace_tolerated():
    assert classify_commit("  feat: 带缩进").key == "feat"


def test_only_first_line_parsed():
    """只解析消息第一行（标题），正文与 trailer 不影响归类。"""
    category = classify_commit(
        "feat: 登录页\n\nCo-Authored-By: Claude Code <noreply@anthropic.com>")
    assert category.key == "feat"


def test_empty_title_is_other():
    assert classify_commit("").key == "other"
    assert classify_commit("   ").key == "other"


def _commit(message):
    return CommitRecord(author="zhangsan", message=message,
                        timestamp=datetime(2026, 9, 29, 9, 0, tzinfo=TZ),
                        repo="org/repo", additions=1, deletions=0, files_changed=1)


def test_classify_commits_groups_by_category_in_fixed_order():
    """分组的 key 依 CATEGORY_ORDER 固定顺序，空类别为 []。"""
    commits = [_commit("更新依赖"), _commit("feat: A"), _commit("fix: B"), _commit("docs: C")]
    groups = classify_commits(commits)

    assert list(groups.keys()) == list(CATEGORY_ORDER)
    assert [c.message for c in groups["feat"]] == ["feat: A"]
    assert [c.message for c in groups["fix"]] == ["fix: B"]
    assert [c.message for c in groups["docs"]] == ["docs: C"]
    assert [c.message for c in groups["other"]] == ["更新依赖"]
    assert all(groups[key] == [] for key in CATEGORY_ORDER
               if key not in ("feat", "fix", "docs", "other"))


def test_classify_commits_preserves_input_order_within_group():
    """组内保持输入顺序（时间排序由调用方决定）。"""
    groups = classify_commits([_commit("feat: 早"), _commit("feat: 晚")])

    assert [c.message for c in groups["feat"]] == ["feat: 早", "feat: 晚"]


def test_category_priorities_monotonic_with_fixed_order():
    """priority 与 CATEGORY_ORDER 单调一致（feat 最优先，other 最后）。"""
    priorities = [CATEGORY_BY_KEY[key].priority for key in CATEGORY_ORDER]
    assert priorities == sorted(priorities) == list(range(len(CATEGORY_ORDER)))
