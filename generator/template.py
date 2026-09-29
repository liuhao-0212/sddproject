"""日报模板：基于 jinja2 的 HTML 页面壳（design.md §4.2）。

- render_html(markdown, report_date, team_name) -> 完整 HTML 页面（可直接作为邮件正文）
- Markdown → HTML 内容转换由 generator/formatter.py 完成（延迟导入避免循环依赖）
- 内容已由 formatter 转义，模板以 safe 插入；title 等模板变量仍自动转义
"""

from __future__ import annotations

from datetime import date

from jinja2 import Environment

_PAGE_TEMPLATE = Environment().from_string("""<!DOCTYPE html>
<html lang="zh-CN">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>{{ title }}</title>
<style>
  body { font-family: -apple-system, "Segoe UI", "PingFang SC", "Microsoft YaHei",
         sans-serif; max-width: 800px; margin: 2rem auto; padding: 0 1rem;
         color: #24292f; line-height: 1.6; }
  h1 { border-bottom: 2px solid #d0d7de; padding-bottom: .4rem; }
  h2 { margin-top: 1.6rem; border-bottom: 1px solid #d0d7de; padding-bottom: .2rem; }
  h3 { color: #57606a; }
  li { margin: .2rem 0; }
</style>
</head>
<body>
{{ content | safe }}
</body>
</html>
""")


def render_html(markdown: str, report_date: date, team_name: str) -> str:
    """将 Markdown 日报转换为完整 HTML 页面。"""
    from generator.formatter import markdown_to_html  # 延迟导入避免循环依赖
    title = f"{team_name} 日报（{report_date.isoformat()}）"
    return _PAGE_TEMPLATE.render(title=title, content=markdown_to_html(markdown))
