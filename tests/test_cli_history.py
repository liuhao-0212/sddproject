"""v1.7 只读 CLI（--history / --show / --export）测试（tasks.md Task 17）。

- 用临时 SQLite 写入多天日报与统计（至少一天不写 daily_stats）
- 经 main.main([...]) + capsys 断言输出与退出码，零真实网络调用
"""

from datetime import date, datetime, timedelta

import pytest

import main
from shared.storage import Storage

TODAY = date.today()

HISTORY_HEADER = "日期        提交  变更行数       生成时间"


def _iso(d: date) -> str:
    return d.isoformat()


@pytest.fixture
def cli_env(monkeypatch, tmp_path):
    """打桩 main 的配置读取，指向临时 SQLite；只读命令不触碰其余模块。"""
    db_path = tmp_path / "report.db"
    monkeypatch.setattr(main, "load_config",
                        lambda: {"storage": {"db_path": str(db_path)}})
    return {"db_path": db_path, "storage": Storage(db_path)}


def _seed(storage: Storage, days: int = 3, stats_days: int = 2) -> None:
    """写入 days 天日报；最近 stats_days 天（含今天）写入 daily_stats。"""
    for i in range(days):
        d = TODAY - timedelta(days=i)
        storage.save_report(
            d, "研发一组",
            f"# 研发一组 智能日报\n\n📅 {_iso(d)}\n",
            f"<h1>{_iso(d)}</h1>",
            generated_at=datetime(2026, 1, 1, 18, 0) - timedelta(days=i),
        )
    for i in range(stats_days):
        d = TODAY - timedelta(days=i)
        storage.save_stats(d, "研发一组", 3 + i, 20 + i, 5 + i, 2 + i)


# ---------- --history ----------

def test_history_lists_days_desc_with_placeholder(monkeypatch, cli_env, capsys):
    _seed(cli_env["storage"], days=3, stats_days=2)  # 前天无 daily_stats

    rc = main.main(["--history"])

    out = capsys.readouterr().out
    assert rc == 0
    assert out.splitlines()[0] == HISTORY_HEADER
    d0, d1, d2 = TODAY, TODAY - timedelta(days=1), TODAY - timedelta(days=2)
    assert out.index(_iso(d0)) < out.index(_iso(d1)) < out.index(_iso(d2))  # 日期倒序
    assert "+20/-5" in out   # 今天 3 次提交 +20/-5
    assert "+21/-6" in out   # 昨天 4 次提交 +21/-6
    assert "—" in out        # 前天无 daily_stats → 计数列显示 —
    assert "18:00" in out    # 生成时间展示为 YYYY-MM-DD HH:MM
    assert "2026-01-01 18:00" in out


def test_history_empty_db_prints_placeholder(monkeypatch, cli_env, capsys):
    rc = main.main(["--history"])

    out = capsys.readouterr().out
    assert rc == 0
    assert out == "暂无历史日报记录\n"


def test_history_days_limits_range(monkeypatch, cli_env, capsys):
    _seed(cli_env["storage"], days=3, stats_days=2)

    rc = main.main(["--history", "--days", "2"])

    out = capsys.readouterr().out
    assert rc == 0
    assert _iso(TODAY) in out
    assert _iso(TODAY - timedelta(days=1)) in out
    assert _iso(TODAY - timedelta(days=2)) not in out  # 超出 2 天窗口


@pytest.mark.parametrize("bad_days", ["0", "-3", "abc", "1.5"])
def test_history_invalid_days(monkeypatch, cli_env, capsys, bad_days):
    rc = main.main(["--history", "--days", bad_days])

    out = capsys.readouterr().out
    assert rc == 1
    assert out == "天数必须是正整数\n"


# ---------- --show ----------

def test_show_prints_latest_markdown_verbatim(monkeypatch, cli_env, capsys):
    storage = cli_env["storage"]
    d = TODAY - timedelta(days=1)
    storage.save_report(d, "研发一组", "旧的一条", "<h1>old</h1>",
                        generated_at=datetime(2026, 1, 1, 9, 0))
    storage.save_report(d, "研发一组", "# 最新一条\n\n- 内容", "<h1>new</h1>",
                        generated_at=datetime(2026, 1, 1, 18, 0))

    rc = main.main(["--show", _iso(d)])

    out = capsys.readouterr().out
    assert rc == 0
    assert out == (f"# 最新一条\n\n- 内容\n\n"
                   f"提示：可执行 python main.py --export {_iso(d)} 导出为文件\n")


def test_show_markdown_without_trailing_newline(monkeypatch, cli_env, capsys):
    """存储内容不以换行结尾时，空行与提示仍正确（原样打印 + 补换行）。"""
    d = TODAY - timedelta(days=1)
    cli_env["storage"].save_report(d, "研发一组", "无结尾换行", "<h1>x</h1>",
                                   generated_at=datetime(2026, 1, 1, 18, 0))

    rc = main.main(["--show", _iso(d)])

    out = capsys.readouterr().out
    assert rc == 0
    assert out == f"无结尾换行\n\n提示：可执行 python main.py --export {_iso(d)} 导出为文件\n"


def test_show_not_found_returns_1(monkeypatch, cli_env, capsys):
    d = _iso(TODAY - timedelta(days=100))

    rc = main.main(["--show", d])

    out = capsys.readouterr().out
    assert rc == 1
    assert out == f"未找到 {d} 的日报记录\n"


@pytest.mark.parametrize("bad_date", ["abc", "2026-9-1", "2026-13-01", "20260901"])
def test_show_invalid_date_returns_1(monkeypatch, cli_env, capsys, bad_date):
    rc = main.main(["--show", bad_date])

    out = capsys.readouterr().out
    assert rc == 1
    assert out == "日期格式错误，应为 YYYY-MM-DD\n"


# ---------- --export ----------

def test_export_writes_md_and_html(monkeypatch, cli_env, capsys, tmp_path):
    d = TODAY - timedelta(days=1)
    cli_env["storage"].save_report(d, "研发一组", "# markdown 正文", "<h1>html 正文</h1>",
                                   generated_at=datetime(2026, 1, 1, 18, 0))
    monkeypatch.chdir(tmp_path)  # 导出目录 output/ 落到临时目录，不污染仓库

    rc = main.main(["--export", _iso(d)])

    out = capsys.readouterr().out
    assert rc == 0
    assert (tmp_path / "output" / f"report-{_iso(d)}.md").read_text(encoding="utf-8") \
        == "# markdown 正文"
    assert (tmp_path / "output" / f"report-{_iso(d)}.html").read_text(encoding="utf-8") \
        == "<h1>html 正文</h1>"
    assert out == (f"已导出：output/report-{_iso(d)}.md\n"
                   f"已导出：output/report-{_iso(d)}.html\n")


def test_export_not_found_returns_1(monkeypatch, cli_env, capsys, tmp_path):
    monkeypatch.chdir(tmp_path)
    d = _iso(TODAY - timedelta(days=100))

    rc = main.main(["--export", d])

    out = capsys.readouterr().out
    assert rc == 1
    assert out == f"未找到 {d} 的日报记录\n"
    assert not (tmp_path / "output").exists()  # 无记录时不产生导出文件


# ---------- 只读性与既有入口不变 ----------

def test_readonly_commands_never_run_pipeline(monkeypatch, cli_env, tmp_path):
    """v1.7：三条只读命令不采集/不生成/不推送/不落库（触碰 run/generate 即失败）。"""
    _seed(cli_env["storage"])
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(main, "run",
                        lambda dry_run=False: pytest.fail("只读命令不应执行完整流程"))
    monkeypatch.setattr(main, "generate",
                        lambda *a, **kw: pytest.fail("只读命令不应生成日报"))

    assert main.main(["--history"]) == 0
    assert main.main(["--show", _iso(TODAY)]) == 0
    assert main.main(["--export", _iso(TODAY)]) == 0


def test_no_args_still_runs_full_pipeline(monkeypatch, capsys):
    """v1.7：不带参数行为不变（完整流程）。"""
    calls = []
    monkeypatch.setattr(main, "run", lambda dry_run: (calls.append(dry_run), 0)[1])

    rc = main.main([])

    assert rc == 0
    assert calls == [False]


def test_check_and_dry_run_dispatch_unchanged(monkeypatch, capsys):
    """v1.7：--check / --dry-run 的分发行为不变。"""
    calls = []
    monkeypatch.setattr(main, "check", lambda: (calls.append("check"), 0)[1])
    monkeypatch.setattr(main, "run", lambda dry_run: (calls.append(("run", dry_run)), 0)[1])

    assert main.main(["--check"]) == 0
    assert main.main(["--dry-run"]) == 0

    assert calls == ["check", ("run", True)]
