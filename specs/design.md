# 智能日报生成器 — 架构设计

## 1. 系统架构
- 架构模式：管道式架构（Pipeline Architecture）
- 数据流向：GitHub API / 飞书任务 API / 飞书消息 API
  → 采集层（原始 JSON）→ 生成层（Markdown 日报）→ 推送层（邮件 + 飞书消息）
- 共享基础层：配置管理、日志、错误处理、数据存储
- 选型理由：选择能满足需求的最简架构；proposal 已定“无需常驻服务”“5 人团队”，故不采用微服务或事件驱动架构

## 2. 模块职责
| 模块 | 文件 | 职责 | 不负责 |
|---|---|---|---|
| collector/ | github.py / lark_task.py / lark_msg.py | 从外部 API 获取原始数据（GitHub Commit、飞书任务状态、飞书群消息） | 不做数据格式化、不做去重判断、不做推送 |
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
- source_errors: dict[str, str] # 数据源失败标注（数据源 key → 原因），由编排层填入

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
# 采集层
```test
github.collect(repos: list[str], since: datetime, until: datetime) → list[CommitRecord]
lark_task.collect(project_id: str, since: datetime, until: datetime) → list[TaskRecord]
lark_msg.collect(chat_id: str, keywords: list[str], since: datetime, until: datetime) → list[MessageRecord]
```

# 生成层
generator.generate(members: list[MemberReport], date: date, team_name: str) → DailyReport

# 推送层
email.send(report: DailyReport, recipients: list[str]) → bool
lark_bot.send(report: DailyReport, chat_id: str) → bool

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