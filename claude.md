# CLAUDE.md

## 项目概述
智能日报生成器，详见 specs/proposal.md

## 规范文件索引
- 需求规范: specs/proposal.md
- 架构设计: specs/design.md
- 任务清单: specs/tasks.md

## 核心约束（从规范中提取）
- 开发语言: Python 3.11+
- 架构: 管道式（采集层 → 生成层 → 推送层 + 共享基础层）
- HTTP 客户端: httpx
- 数据存储: 本地 SQLite
- 运行方式: 定时任务，无需常驻服务
- 错误处理: 优雅降级，采集失败标注“数据获取失败”，禁止静默跳过

## 当前迭代
正在执行 v1.1 迭代：Task 11（考勤采集）+ 修改 Task 6 / Task 9，详见 specs/tasks.md