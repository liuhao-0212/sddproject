"""shared.storage 单元测试。"""

import sqlite3
from datetime import date, datetime, timedelta

import pytest

from shared.errors import StorageError
from shared.storage import Storage


@pytest.fixture
def storage(tmp_path):
    return Storage(tmp_path / "report.db")


def test_init_creates_db_and_table(storage, tmp_path):
    assert (tmp_path / "report.db").exists()
    conn = sqlite3.connect(tmp_path / "report.db")
    tables = {row[0] for row in conn.execute(
        "SELECT name FROM sqlite_master WHERE type = 'table'")}
    conn.close()
    assert "reports" in tables
    assert "daily_stats" in tables  # v1.6：与 reports 表并存，不影响既有表


def test_init_creates_missing_parent_dirs(tmp_path):
    db_path = tmp_path / "a" / "b" / "report.db"
    Storage(db_path)
    assert db_path.exists()


def test_save_and_query(storage):
    report_id = storage.save_report(
        date(2026, 9, 29), "研发一组", "# 日报\n- 张三：3 次提交", "<h1>日报</h1>",
    )
    assert report_id > 0
    rows = storage.get_reports()
    assert len(rows) == 1
    record = rows[0]
    assert record["date"] == "2026-09-29"
    assert record["team_name"] == "研发一组"
    assert record["markdown"] == "# 日报\n- 张三：3 次提交"
    assert record["html"] == "<h1>日报</h1>"
    assert record["generated_at"]


def test_query_filter_by_date(storage):
    storage.save_report(date(2026, 9, 28), "研发一组", "m1", "h1")
    storage.save_report("2026-09-29", "研发一组", "m2", "h2")
    rows = storage.get_reports(date(2026, 9, 29))
    assert len(rows) == 1
    assert rows[0]["markdown"] == "m2"


def test_get_latest_report(storage):
    storage.save_report(date(2026, 9, 28), "研发一组", "m1", "h1")
    storage.save_report(date(2026, 9, 29), "研发一组", "m2", "h2")
    latest = storage.get_latest_report()
    assert latest is not None
    assert latest["date"] == "2026-09-29"


def test_get_latest_report_empty_db(storage):
    assert storage.get_latest_report() is None


def test_db_error_wrapped_as_storage_error(tmp_path):
    with pytest.raises(StorageError):
        Storage(tmp_path)  # 传入目录路径，无法作为 SQLite 数据库打开


# ---------- v1.6：daily_stats 趋势统计（design.md §6.5） ----------

def test_save_stats_and_query_range_ascending(storage):
    storage.save_stats(date(2026, 9, 28), "研发一组", 3, 20, 5, 2)
    storage.save_stats(date(2026, 9, 29), "研发一组", 5, 40, 10, 4)
    storage.save_stats(date(2026, 9, 30), "研发一组", 1, 2, 1, 0)

    rows = storage.get_stats_between(date(2026, 9, 28), date(2026, 9, 29))
    assert [r["date"] for r in rows] == ["2026-09-28", "2026-09-29"]  # 升序
    assert rows[0]["team_name"] == "研发一组"
    assert rows[0]["commits_count"] == 3
    assert rows[0]["additions"] == 20
    assert rows[0]["deletions"] == 5
    assert rows[0]["messages_count"] == 2
    assert rows[1]["commits_count"] == 5


def test_save_stats_upserts_same_date(storage):
    """同一日期重复写入覆盖旧值（INSERT OR REPLACE），不产生重复行。"""
    storage.save_stats(date(2026, 9, 29), "研发一组", 3, 20, 5, 2)
    storage.save_stats(date(2026, 9, 29), "研发一组", 7, 30, 8, 6)

    rows = storage.get_stats_between(date(2026, 9, 29), date(2026, 9, 29))
    assert len(rows) == 1
    assert rows[0]["commits_count"] == 7
    assert rows[0]["additions"] == 30
    assert rows[0]["messages_count"] == 6


def test_get_stats_between_empty_range(storage):
    storage.save_stats(date(2026, 9, 28), "研发一组", 3, 20, 5, 2)
    assert storage.get_stats_between(date(2026, 9, 29), date(2026, 9, 30)) == []


# ---------- v1.7：list_recent_reports（只读历史查询，design.md §6.5） ----------

def test_list_recent_reports_left_joins_stats(storage):
    """最近 days 天按日期倒序；无 daily_stats 的日期计数为 None（LEFT JOIN 语义）。"""
    today = date.today()
    storage.save_report(today, "研发一组", "m1", "h1",
                        generated_at=datetime(2026, 10, 15, 8, 0))
    storage.save_report(today - timedelta(days=1), "研发一组", "m2", "h2",
                        generated_at=datetime(2026, 10, 14, 18, 0))
    storage.save_stats(today, "研发一组", 3, 20, 5, 2)  # 昨天不写 daily_stats

    rows = storage.list_recent_reports(30)

    assert [r["date"] for r in rows] == [today.isoformat(),
                                         (today - timedelta(days=1)).isoformat()]
    assert rows[0]["commits_count"] == 3
    assert rows[0]["additions"] == 20
    assert rows[0]["deletions"] == 5
    assert rows[1]["commits_count"] is None
    assert rows[1]["additions"] is None
    assert rows[1]["deletions"] is None


def test_list_recent_reports_uses_latest_generated_at(storage):
    """同一天多条日报 → generated_at 取该日最新一条。"""
    today = date.today()
    storage.save_report(today, "研发一组", "m1", "h1",
                        generated_at=datetime(2026, 10, 15, 8, 0))
    storage.save_report(today, "研发一组", "m2", "h2",
                        generated_at=datetime(2026, 10, 15, 20, 0))

    rows = storage.list_recent_reports(30)

    assert len(rows) == 1
    assert rows[0]["generated_at"] == datetime(2026, 10, 15, 20, 0).isoformat()


def test_list_recent_reports_respects_days_window(storage):
    """只返回最近 days 天内的日期，超出窗口的旧日报被排除。"""
    today = date.today()
    storage.save_report(today, "研发一组", "m1", "h1", generated_at=datetime.now())
    storage.save_report(today - timedelta(days=10), "研发一组", "m2", "h2",
                        generated_at=datetime.now())

    rows = storage.list_recent_reports(5)

    assert [r["date"] for r in rows] == [today.isoformat()]


def test_list_recent_reports_empty_db(storage):
    assert storage.list_recent_reports(30) == []
