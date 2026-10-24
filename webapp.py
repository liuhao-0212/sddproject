"""只读 Web 界面（v1.18，design.md §6.14）：本地浏览历史日报与周报。

用法：
    python main.py --serve [--port 8000]
    python webapp.py --port 8000

约束：
- 仅绑定 127.0.0.1（本机访问）
- 只读：只查询 reports 表、只读取 output/weekly-*.html，不写库、不推送
- 使用标准库 http.server，无新增依赖
"""
from __future__ import annotations

import argparse
import html
import re
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import unquote, urlparse

from shared.config import load_config
from shared.storage import Storage

_DATE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")
_WEEKLY_RE = re.compile(r"^weekly-(\d{4}-\d{2}-\d{2})\.html$")


def report_dates(storage: Storage, limit: int = 30) -> list[dict]:
    """最近 N 个有日报的日期（每个日期取最新一条，按日期倒序）。"""
    latest: dict[str, dict] = {}
    for row in storage.get_reports(None):
        latest.setdefault(str(row["date"]), row)
    return list(latest.values())[:limit]


def weekly_dates(output_dir: Path) -> list[str]:
    """output 目录下已有周报的日期（倒序）。"""
    if not output_dir.is_dir():
        return []
    dates: list[str] = []
    for file in output_dir.glob("weekly-*.html"):
        match = _WEEKLY_RE.match(file.name)
        if match:
            dates.append(match.group(1))
    return sorted(set(dates), reverse=True)


def render_index(daily: list[dict], weekly: list[str]) -> str:
    """首页 HTML：日报与周报列表。"""
    daily_items = "".join(
        f'<li><a href="/report/{html.escape(str(row["date"]))}">'
        f'{html.escape(str(row["date"]))}</a> '
        f'<a class="md" href="/report/{html.escape(str(row["date"]))}.md">Markdown</a></li>'
        for row in daily
    ) or "<li>暂无日报记录</li>"
    weekly_items = "".join(
        f'<li><a href="/weekly/{html.escape(day)}">{html.escape(day)}</a></li>'
        for day in weekly
    ) or "<li>暂无周报记录</li>"
    return f"""<!DOCTYPE html>
<html lang="zh-CN">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>智能日报 · 历史</title>
<style>
  body {{ font-family: -apple-system, "Segoe UI", "PingFang SC", "Microsoft YaHei",
         sans-serif; max-width: 760px; margin: 0 auto; padding: 2rem 1rem;
         background: #f6f8fa; color: #1f2937; }}
  h1 {{ color: #2563eb; }}
  h2 {{ border-left: 4px solid #2563eb; padding-left: .5rem; margin-top: 1.8rem; }}
  ul {{ list-style: none; padding: 0; }}
  li {{ background: #fff; border: 1px solid #e5e7eb; border-radius: 8px;
        padding: .6rem .9rem; margin: .4rem 0; }}
  a {{ color: #2563eb; text-decoration: none; }}
  a:hover {{ text-decoration: underline; }}
  a.md {{ font-size: 12px; color: #6b7280; margin-left: .5rem; }}
</style>
</head>
<body>
<h1>📊 智能日报 · 历史</h1>
<h2>日报</h2>
<ul>{daily_items}</ul>
<h2>周报</h2>
<ul>{weekly_items}</ul>
<p style="color:#6b7280;font-size:12px;margin-top:2rem">
只读界面 · 数据来自本地 SQLite 与 output/ 目录
</p>
</body>
</html>
"""


def make_server(db_path: str | Path, output_dir: str | Path,
                host: str = "127.0.0.1", port: int = 8000) -> ThreadingHTTPServer:
    """构造只读 HTTP 服务器（port=0 时由系统分配端口，便于测试）。"""
    storage = Storage(db_path)
    out_dir = Path(output_dir)

    class Handler(BaseHTTPRequestHandler):
        server_version = "DailyReportWeb/1.18"

        def do_GET(self) -> None:  # noqa: N802 - BaseHTTPRequestHandler 约定
            parsed = urlparse(self.path)
            route = unquote(parsed.path)

            if route in ("/", ""):
                body = render_index(report_dates(storage), weekly_dates(out_dir))
                self._send(200, body, "text/html; charset=utf-8")
                return

            if route.startswith("/report/"):
                name = route[len("/report/"):]
                if name.endswith(".md"):
                    self._send_report_markdown(name[:-3])
                else:
                    self._send_report_html(name)
                return

            if route.startswith("/weekly/"):
                self._send_weekly(route[len("/weekly/"):])
                return

            self._send(404, "<h1>404 Not Found</h1>", "text/html; charset=utf-8")

        def _send_report_html(self, day: str) -> None:
            if not _DATE_RE.match(day):
                self._send(404, "<h1>404 Not Found</h1>", "text/html; charset=utf-8")
                return
            rows = storage.get_reports(day)
            if not rows:
                self._send(404, f"<h1>未找到 {html.escape(day)} 的日报</h1>",
                           "text/html; charset=utf-8")
                return
            self._send(200, str(rows[0]["html"]), "text/html; charset=utf-8")

        def _send_report_markdown(self, day: str) -> None:
            if not _DATE_RE.match(day):
                self._send(404, "未找到该日报", "text/plain; charset=utf-8")
                return
            rows = storage.get_reports(day)
            if not rows:
                self._send(404, f"未找到 {day} 的日报", "text/plain; charset=utf-8")
                return
            self._send(200, str(rows[0]["markdown"]), "text/plain; charset=utf-8")

        def _send_weekly(self, day: str) -> None:
            if not _DATE_RE.match(day):
                self._send(404, "<h1>404 Not Found</h1>", "text/html; charset=utf-8")
                return
            file = out_dir / f"weekly-{day}.html"
            if not file.is_file():
                self._send(404, f"<h1>未找到 {html.escape(day)} 的周报</h1>",
                           "text/html; charset=utf-8")
                return
            self._send(200, file.read_text(encoding="utf-8"), "text/html; charset=utf-8")

        def _send(self, status: int, body: str, content_type: str) -> None:
            payload = body.encode("utf-8")
            self.send_response(status)
            self.send_header("Content-Type", content_type)
            self.send_header("Content-Length", str(len(payload)))
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            self.wfile.write(payload)

        def log_message(self, format: str, *args) -> None:  # noqa: A002
            # 默认日志打到 stderr；这里静默，避免测试与正常运行输出噪音
            return

    return ThreadingHTTPServer((host, port), Handler)


def serve(port: int = 8000) -> int:
    """启动只读 Web 界面（阻塞，Ctrl+C 退出）。"""
    cfg = load_config()
    db_path = str(cfg.get("storage", {}).get("db_path", "data/report.db"))
    server = make_server(db_path, Path("output"), port=port)
    host, actual_port = server.server_address[0], server.server_address[1]
    print(f"只读 Web 界面：http://{host}:{actual_port}/ （Ctrl+C 退出）")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="智能日报只读 Web 界面")
    parser.add_argument("--port", type=int, default=8000, help="监听端口（默认 8000）")
    args = parser.parse_args(argv)
    return serve(args.port)


if __name__ == "__main__":
    raise SystemExit(main())
