# 智能日报生成器（SDD 实战复现）

> 用《SDD 实战：规范驱动开发之道》的**规范驱动开发（Specification-Driven Development）**方法论，
> 完整复现贯穿全书的实战项目——从需求规范到可运行系统的全流程。

---

## 1. 项目简介

一个自动化日报工具：从团队现有的协作平台采集当日工作数据，聚合生成结构化日报，并推送给相关干系人。

- **采集**：GitHub Commit、飞书任务、飞书群消息、飞书考勤（v1.1）
- **生成**：按「代码提交 → 任务进展 → 工时统计 → 协作沟通」四段式编排
- **推送**：SMTP 邮件（HTML）+ 飞书机器人（Markdown）
- **降级**：单数据源失败不阻断其他源，日报中显式标注"数据获取失败"

## 2. 方法论：规范先行

本项目**不是直接写代码**，而是遵循 SDD 的六阶段工作流：

```
proposal.md（做什么） → design.md（怎么做） → tasks.md（按什么顺序交付） → 代码 → 验证 → 迭代
```

三份规范位于 `specs/`，所有代码都**可追溯到具体规范条款**：

| 规范 | 回答的问题 | 关键内容 |
|---|---|---|
| `specs/proposal.md` | 要解决什么问题 | 背景/目标、功能范围（做什么/不做什么）、可量化验收标准 |
| `specs/design.md` | 用什么结构解决 | 管道式架构、模块职责、数据模型、接口契约、ADR 技术决策、非功能约束 |
| `specs/tasks.md` | 按什么顺序交付 | 11 个任务，每个含输入/输出/依赖/可测试的验收标准 |

**核心理念**：先撰写规范，再编写代码。规范是给 AI（和人）的"工程接口"，
解决 Vibe Coding 常见的"需求蒸发"与"上下文漂移"问题。

## 3. 项目结构

```
.
├── specs/                    # 三份核心规范（人类领地，AI 只读）
│   ├── proposal.md
│   ├── design.md
│   └── tasks.md
├── collector/                # 采集层
│   ├── github.py             #   GitHub Commit
│   ├── lark_task.py          #   飞书任务
│   ├── lark_msg.py           #   飞书群消息
│   ├── lark_attendance.py    #   飞书考勤（v1.1）
│   └── _lark.py              #   飞书采集公共能力（Token/重试）
├── generator/                # 生成层
│   ├── formatter.py          #   数据整理 + Markdown/HTML 生成
│   └── template.py           #   日报模板
├── notifier/                 # 推送层
│   ├── email.py              #   SMTP 邮件
│   └── lark_bot.py           #   飞书机器人
├── shared/                   # 共享基础层
│   ├── config.py             #   配置读取与校验
│   ├── logger.py             #   JSON lines 日志
│   ├── errors.py             #   异常体系
│   └── storage.py            #   SQLite 日报历史
├── scripts/                  # 规范护栏（护栏三明治）
│   ├── input_guard.py        #   改代码前：拦截改规范/硬编码密钥
│   ├── bash_guard.py         #   拦截通过 shell 绕过规范保护
│   └── output_guard.py       #   改代码后：测试不过即拦截
├── .claude/settings.json     # Claude Code hooks 配置
├── .github/workflows/        # 规范 CI
│   └── spec-check.yml        #   PR 时自动检查规范完整性
├── tests/                    # 146 个测试
├── main.py                   # 编排入口
├── config.yaml.example       # 配置模板
└── CLAUDE.md                 # 规范索引（对抗 AI 上下文漂移）
```

## 4. 快速开始

### 环境要求
- Python 3.11+
- [uv](https://docs.astral.sh/uv/)（包管理器）

### 安装
```bash
uv sync
```

### 配置
```bash
cp config.yaml.example config.yaml
# 编辑 config.yaml：填入团队名、成员映射表、仓库、飞书 group/project、邮件收件人等
```

### 密钥（一律通过环境变量注入，禁止写入配置文件）
| 变量 | 用途 |
|---|---|
| `GITHUB_TOKEN` | GitHub API 访问 |
| `LARK_APP_ID` / `LARK_APP_SECRET` | 飞书自建应用凭据 |
| `SMTP_USER` / `SMTP_PASSWORD` / `SMTP_FROM` | 邮件发送 |
| `LARK_BOT_WEBHOOK_URL` | 飞书机器人 webhook |

### 运行
```bash
python main.py --check      # 健康检查：验证配置与各 API 连通性
python main.py --dry-run    # 采集 + 生成（不推送、不落库）
python main.py              # 完整流程：采集 → 聚合 → 生成 → 推送 → 落库
```

## 5. 测试

```bash
uv run pytest -q      # 146 passed
```

覆盖：单元测试（各模块）+ 端到端集成测试（正常/降级/空数据/全失败四种场景）。

## 6. 工程实践

### 优雅降级（design.md §6.1）
- 单个数据源失败**不阻断**其他源，日报中显式标注"数据获取失败"，严禁静默跳过
- 推送渠道**互为备份告警通道**：邮件失败经飞书告警，飞书失败经邮件告警
- 任何失败都必须有日志记录

### 规范护栏（第 8 章 Agent 模式）
- `input_guard.py`：AI 改代码前拦截——禁止改 `specs/`、禁止硬编码密钥、禁止开发已排除功能
- `bash_guard.py`：封堵"用 Bash 绕过护栏"的路径
- `output_guard.py`：AI 改代码后自动跑测试，不过则拦截

### 规范 CI（第 9 章）
`.github/workflows/spec-check.yml` 在提 PR 时自动检查：
- 🔴 硬错误：三份规范必须存在、proposal 必须有"不做什么"章节
- 🟡 警告：改了代码未同步 `specs/`

## 7. 迭代记录

| 版本 | 内容 | 对应规范变更 |
|---|---|---|
| v1.0 | 三源采集 + 日报生成 + 邮件/飞书推送 | 初始三份规范 + 10 个任务 |
| v1.1 | 新增飞书考勤采集 + 日报"工时统计"板块 | 三份规范同步更新（新增 `AttendanceRecord`、ADR-003） |
| 修复 | 采集器异常兜底、时区缺陷（采集窗口与展示统一本地时区） | design.md 新增 §6.4 时区约定 |

## 8. 参考

- 书：《SDD 实战：规范驱动开发之道》（黄佳 著，人民邮电出版社）
- 方法论：Specification-Driven Development（规范驱动开发）

---

*本项目为学习性复现，演示 SDD 方法论在真实项目中的应用。*