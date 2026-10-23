# 智能日报生成器 — 任务清单

## 元信息
- 关联规范: specs/proposal.md、specs/design.md
- 任务总数: 11
- 预计总执行时间: 3 到 4h（包含测试）
- 执行策略: 按依赖关系顺序执行，独立任务可并行执行

---

## Task 1: 创建项目结构和基础配置
描述: 初始化项目目录结构，创建 pyproject.toml，安装核心依赖，创建配置文件模板
输入: specs/design.md（模块职责定义）
输出: 项目根目录结构 + pyproject.toml + config.yaml.example
依赖: 无

验收标准:
- [x] 目录结构符合 design.md §2 的模块划分（collector/、generator/、notifier/、shared/、tests/）
- [x] pyproject.toml 包含 httpx、pyyaml、jinja2 依赖
- [x] uv sync 成功安装所有依赖
- [x] config.yaml.example 包含 members 映射表模板

---

## Task 2: 实现共享基础层
描述: 实现配置读取、日志记录、错误处理和 SQLite 存储 4 个共享模块
输入: config.yaml.example（配置格式）、design.md §6（非功能约束）
输出: shared/config.py、shared/logger.py、shared/errors.py、shared/storage.py
依赖: Task 1

验收标准:
- [x] config.py 能读取 config.yaml 并返回配置对象
- [x] config.py 在配置缺少必填字段时抛出明确的错误信息
- [x] logger.py 输出 JSON lines 格式日志
- [x] errors.py 定义 CollectorError、GeneratorError、NotifierError 三个自定义异常
- [x] storage.py 能创建 SQLite，写入和查询日报记录
- [x] 所有模块包含对应的单元测试，测试通过

---

## Task 3: 实现 GitHub 采集模块
描述: 调用 GitHub API 获取指定仓库在指定时间范围内的 Commit 记录，返回 CommitRecord 列表
输入: design.md §3（CommitRecord 数据模型）、design.md §4.1（采集层接口契约）
输出: collector/github.py + tests/test_github.py
依赖: Task 2

验收标准:
- [x] collect() 函数签名符合 design.md §4.1 的接口定义
- [x] 返回的每个 CommitRecord 包含全部 7 个字段且类型正确
- [x] 支持分页查询（GitHub API 默认每页 30 条）
- [x] API 超时重试 3 次（间隔 5s），若仍失败则返回空列表 + 错误日志
- [x] GitHub API 限流（HTTP 403）时，等待 reset 时间后重试
- [x] 使用 Mock 数据的单元测试全部通过

---

## Task 4: 实现飞书任务采集模块
描述: 调用飞书开放平台 API 获取指定项目中当日状态变更的任务列表，返回 TaskRecord 列表
输入: design.md §3（TaskRecord 数据模型）、design.md §4.1（采集层接口契约）
输出: collector/lark_task.py + tests/test_lark_task.py
依赖: Task 2

验收标准:
- [x] collect() 函数签名符合 design.md §4.1 的接口定义
- [x] 返回的每个 TaskRecord 包含全部 5 个字段且类型正确
- [x] 飞书 Token 过期时自动刷新后重试 1 次
- [x] API 超时重试 3 次，若仍失败则返回空列表 + 错误日志
- [x] 使用 Mock 数据的单元测试全部通过

---

## Task 5: 实现飞书消息采集模块
描述: 调用飞书开放平台 API 获取指定群的消息记录，按关键词过滤，返回 MessageRecord 列表
输入: design.md §3（MessageRecord 数据模型）、design.md §4.1（采集层接口契约）
输出: collector/lark_msg.py + tests/test_lark_msg.py
依赖: Task 2

验收标准:
- [x] collect() 函数签名符合 design.md §4.1 的接口定义
- [x] 返回的每个 MessageRecord 包含全部 4 个字段且类型正确
- [x] 关键词过滤正确，只返回包含指定关键词的消息
- [x] 敏感关键词（薪资、绩效等）被正确过滤，不出现在结果中
- [x] 飞书 Token 过期时自动刷新后重试 1 次
- [x] 使用 Mock 数据的单元测试全部通过

---

## Task 6: 实现日报生成模块
描述: 接收各成员的采集数据，按“代码提交→任务进展→工时统计→协作沟通”组织日报内容，输出 Markdown 和 HTML 格式文件
输入: design.md §3（MemberReport、DailyReport 数据模型）、design.md §4.2（生成层接口契约）
输出: generator/formatter.py、generator/template.py + tests/test_generator.py
依赖: Task 2、Task 11

验收标准:
- [x] generate() 函数签名符合 design.md §4.2 的接口定义
- [x] 输出的 DailyReport.markdown 包含三个部分标题
- [x] 输出的 DailyReport.html 可被浏览器正确渲染
- [x] 若某成员当日无任何记录，日报中需要显示“今日无记录”
- [x] 若某数据源采集失败，日报中需要标注“数据获取失败”
- [x] Markdown 到 HTML 的转换格式正确
- [x] 使用 Mock 数据的单元测试全部通过
- [x] 日报内容必须包含每位团队成员的工时统计（v1.1 新增）
- [x] 当考勤数据不可用时，界面需要显示“考勤数据暂不可用”（v1.1 新增）
- [x] 布局顺序调整：工时统计模块应位于“任务进展”之后、“协作沟通”之前（v1.1 新增）

---

## Task 7: 实现邮件推送模块
描述: 将 HTML 格式日报通过 SMTP 发送给指定收件人
输入: design.md §4.3（推送层接口契约）
输出: notifier/email.py + tests/test_email.py
依赖: Task 6

验收标准:
- [x] send() 函数签名符合 design.md §4.3 的接口定义
- [x] 邮件主题包含日期和团队名称
- [x] 邮件正文为 HTML 格式的日报内容
- [x] 发送失败时重试 2 次，仍失败则返回 False + 错误日志
- [x] 使用 Mock SMTP 的单元测试通过

---

## Task 8: 实现飞书推送模块
描述: 将 Markdown 格式日报通过飞书机器人 webhook 发送到指定群
输入: design.md §4.3（推送层接口契约）
输出: notifier/lark_bot.py + tests/test_lark_bot.py
依赖: Task 6

验收标准:
- [x] send() 函数签名符合 design.md §4.3 的接口定义
- [x] 发送的消息为 Markdown 格式
- [x] 发送失败时重试 2 次，仍失败则返回 False + 错误日志
- [x] 使用 Mock webhook 的单元测试通过

---

## Task 9: 实现主编排入口
描述: 创建 main.py 作为系统入口，按顺序编排“采集→聚合→生成→推送”的完整流程
输入: 所有模块的接口
输出: main.py
依赖: Task 3、Task 4、Task 5、Task 6、Task 7、Task 8

验收标准:
- [x] python main.py 执行完整的日报生成流程
- [x] python main.py --check 验证所有 API 连接和配置
- [x] python main.py --dry-run 执行采集和生成但不推送
- [x] 若单个数据源失败，不应影响其他数据源的采集
- [x] 若所有数据源失败，不生成空日报，记录错误日志
- [x] 执行日志包含：开始时间、各数据源数据采集条数、推送结果、结束时间

---

## Task 10: 集成测试
描述: 编写端到端集成测试，验证完整流程在各种场景下的行为
输入: main.py + 所有模块
输出: tests/test_integration.py
依赖: Task 9

验收标准:
- [x] 正常场景：所有数据源可用，日报生成并推送成功
- [x] 降级场景：若某个数据源超时，日报中需要标注“数据获取失败”，其他部分正常
- [x] 空数据场景：若某成员当日无任何记录，日报中需要显示“今日无记录”
- [x] 全部失败场景：若所有数据源不可用，不生成日报，记录错误日志
- [x] 执行耗时少于 60s（Mock 环境下）

---

---

## Task 11: 实现考勤数据采集模块（新增 v1.1）
描述: 对接飞书考勤 API，获取团队成员的签到与签退数据
输入: design.md §3（AttendanceRecord 数据模型）、design.md §4.1（采集层接口契约）
输出: collector/lark_attendance.py + tests/test_attendance.py
依赖: Task 2（共享基础层）

验收标准:
- [x] collect() 函数签名严格符合 design.md 的接口定义
- [x] 返回 CollectResult 对象（包含 success 标志位）
- [x] 逻辑正确计算工时（check_out - check_in）
- [x] 妥善处理缺失签退数据的情况（状态标注为“签退缺失”）
- [x] 当 API 不可用时，success 置为 False 并附带详细错误信息
- [x] 基于 Mock 数据的单元测试全部通过

## Task 12: 优化日报展示（v1.2）
描述: 日报顶部新增数据源状态区；代码提交板块通俗化（只留标题+行数）；失败板块简化；HTML 美化
输入: design.md §6.5、proposal.md §3.5
输出: generator/formatter.py、generator/template.py、main.py + tests
依赖: Task 6

验收标准:
- [ ] 日报顶部含数据源状态区（每源显示 可用/未接入 + 条数）
- [ ] 代码提交只显示 commit 标题 + 行数（无 Co-Authored-By / 堆栈 / 环境变量名）
- [ ] 缺凭据的板块显示"未接入，已跳过"（一句话）
- [ ] HTML 邮件样式美化（卡片/状态徽章/配色）
- [ ] 全部测试通过（含原有 148 个）

## Task 13: 日报可读性强化（v1.3）
描述: 加头部信息条/今日概览/落款；去掉技术细节与运维信息；未接入源不显示
输入: design.md §6.5(v1.3)、proposal.md §3.6
输出: generator/formatter.py、generator/template.py、main.py + tests
依赖: Task 6

验收标准:
- [ ] 头部含 标题/团队名/日期(星期)/数据时间范围
- [ ] 含"今日概览"汇总数字
- [ ] 底部含 自动生成声明/数据来源/生成时间
- [ ] 不出现内部 ID、技术描述、数据源健康状态
- [ ] 未接入的源不显示；已接入但失败的仍显示
- [ ] 全部测试通过

## Task 14: 日报聚焦可用数据（v1.4）
描述: 未接入/不可用的数据源在日报中完全不出现；失败信息只进日志；数据来源只列实际呈现的源
输入: design.md §6.5(v1.4)、proposal.md §3.7
输出: generator/formatter.py、main.py + tests
依赖: Task 13

验收标准:
- [ ] 不可用的数据源：板块保留 + 一行可读说明（不隐藏）
- [ ] 说明文案按错误类型映射，不含技术细节
- [ ] 未接入（未配置）的源：不渲染
- [ ] 有数据 / 无数据："今日无记录"
- [ ] 落款「数据来源」列出所有【已接入】的源
- [ ] 失败仍记日志 + 告警
- [ ] 全部测试通过

## Task 15: HTML 邮件视觉强化（v1.5）
描述: 重做 HTML 模板——标题条/概览卡片/板块卡片/状态徽章/落款，统一配色与响应式
输入: design.md §6.5(v1.5)、proposal.md §3.8
输出: generator/template.py（+ 必要调整）+ tests
依赖: Task 14

验收标准:
- [ ] 邮件含深色标题条（团队名+日期+范围）
- [ ] 概览为卡片组（大号数字，横向排列）
- [ ] 各板块为浅色卡片 + 图标
- [ ] 状态为徽章（✅ 正常 / ⚠️ 不可用）
- [ ] 窄屏（<600px）卡片纵向堆叠
- [ ] Markdown / 飞书卡片结构不变
- [ ] 全部测试通过

## Task 16: 趋势与周报（v1.6）
描述: 新增「本周概况」板块（本周统计 + 较上周对比）；今日概览加「较昨日」
输入: design.md §6.5(v1.6)、proposal.md §3.9
输出: shared/storage.py、main.py、generator/formatter.py、generator/template.py + tests
依赖: Task 15

验收标准:
- [ ] storage 新增 daily_stats 表（创建/写入/按日期区间查询）
- [ ] 日报含「本周概况」（提交次数/变更行数/活跃天数）
- [ ] 含「较上周」「较昨日」对比
- [ ] 无历史数据时显示「暂无历史数据」，不报错
- [ ] HTML 模板同步（新板块用已有卡片样式）
- [ ] Markdown 输出结构向后兼容
- [ ] 全部测试通过

## Task 17: 日报历史查询与导出（v1.7）
描述: 新增只读 CLI 命令 --history / --history --days N / --show / --export，读取本地 SQLite 日报
输入: design.md §6.3、§6.5(v1.7)、proposal.md §3.10
输出: shared/storage.py（新增只读查询方法）、main.py（argparse + 处理函数）+ tests
依赖: Task 16

验收标准:
- [ ] --history 按日期倒序列出（默认 30 天），含日期/提交次数/变更行数/生成时间
- [ ] --history --days N 生效；N 非法（0/负数/非数字）时提示并退出码 1
- [ ] --show <日期> 打印该日最新 Markdown
- [ ] --export <日期> 生成 output/report-<日期>.md 与 .html
- [ ] 三个命令只读（不采集/不生成/不推送/不落库）
- [ ] 无记录 / 日期非法 → 友好提示，不抛堆栈
- [ ] --check / --dry-run / 无参数行为不变
- [ ] 全部测试通过

## Task 18: 日报内容智能化（v1.8）
描述: 提交按 Conventional Commits 归类；概览新增「工作分类」；成员提交板块分组；新增「今日亮点」板块
输入: design.md §6.5(v1.8)、proposal.md §3.11
输出: generator/classify.py（新增）、generator/formatter.py、generator/template.py、tests（新增 + 更新）
依赖: Task 17

验收标准:
- [ ] classify_commit 处理 feat/fix/perf/refactor/docs/test/ci/build/chore/style、scope、`!`、无前缀
- [ ] 概览「工作分类」行按固定顺序只列非零项；无提交时不显示
- [ ] 提交板块按类别分组，组内按时间升序，组顺序固定
- [ ] 「今日亮点」最多 3 条，优先级 feat > fix > perf > refactor > 其他，同类取最新
- [ ] 无提交时「今日亮点」板块不渲染（Markdown 与 HTML 一致）
- [ ] HTML 新板块复用现有卡片样式，无新增配色
- [ ] 既有测试全绿，新增分类/亮点用例

## Task 19: 未归属记录归集（v1.9）
描述: 未匹配到成员的提交/任务/消息归入「其他贡献」段落，不再丢弃；概览与亮点同步计入
输入: design.md §6.5(v1.9)、proposal.md §3.12
输出: generator/formatter.py、main.py、config.yaml.example、pipeline/fixtures 新增夹具、tests
依赖: Task 18

验收标准:
- [ ] 未归属提交/任务/消息归入「其他贡献」，记录完整、顺序稳定
- [ ] 仅在有未归属记录时出现，且排在成员段之后
- [ ] 只渲染有数据的板块（无「今日无记录」噪音）
- [ ] 概览、工作分类、今日亮点计入未归属记录
- [ ] 未归属考勤仍只记日志
- [ ] 全部测试通过

## Task 22: 仓库动态 + 趋势，移除协作沟通（v1.12）
描述: 删除协作沟通渲染；新增仓库动态（PR/Issue/Release）与近 7 天趋势
输入: design.md §6.8、proposal.md §3.15
输出: collector/github_activity.py（新增）、generator/formatter.py、generator/template.py、main.py、config.yaml.example、pipeline/fixtures、tests
依赖: Task 21

验收标准:
- [ ] 协作沟通不再出现在 Markdown/HTML/概览/落款
- [ ] 仓库动态五类事件正确采集与展示，上限与空态正确
- [ ] 7 天趋势与峰值正确，缺数据降级
- [ ] 飞书消息采集默认关闭
- [ ] 全部测试通过

## Task 23: 飞书考勤接入修复（v1.13）
描述: 修正考勤接口路径与请求契约，补 employee_id 解析，让工时统计恢复可用
输入: design.md §6.9、proposal.md §3.16
输出: collector/lark_attendance.py、tests/test_attendance.py、config.yaml.example
依赖: Task 22

验收标准:
- [ ] 使用 user_stats_datas/query + employee_type 查询参数
- [ ] 请求体字段与官方契约一致
- [ ] employee_id 来源：config.attendance 优先，其次通讯录解析
- [ ] 解析失败记录真实响应；404 文案不再误导
- [ ] 全部测试通过

## Task 24: 趋势校准 + 版本发布分类（v1.14）
描述: 修正近 7 天趋势缺少当天数据；新增「版本发布」提交类别
输入: design.md §6.10、proposal.md §3.17
输出: main.py（_load_history）、generator/classify.py、tests
依赖: Task 23

验收标准:
- [ ] 趋势最后一天使用本次采集的当日提交数
- [ ] 当日无提交时趋势最后一天为 0
- [ ] vX.Y: / release: / publish: / version: 归入「版本发布」
- [ ] 工作分类与今日亮点按新类别展示
- [ ] 全部测试通过

## Task 25: 本地运行体验（v1.15）
描述: 自动加载 .env、提供 .env.example 与一键运行脚本
输入: design.md §6.11、proposal.md §3.18
输出: shared/config.py、.env.example、run.bat、run.ps1、README、tests
依赖: Task 24

验收标准:
- [ ] 导入 shared.config 自动加载 .env，且不覆盖已有环境变量
- [ ] 解析支持注释/空行/export/引号
- [ ] .env 缺失时无副作用
- [ ] .env.example 与一键运行脚本已提供
- [ ] 全部测试通过

## 执行顺序
阶段 1: Task 1（项目初始化）
阶段 2: Task 2（共享基础层）
阶段 3: Task 3 + Task 4 + Task 5 + Task 11 并行（采集模块）
阶段 4: Task 6（日报生成模块，含 v1.1 工时展示）
阶段 5: Task 7 + Task 8 并行（两个推送模块）
阶段 6: Task 9（主编排入口）
阶段 7: Task 10（集成测试）