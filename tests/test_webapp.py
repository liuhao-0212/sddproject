"""只读 Web 界面测试（v1.18）。"""
import threading
import urllib.error
import urllib.request

import main
from shared.storage import Storage
from webapp import make_server, render_index


def _seed(tmp_path):
    db = tmp_path / "report.db"
    storage = Storage(db)
    storage.save_report("2026-10-23", "刘昊", "# 日报A", "<html><body>日报A</body></html>")
    storage.save_report("2026-10-24", "刘昊", "# 日报B", "<html><body>日报B</body></html>")
    out = tmp_path / "output"
    out.mkdir()
    (out / "weekly-2026-10-24.html").write_text(
        "<html><body>周报24</body></html>", encoding="utf-8")
    return db, out


def _get(port, path):
    url = f"http://127.0.0.1:{port}{path}"
    try:
        with urllib.request.urlopen(url, timeout=5) as resp:
            return resp.status, resp.read().decode("utf-8")
    except urllib.error.HTTPError as exc:
        return exc.code, exc.read().decode("utf-8")


def test_web_index_and_pages(tmp_path):
    db, out = _seed(tmp_path)
    server = make_server(db, out, port=0)
    port = server.server_address[1]
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        status, body = _get(port, "/")
        assert status == 200
        assert "2026-10-24" in body and "2026-10-23" in body
        assert "/weekly/2026-10-24" in body

        status, body = _get(port, "/report/2026-10-24")
        assert status == 200 and "日报B" in body

        status, body = _get(port, "/report/2026-10-24.md")
        assert status == 200 and body == "# 日报B"

        status, body = _get(port, "/weekly/2026-10-24")
        assert status == 200 and "周报24" in body

        status, _ = _get(port, "/report/1999-01-01")
        assert status == 404
        status, _ = _get(port, "/weekly/1999-01-01")
        assert status == 404
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)


def test_render_index_empty():
    body = render_index([], [])
    assert "暂无日报记录" in body and "暂无周报记录" in body


def test_serve_cli_dispatch(monkeypatch):
    monkeypatch.setattr(main, "serve_web", lambda port: 3)
    assert main.main(["--serve", "--port", "8123"]) == 3
