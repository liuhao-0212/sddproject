# 智能日报生成器 — 任务清单

## 元信息
- 关联规范: specs/proposal.md、specs/design.md
- 任务总数: 10
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
描述: 接收各成员的采集数据，按“代码提交→任务进展→协作沟通”组织日报内容，输出 Markdown 和 HTML 格式文件
输入: design.md §3（MemberReport、DailyReport 数据模型）、design.md §4.2（生成层接口契约）
输出: generator/formatter.py、generator/template.py + tests/test_generator.py
依赖: Task 2

验收标准:
- [x] generate() 函数签名符合 design.md §4.2 的接口定义
- [x] 输出的 DailyReport.markdown 包含三个部分标题
- [x] 输出的 DailyReport.html 可被浏览器正确渲染
- [x] 若某成员当日无任何记录，日报中需要显示“今日无记录”
- [x] 若某数据源采集失败，日报中需要标注“数据获取失败”
- [x] Markdown 到 HTML 的转换格式正确
- [x] 使用 Mock 数据的单元测试全部通过

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
- [ ] send() 函数签名符合 design.md §4.3 的接口定义
- [ ] 发送的消息为 Markdown 格式
- [ ] 发送失败时重试 2 次，仍失败则返回 False + 错误日志
- [ ] 使用 Mock webhook 的单元测试通过

---

## Task 9: 实现主编排入口
描述: 创建 main.py 作为系统入口，按顺序编排“采集→聚合→生成→推送”的完整流程
输入: 所有模块的接口
输出: main.py
依赖: Task 3、Task 4、Task 5、Task 6、Task 7、Task 8

验收标准:
- [ ] python main.py 执行完整的日报生成流程
- [ ] python main.py --check 验证所有 API 连接和配置
- [ ] python main.py --dry-run 执行采集和生成但不推送
- [ ] 若单个数据源失败，不应影响其他数据源的采集
- [ ] 若所有数据源失败，不生成空日报，记录错误日志
- [ ] 执行日志包含：开始时间、各数据源数据采集条数、推送结果、结束时间

---

## Task 10: 集成测试
描述: 编写端到端集成测试，验证完整流程在各种场景下的行为
输入: main.py + 所有模块
输出: tests/test_integration.py
依赖: Task 9

验收标准:
- [ ] 正常场景：所有数据源可用，日报生成并推送成功
- [ ] 降级场景：若某个数据源超时，日报中需要标注“数据获取失败”，其他部分正常
- [ ] 空数据场景：若某成员当日无任何记录，日报中需要显示“今日无记录”
- [ ] 全部失败场景：若所有数据源不可用，不生成日报，记录错误日志
- [ ] 执行耗时少于 60s（Mock 环境下）

---

## 执行顺序
阶段 1: Task 1（项目初始化）
阶段 2: Task 2（共享基础层）
阶段 3: Task 3 + Task 4 + Task 5 并行（三个采集模块）
阶段 4: Task 6（日报生成模块）
阶段 5: Task 7 + Task 8 并行（两个推送模块）
阶段 6: Task 9（主编排入口）
阶段 7: Task 10（集成测试）