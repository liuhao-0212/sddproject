"""周报生成测试（v1.17）。"""
from datetime import date, datetime, timezone

import main
from collector.github import CommitRecord
from collector.github_activity import RepoEvent
from generator.formatter import MemberReport
from generator.weekly import generate_weekly

START = date(2026, 10, 19)
END = date(2026, 10, 24)
TZ = timezone.utc


def _commit(message, additions=10, deletions=2, day=20, hour=9):
    return CommitRecord(author="liuhao", message=message,
                        timestamp=datetime(2026, 10, day, hour, tzinfo=TZ),
                        repo="org/repo", additions=additions, deletions=deletions,
                        files_changed=1)


def _member(commits):
    return MemberReport(name="liuhao", github_username="liuhao", commits=commits)


def _stats(day, commits):
    return {"date": day, "team_name": "刘昊", "commits_count": commits,
            "additions": 0, "deletions": 0, "messages_count": 0}


def _generate(members, last_rows=None, events=None):
    return generate_weekly(members, START, END, last_rows or [], events or [], "刘昊",
                           generated_at=datetime(2026, 10, 24, 10, 0))


def test_weekly_overview_categories_trend_repo_and_ranking():
    member = _member([_commit("feat: A", 100, 10, day=20),
                      _commit("v1.17: B", 50, 5, day=24)])
    events = [RepoEvent(kind="pr_merged", number=9, title="v1.17: 周报",
                        timestamp=datetime(2026, 10, 22, 12, tzinfo=TZ), repo="org/repo")]

    report = _generate([member], last_rows=[_stats("2026-10-13", 1)], events=events)
    md = report.markdown

    assert "# 📊 刘昊 智能周报" in md
    assert "- · 提交 2 次 ｜ 变更 +150 / -15 行" in md
    assert "- · 活跃 2 天 ｜ 📈 较上周：提交 +1 次" in md
    assert "✨ 新功能 1" in md and "🚀 版本发布 1" in md
    assert "周二 1" in md and "周六 1" in md
    assert "合并 PR（1）" in md and "#9 v1.17: 周报" in md
    assert "- · 1. liuhao — 2 次提交，+150 / -15 行" in md
    assert "智能周报" in report.html and "<h2>" in report.html


def test_weekly_empty_placeholders():
    md = _generate([]).markdown
    assert "- · 本周无提交" in md
    assert "- · 本周无仓库动态" in md
    assert "## ▎成员排名" in md


def test_weekly_cli_dispatch(monkeypatch):
    monkeypatch.setattr(main, "weekly", lambda: 7)
    assert main.main(["--weekly"]) == 7
