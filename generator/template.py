"""日报模板：基于 jinja2 的 HTML 页面壳（design.md §4.2）。

- render_html(markdown, report_date, team_name) -> 完整 HTML 页面（可直接作为邮件正文）
- Markdown → HTML 内容转换由 generator/formatter.py 完成（延迟导入避免循环依赖）
- 内容已由 formatter 转义，模板以 safe 插入；title 等模板变量仍自动转义
- v1.2（design.md §6.5）：正文按 <h2> 节切分为卡片（数据源状态卡 + 各成员卡），
  状态区列表项渲染为徽章（✅/⚠️/－ 与 formatter 的状态图标对应）
"""

from __future__ import annotations

import re
from datetime import date

from jinja2 import Environment

# 状态图标 → 徽章配色类，图标与 formatter.ICON_* 常量一一对应（design.md §6.5）
_BADGE_CLASSES = {"✅": "badge-ok", "⚠️": "badge-warn", "－": "badge-off"}
_BADGE_ITEM = re.compile(r"<li>(✅|⚠️|－)\s*(.+?)</li>")

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
  h1 { font-size: 1.5rem; margin: .5rem 0 1.2rem; padding-bottom: .5rem;
       border-bottom: 2px solid #0969da; color: #1f2328; }
  .card { background: #ffffff; border: 1px solid #d0d7de; border-radius: 10px;
          padding: .9rem 1.2rem; margin: 1rem 0;
          box-shadow: 0 1px 2px rgba(31, 35, 40, .06); }
  .status-card { background: #f0f6ff; border-left: 4px solid #0969da; }
  .card h2 { margin-top: 0; }
  h2 { font-size: 1.2rem; border-bottom: 1px solid #d0d7de; padding-bottom: .35rem; }
  h3 { color: #0969da; border-left: 3px solid #0969da; padding-left: .45rem;
       margin: 1rem 0 .4rem; font-size: 1rem; }
  ul { margin: .3rem 0; padding-left: 1.5rem; }
  li { margin: .25rem 0; }
  .status-card ul { list-style: none; padding-left: 0; }
  .badge { display: inline-block; padding: .1rem .65rem; border-radius: 999px;
           font-size: .88rem; font-weight: 500; margin: .12rem 0; }
  .badge-ok { background: #dafbe1; color: #1a7f37; }
  .badge-warn { background: #fff8c5; color: #9a6700; }
  .badge-off { background: #eaeef2; color: #57606a; border: 1px solid #d0d7de; }
</style>
</head>
<body>
{{ content | safe }}
</body>
</html>
""")


def render_html(markdown: str, report_date: date, team_name: str) -> str:
    """将 Markdown 日报转换为完整 HTML 页面（v1.2：卡片式布局 + 状态徽章）。"""
    from generator.formatter import markdown_to_html  # 延迟导入避免循环依赖
    title = f"{team_name} 日报（{report_date.isoformat()}）"
    return _PAGE_TEMPLATE.render(title=title,
                                 content=_layout_sections(markdown_to_html(markdown)))


def _layout_sections(body_html: str) -> str:
    """按 <h2> 边界把正文切成卡片：数据源状态区为状态卡，各成员为成员卡。"""
    parts: list[str] = []
    for chunk in re.split(r"(?=<h2>)", body_html):
        if not chunk.strip():
            continue
        if chunk.startswith("<h2>数据源状态</h2>"):
            parts.append(f'<section class="card status-card">{_badge_items(chunk)}</section>')
        elif chunk.startswith("<h2>"):
            parts.append(f'<section class="card member-card">{chunk}</section>')
        else:
            parts.append(chunk)  # 头部（<h1> 标题等）
    return "\n".join(parts)


def _badge_items(section_html: str) -> str:
    """状态区每个列表项 → 状态徽章行（✅ 可用 / ⚠️ 调用失败 / － 未接入）。"""
    def badge(match: re.Match) -> str:
        icon, text = match.group(1), match.group(2)
        cls = _BADGE_CLASSES.get(icon, "badge-off")
        return f'<li><span class="badge {cls}">{icon} {text}</span></li>'
    return _BADGE_ITEM.sub(badge, section_html)
