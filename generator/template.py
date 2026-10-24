"""日报模板：基于 jinja2 的 HTML 页面壳（design.md §4.2、§6.5 v1.5）。

- render_html(structure) -> 完整 HTML 页面（可直接作为邮件正文）
- 输入为 formatter 组装的结构化数据（标题条 / 概览卡片 / 成员板块 / 落款）；
  板块正文与落款已由 formatter 转义并转换为 HTML，模板以 safe 插入，
  其余变量（团队名、日期、卡片数字等）仍由 jinja 自动转义
- v1.5（design.md §6.5）：深色标题条 → 概览卡片组 → 板块卡片（图标 + 状态徽章）→ 落款；
  邮件客户端兼容优先 table/inline-block，窄屏经 @media 纵向堆叠
- v1.6：标题条之后新增「本周概况」板块，复用成员卡片样式（.card.member-card，
  不引入新配色）；正文由 formatter 转换后以 safe 插入
- v1.8：概览卡片之后、成员板块之前新增「今日亮点」板块（有内容时渲染，
  highlights_html 由 formatter 转换后以 safe 插入），复用成员卡片样式、不新增配色
- v1.12：今日亮点之后、成员板块之前新增「仓库动态」板块（repo_html 由 formatter
  转换后以 safe 插入），同样复用成员卡片样式；未提供仓库动态时为空串、不渲染
"""

from __future__ import annotations

from typing import Any

from jinja2 import Environment

_PAGE_TEMPLATE = Environment().from_string("""<!DOCTYPE html>
<html lang="zh-CN">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>{{ team_name }} {{ report_name }}（{{ title_date }}）</title>
<style>
  body { font-family: -apple-system, "Segoe UI", "PingFang SC", "Microsoft YaHei",
         sans-serif; max-width: 860px; margin: 0 auto; padding: 1rem 1rem 2rem;
         background: #f6f8fa; color: #1f2937; line-height: 1.6; }
  /* 标题条：主色深底白字（团队名 + 日期含星期 + 数据范围） */
  .report-head { background: #2563eb; color: #ffffff; border-radius: 10px;
                 padding: 1.1rem 1.3rem; margin: 1rem 0; }
  .report-head h1 { font-size: 1.45rem; margin: 0 0 .3rem; color: #ffffff; }
  .report-meta { font-size: 12.5px; margin: 0; color: #dbeafe; }
  /* 概览卡片组：table 布局（邮件兼容），大号数字 + 小号标签 */
  .stat-row { width: 100%; border-collapse: separate; border-spacing: 6px; }
  .stat-card { background: #eff6ff; border: 1px solid #bfdbfe; border-radius: 8px;
               padding: .7rem .4rem; text-align: center; }
  .stat-value { display: block; font-size: 26px; font-weight: 700; color: #2563eb; }
  .stat-label { display: block; font-size: 12px; color: #1f2937; margin-top: 2px; }
  /* 近 7 天趋势（v1.16）：概览卡片下方的迷你趋势 */
  .trend { margin: .8rem 0 0; }
  .trend svg { display: block; max-width: 100%; height: auto; margin: 0 auto; }
  .trend ul { list-style: none; padding: 0; margin: .4rem 0 0; text-align: center; font-size: 13px; }
  /* 成员板块：浅色卡片 + 圆角 + 内边距 */
  .member-card { background: #f9fafb; border: 1px solid #e5e7eb; border-radius: 10px;
                 padding: .9rem 1.2rem; margin: 1rem 0; }
  .member-card h2 { font-size: 1.15rem; margin: .3rem 0 .6rem; color: #1f2937;
                    border-bottom: 1px solid #e5e7eb; padding-bottom: .4rem; }
  .section h3 { color: #2563eb; border-left: 3px solid #2563eb; padding-left: .45rem;
                margin: 1rem 0 .4rem; font-size: 1rem; }
  ul { margin: .3rem 0; padding-left: 1.5rem; }
  li { margin: .25rem 0; }
  /* 状态徽章：圆角小标签（✅ 正常 绿 / ⚠️ 不可用 橙） */
  .badge { display: inline-block; border-radius: 10px; padding: 0 8px;
           font-size: 11px; font-weight: 500; vertical-align: 1px; }
  .badge-ok { background: #dcfce7; color: #16a34a; }
  .badge-warn { background: #fef3c7; color: #f59e0b; }
  /* 落款：浅灰背景、居中、小字 */
  .report-foot { background: #f3f4f6; border-radius: 10px; text-align: center;
                 padding: .8rem 1rem; margin: 1.5rem 0 1rem; }
  .report-foot p { color: #6b7280; font-size: 12px; margin: 4px 0; }
  /* 窄屏（手机）：概览卡片纵向堆叠 */
  @media (max-width: 600px) {
    .stat-row, .stat-row tr, .stat-card { display: block !important; width: 100% !important; }
    .stat-card { margin: 6px 0; box-sizing: border-box; }
  }
</style>
</head>
<body>
<header class="report-head">
  <h1>📊 {{ team_name }} {{ report_name }}</h1>
  <p class="report-meta">📅 {{ date_text }}{% if range_text %} · 数据范围 {{ range_text }}{% endif %}</p>
</header>
{% if overview_cards %}
<section class="overview">
  <table class="stat-row" role="presentation" cellpadding="0" cellspacing="0">
    <tr>
      {% for card in overview_cards %}
      <td class="stat-card">
        <span class="stat-value">{{ card.value }}</span>
        <span class="stat-label">{{ card.label }}</span>
      </td>
      {% endfor %}
    </tr>
  </table>
  {% if trend_svg or trend_html %}
  <div class="trend">
    {% if trend_svg %}{{ trend_svg | safe }}{% endif %}
    {{ trend_html | safe }}
  </div>
  {% endif %}
</section>
{% endif %}
{% if highlights_html %}
<section class="card member-card">
  {{ highlights_html | safe }}
</section>
{% endif %}
{% if repo_html %}
<section class="card member-card">
  {{ repo_html | safe }}
</section>
{% endif %}
{% for member in member_sections %}
<section class="card member-card">
  <h2>▎{{ member.name }}</h2>
  {% for section in member.sections %}
  <div class="section">
    <h3>{{ section.icon }} ▪ {{ section.title }}{% if section.status == "warn" %} <span class="badge badge-warn">⚠️ 不可用</span>{% else %} <span class="badge badge-ok">✅ 正常</span>{% endif %}</h3>
    {{ section.body_html | safe }}
  </div>
  {% endfor %}
</section>
{% endfor %}
<footer class="report-foot">
  {{ footer_html | safe }}
</footer>
</body>
</html>
""")


def render_html(structure: dict[str, Any]) -> str:
    """渲染完整 HTML 页面（design.md §6.5 v1.5）：
    标题条 → 概览卡片组 → 成员板块卡片 → 落款。

    structure 由 formatter 组装（标题 / 概览卡片 / 板块与状态 / 落款）；
    板块正文与落款已转义并转换为 HTML，模板以 safe 插入。
    """
    return _PAGE_TEMPLATE.render(**structure)
