"""日报历史存储：本地 SQLite（design.md ADR-002）。

- 定时任务场景（每天仅一次写入），每个操作独立开关连接，无需常驻
- 失败统一抛 StorageError，由调用方（main.py）记录日志
"""

from __future__ import annotations

import sqlite3
from contextlib import contextmanager
from datetime import date, datetime
from pathlib import Path
from typing import Any, Iterator

from shared.errors import StorageError

_SCHEMA = """
CREATE TABLE IF NOT EXISTS reports (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    date TEXT NOT NULL,
    team_name TEXT NOT NULL,
    markdown TEXT NOT NULL,
    html TEXT NOT NULL,
    generated_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_reports_date ON reports(date);
"""


class Storage:
    """SQLite 日报存储。创建实例时自动建库建表（父目录不存在则创建）。"""

    def __init__(self, db_path: str | Path):
        self.db_path = Path(db_path)
        self._init_schema()

    def _init_schema(self) -> None:
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        with self._connect() as conn:
            conn.executescript(_SCHEMA)

    @contextmanager
    def _connect(self) -> Iterator[sqlite3.Connection]:
        try:
            conn = sqlite3.connect(self.db_path)
        except sqlite3.Error as exc:
            raise StorageError(f"无法打开数据库 {self.db_path}: {exc}") from exc
        conn.row_factory = sqlite3.Row
        try:
            yield conn
            conn.commit()
        except sqlite3.Error as exc:
            conn.rollback()
            raise StorageError(f"SQLite 操作失败: {exc}") from exc
        finally:
            conn.close()

    def save_report(
        self,
        report_date: date | str,
        team_name: str,
        markdown: str,
        html: str,
        generated_at: datetime | None = None,
    ) -> int:
        """写入一条日报记录，返回记录 id。"""
        generated_at = generated_at or datetime.now()
        with self._connect() as conn:
            cursor = conn.execute(
                "INSERT INTO reports (date, team_name, markdown, html, generated_at)"
                " VALUES (?, ?, ?, ?, ?)",
                (self._date_str(report_date), team_name, markdown, html,
                 generated_at.isoformat()),
            )
            return int(cursor.lastrowid)

    def get_reports(self, report_date: date | str | None = None) -> list[dict[str, Any]]:
        """查询日报记录（按 id 倒序）；指定 report_date 时仅返回该日记录。"""
        with self._connect() as conn:
            if report_date is None:
                rows = conn.execute("SELECT * FROM reports ORDER BY id DESC").fetchall()
            else:
                rows = conn.execute(
                    "SELECT * FROM reports WHERE date = ? ORDER BY id DESC",
                    (self._date_str(report_date),),
                ).fetchall()
        return [dict(row) for row in rows]

    def get_latest_report(self) -> dict[str, Any] | None:
        """返回最新一条日报记录，无记录时返回 None。"""
        with self._connect() as conn:
            row = conn.execute("SELECT * FROM reports ORDER BY id DESC LIMIT 1").fetchone()
        return dict(row) if row else None

    @staticmethod
    def _date_str(value: date | str) -> str:
        return value.isoformat() if isinstance(value, date) else str(value)
