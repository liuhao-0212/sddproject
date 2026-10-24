# 智能日报生成器 — 架构设计

## 1. 系统架构
- 架构模式：管道式架构（Pipeline Architecture）
- 数据流向：GitHub API / 飞书任务 API / 飞书消息 API / 飞书考勤 API
  → 采集层（原始 JSON）→ 生成层（Markdown 日报）→ 推送层（邮件 + 飞书消息）
- 共享基础层：配置管理、日志、错误处理、数据存储
- 选型理由：选择能满足需求的最简架构；proposal 已定“无需常驻服务”“5 人团队”，故不采用微服务或事件驱动架构

## 2. 模块职责
| 模块 | 文件 | 职责 | 不负责 |
|---|---|---|---|
| collector/ | github.py / lark_task.py / lark_msg.py / lark_attendance.py | 从外部 API 获取原始数据（GitHub Commit、飞书任务状态、飞书群消息、飞书考勤） | 不做数据格式化、不做去重判断、不做推送 |
| generator/ | formatter.py / template.py | 将原始数据组织为日报（数据整理 + Markdown 生成、日报模板管理） | 不做数据采集、不做 API 调用、不做推送 |
| notifier/ | email.py / lark_bot.py | 将日报推送给目标（SMTP 邮件、飞书机器人消息） | 不做数据处理、不做日报生成、不做数据采集 |
| shared/ | config.py / logger.py / errors.py / storage.py | 跨模块共享能力（配置读取校验、统一日志格式、自定义异常、SQLite 存储） | 不包含业务逻辑 |
| main.py | — | 编排入口：按顺序调用三层，处理全局异常，记录执行状态 | 不实现具体业务 |


## 3. 数据模型
### 3.1 原始记录
CommitRecord（代码提交记录）
- author: str          # 提交者（GitHub 用户名）
- message: str         # 提交信息（Commit Message）
- timestamp: datetime  # 提交时间
- repo: str            # 仓库名称
- additions: int       # 新增行数
- deletions: int       # 删除行数
- files_changed: int   # 变更文件数

TaskRecord（任务变更记录）
- assignee: str        # 负责人（飞书用户名）
- title: str           # 任务标题
- status_from: str     # 原状态
- status_to: str       # 新状态
- updated_at: datetime # 变更时间

MessageRecord（消息记录）
- sender: str          # 发送者（飞书用户名）
- content: str         # 消息内容（纯文本）
- timestamp: datetime  # 发送时间
- chat_name: str       # 群名称

AttendanceRecord（考勤记录）
- employee_id: str          # 飞书用户 ID
- date: date                # 考勤日期
- check_in: datetime | None # 签到时间
- check_out: datetime | None# 签退时间
- work_hours: float         # 工时（小时）
- status: str               # 正常 / 迟到 / 早退 / 缺勤 / 休假

### 3.2 日报对象
DailyReport（每日报告）
- date: date           # 日报日期
- team_name: str       # 团队名称
- members: list[MemberReport]  # 各成员的日报段落
- generated_at: datetime        # 生成时间
- markdown: str        # 完整 Markdown 格式日报
- html: str            # 完整 HTML 格式日报

MemberReport（成员报告）
- name: str            # 成员姓名
- github_username: str # GitHub 用户名
- commits: list[CommitRecord]  # 代码提交记录
- tasks: list[TaskRecord]      # 任务变更记录
- messages: list[MessageRecord] # 相关消息记录
- source_errors: dict[str, str] # 数据源失败标注（数据源 key → 原因）
- attendance: AttendanceRecord | None  # 考勤记录（v1.1 新增）

### 3.3 成员身份映射（config.yaml）
GitHub 用户名与飞书用户名需通过映射表关联：
members:
  - name: "张三"
    github: "zhangsan"
    lark: "zhangsan@company.com"
  - name: "李四"
    github: "lisi-dev"
    lark: "lisi@company.com"
  - name: "王五"
    github: "wangwu"
    lark: "wangwu@company.com"

## 4. 接口契约
```text
# 采集层
github.collect(repos: list[str], since: datetime, until: datetime) → list[CommitRecord]
lark_task.collect(project_id: str, since: datetime, until: datetime) → list[TaskRecord]
lark_msg.collect(chat_id: str, keywords: list[str], since: datetime, until: datetime) → list[MessageRecord]
lark_attendance.collect(since: date, until: date) → CollectResult[AttendanceRecord]

# 生成层
generator.generate(members: list[MemberReport], date: date, team_name: str) → DailyReport

# 推送层
email.send(report: DailyReport, recipients: list[str]) → bool
lark_bot.send(report: DailyReport, chat_id: str) → bool
```

## 5. 技术选型（ADR）

### ADR-001：使用 httpx 作为 HTTP 客户端
## 状态
已采纳
## 背景
智能日报生成器需要调用多个外部 API（GitHub API、飞书开放平台 API）获取数据，需选择一个稳定高效的 HTTP 客户端库。
## 选项
| 选项 | 优点 | 缺点 |
|---|---|---|
| requests | 社区最广泛、文档丰富、团队熟悉 | 不支持原生异步、连接池管理较弱 |
| httpx | 同时支持同步和异步、API 兼容 requests、支持 HTTP/2 | 相对较新、部分边缘场景文档不足 |
| aiohttp | 成熟的异步 HTTP 库 | 仅支持异步、API 风格与 requests 差异大 |
## 决策
使用 httpx
## 理由
1. proposal 要求“5 人团队日报生成 <60s”，三个数据源需并发采集，异步能力是关键
2. httpx 同步 API 与 requests 几乎一致，迁移成本极低
3. 未来可平滑切换异步，无需重写代码
4. 不选 aiohttp：无需强制异步，同步模式也必须能正常运行

### ADR-002：使用 SQLite 存储日报历史
## 状态
已采纳
## 背景
proposal 已要求“数据存储：本地 SQLite”，仍需正式评估其是否适合本场景。
## 选项
| 选项 | 优点 | 缺点 |
|---|---|---|
| SQLite | 零部署、零运维、Python 内置、文件级备份 | 不支持高并发写入、数据量极大时查询性能下降 |
| PostgreSQL | 功能完整、高并发、强全文搜索 | 需独立部署运维、对 5 人团队场景过重 |
| JSON 文件 | 最简单、无依赖 | 缺乏复杂查询、并发写不安全、难管理 |
## 决策
使用 SQLite
## 理由
1. 运行方式为“定时任务、无需常驻服务”，不存在并发写入（每天仅一次）
2. 5 人团队一年约 1250 条数据，SQLite 绰绰有余
3. 零运维符合项目定位
4. Python 标准库 sqlite3 开箱即用


### ADR-003：采集接口返回 CollectResult（v1.1 新增）

## 状态
已采纳

## 背景
v1.1 新增飞书考勤采集。考勤存在两种要区分的情况：
“接口调用失败” 与 “接口成功但数据缺失（如某人未签退）”。
调用方必须能明确区分，才能分别标注“考勤数据暂不可用”与“签退缺失”。

## 选项
| 选项 | 优点 | 缺点 |
|---|---|---|
| 返回裸 list（同 v1.0 三个采集器） | 与既有接口一致 | 无法从返回值区分“空”与“失败” |
| 返回 CollectResult[T] | 成功/失败显式可判 | 与 v1.0 三个采集器风格不一致 |

## 决策
考勤采集器返回 CollectResult[AttendanceRecord]

## 理由
1. 能否拿到数据必须由返回值本身表达（§6.1 要求“考勤不可用”显式标注）
2. 避免再引入模块级 last_error 的隐式状态
3. 已知代价：与 v1.0 三个采集器不一致；后续可评估统一（另开 ADR）

## CollectResult 定义
CollectResult[T]:
- success: bool        # 调用是否成功
- data: list[T]        # 成功时的数据（失败时为空列表）
- error: str | None    # 失败原因（成功时为 None）

## 6. 非功能性约束
### 6.1 错误处理策略（优雅降级）
| 场景 | 处理方式 |
|---|---|
| GitHub API 超时 | 重试 3 次（间隔 5s），仍失败则标记“数据获取失败” |
| 飞书 Token 过期 | 自动刷新 Token 后重试 1 次 |
| 单个数据源完全不可用 | 跳过该数据源，日报标注“xx 数据源暂不可用”，其他正常采集 |
| 所有数据源都不可用 | 记录错误日志、发送告警邮件，不生成空日报 |
| 邮件发送失败 | 重试 2 次，仍失败则记日志 + 飞书消息告警 |
| 飞书推送失败 | 重试 2 次，仍失败则记日志 + 邮件告警 |
- **告警触发条件**：仅当推送渠道**已配置**且发送失败时才触发备份告警；
  渠道未配置（如未设置 webhook/收件人）视为"未接入"，不告警

关键原则：
1. 采集层失败不阻塞生成层
2. 生成层失败不阻塞推送层（需推送错误报告）
3. 任何失败都必须有日志记录
4. 推送渠道互为备份告警通道

### 6.2 安全约束
- 密钥管理：API 密钥全部通过环境变量注入；严禁硬编码；.env 必须加入 .gitignore
- 数据安全：过滤飞书消息中的敏感关键词（薪资/绩效/裁员等，配置黑名单）；日报不含代码差异具体内容，仅统计（如 +10 行 / -5 行）
- 访问控制：仅采集配置文件中明确列出的仓库/群组，不采集范围之外的数据

### 6.3 可运维性设计
- 日志：JSON lines 格式；INFO（正常）+ ERROR（异常）；每次执行记录开始/结束时间、各数据源采集条数、推送结果
- 健康检查：提供 `main.py --check` 模式，验证 API 连接、邮件配置、飞书机器人权限
- 配置热更新：成员映射表 config.yaml 修改后下次执行自动生效，无需重启
- 历史查询：提供只读命令 `--history [--days N]`（列表）、`--show <日期>`（终端打印）、`--export <日期>`（导出 output/report-<日期>.md/.html）；只读命令不采集/不生成/不推送/不落库，纯本地读取（ADR-002 存储）

### 6.4 时区约定

- **"当日"的定义**：本地时区（系统时区）的自然日，**不是 UTC 日**
- **采集窗口**：本地时区当日 00:00 → 执行时刻；传给外部 API 时**必须携带时区信息**
  （ISO 8601 带偏移量，或 epoch 秒），**禁止传裸的 naive datetime**
- **展示**：日报中所有时间（提交 / 任务 / 消息 / 考勤）统一转换为**本地时区**后显示
- **日志**：沿用本地时区，与展示保持一致
- **理由**：naive datetime 会被 GitHub / 飞书按 UTC 解析，导致
  采集窗口整体偏移、漏数据、时间显示错误（东八区会偏 8 小时）

### 6.5 日报展示约定
- **面向读者**：团队 Leader（非技术人员），正文应避免技术细节
- **代码提交板块**：只展示 commit 标题 + 变更行数（+X/−Y），
  取 commit 消息的**第一行**（标题），忽略正文（如 Co-Authored-By）
- **失败展示分级**：
  - 「未接入」（缺凭据）→ 一句话「未接入，已跳过」，不显示原因
  - 「调用失败」（已接入但出错）→ 显示简短原因（不含堆栈、环境变量名）
- **异常细节**（堆栈、环境变量名）只进日志，不进日报正文

**v1.3 强化**：
- 结构：头部信息条（标题·团队名 / 日期含星期 / 数据时间范围）
  → 今日概览（汇总数字）→ 成员段落（▎成员 → ▪板块 → ·条目）→ 落款
- 必须删除：数据源健康状态区、GitHub 用户名、commit 的技术描述、内部 ID
- 必须新增：头部日期与数据范围、今日概览、落款（自动生成声明 + 数据来源 + 生成时间）
- 符号体系：▎分区 / ▪子板块 / ·条目
- 接口：generate(members, date, team_name, window=None)

**v1.4：日报展示失败原因（修订）**
- 数据源展示规则：
  - 有数据 → 正常显示
  - 已接入但当天无数据 → "今日无记录"（板块保留）
  - **不可用** → **板块保留 + 一行可读说明**（如"飞书接口受限（平台策略），暂不可用"）
  - 未接入（未配置）→ 不渲染
- **错误文案映射表**（技术错误 → 人话）：
  | 匹配关键词 | 文案 |
  |---|---|
  | platform policy / 平台 | 飞书接口受限（平台策略），暂不可用 |
  | secret / 凭据未配置 | 飞书凭证未配置 |
  | 403 / permission / 权限 | 飞书权限不足，暂不可用 |
  | timeout / 超时 / connect | 飞书接口连接失败 |
  | 其他 | 飞书数据暂不可用 |
- 日报正文**不出现**原始异常文本、环境变量名、堆栈

**v1.5：HTML 视觉强化**
- 布局：标题条 → 概览卡片组 → 成员板块卡片 → 落款
- 组件：
  - 标题条：深色背景（主色）+ 白字，含团队名/日期/数据范围
  - 概览卡片：一等宽列（flex），大号数字 + 小号标签
  - 板块卡片：浅色背景 + 圆角 + 内边距；标题前带图标
  - 状态徽章：圆角小标签（✅ 绿 / ⚠️ 橙）
  - 落款：浅灰背景、居中、小字
- 配色：主色 #2563eb（蓝）；成功 #16a34a；警告 #f59e0b；文字 #1f2937
- 响应式：@media (max-width: 600px) 概览卡片改为纵向堆叠
- 兼容性：用内联/简单 CSS（邮件客户端支持有限，避免 flex 复杂用法时提供降级）
- Markdown（飞书卡片）**结构不变**，只优化 HTML 模板

**v1.6：趋势与周报**
- 新增板块「本周概况」（位于今日概览之前）：
  - 本周（周一 ~ 当日）提交次数、变更行数、活跃天数
  - 较上周对比（提交次数 ±N）
- 今日概览增加「较昨日」提示（提交次数 ±N）
- 数据来源：本地存储的结构化统计（ADR-002）
  - 新增表 daily_stats(date, team_name, commits_count, additions, deletions, messages_count)
  - 每次生成日报后写入当天统计
- 无历史数据时显示「暂无历史数据」，不报错
- 接口：generate(members, date, team_name, window=None, history=None)

**v1.7：日报历史查询与导出（只读 CLI）**
- `--history [--days N]`：默认最近 30 天，按日期倒序；表头「日期 / 提交 / 变更行数 / 生成时间」
  - 数据来源：reports（日期与生成时间）+ daily_stats（计数，LEFT JOIN 语义）
  - 该日无 daily_stats 行时，计数列显示「—」，不报错
- `--show <YYYY-MM-DD>`：原样打印该日**最新**一条 Markdown，末尾附一行导出提示
- `--export <YYYY-MM-DD>`：写 output/report-<日期>.md 与 .html，打印写入路径
- 三条命令均不触发采集/生成/推送/落库
- 空结果文案：「暂无历史日报记录」/「未找到 YYYY-MM-DD 的日报记录」
- 日期解析：仅接受 YYYY-MM-DD，非法输入提示格式并返回退出码 1

**v1.8：日报内容智能化（提交归类 + 今日亮点）**
- 归类规则（只解析提交消息第一行），解析正则：`^\s*(?P<type>[a-zA-Z]+)(\([^)]*\))?!?:`
  | 前缀 | 类别 | 图标 |
  |---|---|---|
  | feat / feature | 新功能 | ✨ |
  | fix / bugfix / hotfix | 修复 | 🐛 |
  | perf | 性能 | ⚡ |
  | refactor | 重构 | ♻️ |
  | docs | 文档 | 📝 |
  | test / tests | 测试 | ✅ |
  | ci / build / chore | 工程 | 🔧 |
  | style | 样式 | 🎨 |
  | 无前缀 / 其他 | 其他 | 📦 |
- 类别固定顺序：feat → fix → perf → refactor → docs → test → ci → style → 其他
- 概览：有提交时追加「工作分类：✨ 新功能 N ｜ 🐛 修复 M …」（固定顺序，只列非零）
- 提交板块：先一行汇总，再按类别分组（`- · ✨ 新功能（N）` + 子条目 `  - 标题（+X/-Y 行）`），组内按提交时间升序
- 今日亮点：位于今日概览之后、成员段落之前；按优先级 feat > fix > perf > refactor > 其他，同类取最新，最多 3 条；当天无提交则不渲染该板块
- HTML：新增 highlights_html 区块，复用 `.card.member-card` 样式，不新增配色
- classify 逻辑放在 generator/classify.py（纯函数，便于单测）；generate(...) 签名不变

**v1.9：未归属记录归集（其他贡献）**
- 触发：编排层聚合时存在未能匹配到 config.members 的提交 / 任务 / 消息
- 结构：追加伪成员 MemberReport(name="其他贡献", is_unassigned=True)，排在所有成员之后
- 渲染：该成员只渲染有数据的板块，不输出「今日无记录」；考勤不参与归集，仍记日志
- 统计口径：概览、工作分类、今日亮点把「其他贡献」当作普通成员计入
- 团队级数据源可用性（_configured_sources）忽略伪成员，避免空 source_errors 影响判定
- 日志：未归属记录归入「其他贡献」，带 commits / tasks / messages 计数
- 接口：MemberReport 新增 is_unassigned: bool = False（默认值保持向后兼容）

### 6.8 仓库动态与趋势展示（v1.12）
- 仓库动态数据源（GitHub REST）：
  - PR：GET /repos/{repo}/pulls?state=all&sort=updated&direction=desc&per_page=100
  - Issue：GET /repos/{repo}/issues?state=all&since={since}&per_page=100（排除含 pull_request 的条目）
  - Release：GET /repos/{repo}/releases?per_page=30
- 归类口径：合并 PR 用 merged_at；关闭 PR 用 closed_at（merged_at 为空）；新建用 created_at；Release 用 published_at
- 展示顺序固定：合并 PR → 新建 PR → 关闭 Issue → 新建 Issue → 发布版本
- 每类最多 5 条，超出显示「另有 N 条」；无数据显示「今日无仓库动态」
- 7 天趋势：取 daily_stats 最近 7 个自然日提交数，缺失按 0；字符 ▁▂▃▄▅▆▇█ 按最大值归一化
- 移除协作沟通：SOURCE_ORDER / SECTIONS / 概览 / 落款同步删除 lark_msg；MessageRecord 与 lark_msg 采集模块保留
- daily_stats.messages_count 字段保留，写 0

### 6.9 飞书考勤接口契约（v1.13）
- 端点：POST /open-apis/attendance/v1/user_stats_datas/query?employee_type=employee_id
- 查询参数：employee_type（employee_id / employee_no）
- 请求体：locale=zh、stats_type=daily、start_date/end_date 为整数 yyyyMMdd、user_ids 为 employee_id 列表
- 员工标识：members[].attendance 优先；否则用 members[].lark（open_id）调
  GET /open-apis/contact/v3/users/{open_id}?user_id_type=open_id 取 data.user.employee_id
- 无法解析 employee_id → 按未接入处理；有 employee_id 但接口失败 → 板块显示可读原因
- 调试期：解析不出记录时记录原始响应（截断 2000 字符）

#### 修订（v1.13 实测校准）
- 改用 POST /open-apis/attendance/v1/user_tasks/query?employee_type=employee_id
- 请求体：user_ids / check_date_from / check_date_to（整数 yyyyMMdd）
- 响应：data.user_task_results[]，每项含 day / user_id / records[]
- 每条 record 含 check_in_record.check_time / check_out_record.check_time /
  check_in_result / check_out_result
- 员工 ID 解析顺序：members[].attendance → contact.user.employee_id → contact.user.user_id
- 同一天多条 record 合并：最早 check_in + 最晚 check_out；状态取第一条非「正常」
- 无记录返回空列表且 success=True（不是错误）


### 6.10 趋势校准与版本发布分类（v1.14）
- 趋势校准：_load_history 读取 daily_stats 后，用本次采集的 today_stats["commits"] 覆盖趋势最后一天（今日统计尚未落库）
- 分类新增：release 类别（🚀 版本发布），位于 fix 之后、perf 之前，优先级 2
- 前缀识别：vX.Y[:] / release: / publish: / version:（大小写不敏感）
- 其余分类与固定顺序保持不变
### 6.11 本地运行环境（v1.15）
- 加载顺序：系统环境变量 > `.env` > 代码默认值（`load_dotenv` 不覆盖已存在的变量）
- 解析规则：忽略空行与 `#` 注释；支持 `export KEY=VALUE`；值可带单/双引号；不展开变量
- 生效方式：`shared.config` 导入时自动调用 `load_dotenv()`，所有入口（main.py / show_report.py）共享
- 一键运行：`run.bat`（CMD）与 `run.ps1`（PowerShell），内容等价于 `uv run python main.py`
- 安全：`.env` 已在 `.gitignore`；仓库只提交 `.env.example`（占位值）

### 6.12 展示口径与预览工具（v1.16）
- 板块顺序：头部 → 本周累计 → 今日概览（含近 7 天趋势）→ 今日亮点 → 仓库动态 → 成员段落 → 落款
- 「本周累计」= 周一~当日累计 + 较上周对比，不再包含近 7 天趋势
- 「近 7 天」行位于今日概览末尾（Markdown）；HTML 位于概览卡片下方（template 的 trend_html 区块）
- show_report.py：复用编排层的采集与生成路径（含 history / repo_events），只写 output/demo-report.*，不推送、不落库

### 6.13 周报设计（v1.17）
- 入口：`python main.py --weekly`（互斥组新增 --weekly）
- 窗口：本周一 00:00 ~ 现在（本地时区，§6.4）；仓库动态使用同一窗口
- 数据：GitHub 提交（周窗口）+ daily_stats（本周与上周）+ github_activity 事件
- 结构：标题 → 本周概览 → 工作分类 → 每日趋势 → 仓库动态 → 成员排名 → 落款
- 输出：output/weekly-<YYYY-MM-DD>.md/.html；打印 Markdown；不推送、不落库
- 趋势：周一~今天逐日提交数，缺失按 0；字符 ▁▂▃▄▅▆▇█ + 峰值
- 排名：按提交次数降序（并列按新增行数），仅列有提交的成员

### 6.14 只读 Web 界面（v1.18）
- 入口：`python main.py --serve [--port 8000]`（或 `python webapp.py --port N`）
- 绑定：仅 127.0.0.1（本机访问），端口默认 8000，port=0 时由系统分配（测试用）
- 路由：`/` 首页列表；`/report/<YYYY-MM-DD>` 日报 HTML；`/report/<日期>.md` Markdown；`/weekly/<日期>` 周报 HTML
- 数据：日报读 reports 表（每日期取最新一条）；周报读 output/weekly-<日期>.html
- 只读：不写库、不推送；所有日期先做 YYYY-MM-DD 校验，路径不越出 output/
- 依赖：标准库 http.server + 现有 jinja2/存储层，不新增运行时依赖

### 6.15 HTML 趋势图（v1.19）
- 位置：HTML 日报概览卡片下方（template 的 trend 区块）
- 形式：内联 SVG（viewBox 560x130，7 根柱子），峰值柱用深色 #1d4ed8，其余 #2563eb
- 标注：柱顶数值 + 底部星期标签；aria-label="近 7 天提交趋势"
- 降级：SVG 下方保留字符趋势文字（v1.16 的 trend_html），无趋势数据时只显示「暂无趋势数据」
- 兼容：不依赖外部图片/JS；Markdown 与飞书卡片不包含 SVG
