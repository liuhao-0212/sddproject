"""日报模板：基于 jinja2 的 HTML 页面壳（design.md §4.2）。

- render_html(markdown, report_date, team_name) -> 完整 HTML 页面（可直接作为邮件正文）
- Markdown → HTML 内容转换由 generator/formatter.py 完成（延迟导入避免循环依赖）
- 内容已由 formatter 转义，模板以 safe 插入；title 等模板变量仍自动转义
- v1.3（design.md §6.5）：头部信息卡 + 今日概览卡 + 成员卡 + 落款，适合邮件阅读
"""

from __future__ import annotations

import re
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
         sans-serif; max-width: 860px; margin: 0 auto; padding: 1.25rem 1rem 2rem;
         background: #f6f8fa; color: #24292f; line-height: 1.6; }
  .report-head { background: #ffffff; border: 1px solid #d0d7de;
                 border-left: 4px solid #0969da; border-radius: 10px;
                 padding: 1.1rem 1.3rem; margin: 1rem 0;
                 box-shadow: 0 1px 2px rgba(31, 35, 40, .06); }
  .report-head h1 { font-size: 1.45rem; margin: 0 0 .35rem; color: #1f2328; }
  .report-head p { color: #57606a; font-size: 12.5px; margin: 0;
                   font-family: 'JetBrains Mono', ui-monospace, Menlo, monospace; }
  .card { background: #ffffff; border: 1px solid #d0d7de; border-radius: 10px;
          padding: .9rem 1.2rem; margin: 1rem 0;
          box-shadow: 0 1px 2px rgba(31, 35, 40, .06); }
  .overview-card { background: #f0f6ff; border-left: 4px solid #0969da; }
  .overview-card h2 { font-size: 1.05rem; }
  .overview-card ul { list-style: none; padding-left: 0; margin: .3rem 0; }
  .overview-card li { padding: 3px 0; font-size: 13px; }
  .card h2 { margin-top: 0; }
  h2 { font-size: 1.2rem; border-bottom: 1px solid #d0d7de; padding-bottom: .35rem; }
  h3 { color: #0969da; border-left: 3px solid #0969da; padding-left: .45rem;
       margin: 1rem 0 .4rem; font-size: 1rem; }
  ul { margin: .3rem 0; padding-left: 1.5rem; }
  li { margin: .25rem 0; }
  hr.rule { border: 0; border-top: 1px solid #d0d7de; margin: 1.5rem 0 .75rem; }
  .report-foot { margin: 0 0 1.5rem; color: #57606a; font-size: 12.5px; }
  .report-foot p { font-family: 'JetBrains Mono', ui-monospace, Menlo, monospace;
                   font-size: 11.5px; margin-top: 4px; }
</style>
</head>
<body>
{{ content | safe }}
</body>
</html>
""")


def render_html(markdown: str, report_date: date, team_name: str) -> str:
    """将 Markdown 日报转换为完整 HTML 页面（v1.3：头部卡 + 概览卡 + 成员卡 + 落款）。"""
    from generator.formatter import REPORT_NAME, markdown_to_html  # 延迟导入避免循环依赖
    title = f"{team_name} {REPORT_NAME}（{report_date.isoformat()}）"
    return _PAGE_TEMPLATE.render(title=title,
                                 content=_layout_sections(markdown_to_html(markdown)))


def _layout_sections(body_html: str) -> str:
    """按 <h2> 边界把正文切成结构块：头部信息卡 → 概览卡 → 成员卡 → 落款。"""
    parts: list[str] = []
    for chunk in re.split(r"(?=<h2>)", body_html):
        if not chunk.strip():
            continue
        # 落款以 🤖 段落开始（前有 hr），从最后一个成员卡中切出
        footer = ""
        if "<p>🤖" in chunk:
            cut = chunk.index("<p>🤖")
            hr = chunk.rfind("<hr", 0, cut)
            if hr != -1:
                cut = hr
            footer = chunk[cut:]
            chunk = chunk[:cut]
        if chunk.startswith("<h2>▎今日概览</h2>"):
            parts.append(f'<section class="card overview-card">{chunk}</section>')
        elif chunk.startswith("<h2>"):
            parts.append(f'<section class="card member-card">{chunk}</section>')
        else:
            parts.append(f'<header class="report-head">{chunk}</header>')
        if footer:
            parts.append(f'<footer class="report-foot">{footer}</footer>')
    return "\n".join(parts)
