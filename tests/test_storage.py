"""shared.storage 单元测试。"""

import sqlite3
from datetime import date

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
