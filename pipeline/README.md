# CI/CD 实训：给「智能日报生成系统」装上流水线

这个目录是你的 SDD 项目（`D:\projectsdd`）的**实训副本**，在它上面补全了整套 CI/CD：

- 原有内容一字未改：`main.py`、`collector/`、`generator/`、`notifier/`、`shared/`、`scripts/`、`specs/`、`tests/`
- 新增内容：`.github/workflows/`（云端流水线）+ `pipeline/`（本机流水线）+ `Dockerfile`
- 项目自己的 `.venv`、`config.yaml`、`data/`、`output/` 没有复制过来（它们本来就不该进仓库）

---

## 1. 30 秒跑起来

在本目录（项目根）打开 PowerShell：

```powershell
# 1) 建项目环境（你原来的机器上已经做过，这里只是说明）
uv sync

# 2) 准备本地配置（config.yaml 被 .gitignore 忽略，需要自己生成）
Copy-Item config.yaml.example config.yaml

# 3) 跑整条流水线（会自动通过生产审批门禁）
powershell -ExecutionPolicy Bypass -File pipeline\run_pipeline.ps1 -AutoApprove
```

预期：13 个阶段依次执行，最后在 `reports/pipeline-report.md` 生成运行报告，
在 `dist/` 生成制品，在 `environments/prod/` 留下发布记录，在 `reports/verify-prod/`
生成一份真实的日报（Markdown + HTML）。

> 如果你的机器上 `.venv` 可用，脚本会自动优先使用它 —— **流水线必须在项目自己的环境里跑**。

---

## 2. 13 个阶段（本地 ↔ 云端）

| # | 本地阶段 | 云端 job | 做什么 | 失败意味着什么 |
|---|---------|---------|--------|--------------|
| 1 | 代码检出 | `actions/checkout` | 取分支、提交号、工作区状态 | — |
| 2 | 环境与依赖自检 | `env-check` | 解释器版本 vs `requires-python`、依赖、配置、密钥文件是否被忽略 | 环境就不对，后面的红都是假的 |
| 3 | 规范完整性 | `spec` | 三份规范齐全、"不做"章节在、代码改动同步了 specs | 规范失守，AI 会自由发挥 |
| 4 | 静态检查 | `lint` | 行长/命名/docstring/裸 except/可变默认参数 | 代码健康度问题（当前报告级） |
| 5 | 单元与集成测试 | `test`（3.11 + 3.13 矩阵） | 248 个用例 | 功能回归，禁止发布 |
| 6 | 依赖漏洞扫描 | `security` | 读 `uv.lock` 真实版本，对照漏洞库 | 供应链风险 |
| 7 | 密钥与配置卫生 | `security` | 硬编码密钥、敏感文件入库 | 一次泄漏 = 全量轮换 |
| 8 | 构建制品 | `build` | 打包 zip + manifest + sha256，并验证可复现 | 产物不可信 |
| 9 | 部署预发 | `staging` | 解压同一制品、写发布记录 | 部署失败 |
| 10 | 冒烟测试 | `staging` | 用固定数据集调用**已部署代码**生成日报，过质量门禁 | 预发跑不通，生产不动 |
| 11 | 生产审批门禁 | `environment: production` | 人工批准（GitHub 里就是 required reviewers） | 没人批就不发 |
| 12 | 部署生产 | `production` | 部署**预发验证过的同一个 sha256** | 生产部署失败 → 回滚 |
| 13 | 发布后验证 | `production` | 生产环境再生成一次日报并校验 | 立即回滚 + 复盘 |

对应的云端定义见 `.github/workflows/ci.yml`（1-8 中的 CI 部分）、`cd.yml`（9-13）、
`daily-report.yml`（每天 08:00 的定时任务）、`rollback.yml`（回滚）。

---

## 3. 六个演示场景（我在这台机器上实测过）

| # | 命令 | 预期结果 | 实测 |
|---|------|---------|------|
| 1 | `run_pipeline.ps1 -AutoApprove` | 环境门禁拦下 | ❌ 环境与依赖自检：解释器 3.10.20 < requires-python 3.11 |
| 2 | `run_pipeline.ps1 -AutoApprove -AllowVersionMismatch` | 测试阶段拦下 | ❌ 8 failed / 240 passed（全部是 `test_github.py`） |
| 3 | `run_pipeline.ps1 -AutoApprove -AllowVersionMismatch -From build` | 构建→预发→生产全绿 | ✅ 制品 `daily-report-0.1.0-*.zip`，日报 523 字符，11 项质量检查通过 |
| 4 | `run_pipeline.ps1 -Only test -Fault test` | 真实回归被拦截 | ❌ 18 failed / 230 passed（注入的文案回归 + 环境差异） |
| 5 | `run_pipeline.ps1 -Only secret -Fault secret` | 密钥泄漏被拦截 | ❌ `shared/logger.py:1 疑似硬编码口令赋值` |
| 6 | `run_pipeline.ps1 -Only dep -Fault dep` | 依赖漏洞被拦截 | ❌ pyyaml 5.3.1 命中 CVE-2020-14343 |

所有故障注入在运行结束时都会自动还原（脚本用 `*.cicd-backup` 备份，`finally` 里恢复），
跑完 `git diff` 应当为空。

### 为什么场景 1、2 是红的？

这台机器上能执行的 Python 只有 **3.10.20**，而项目声明 `requires-python >= 3.11`：
代码里 `datetime.fromisoformat("2026-09-30T08:30:00Z")` 这种写法在 3.10 会抛 `ValueError`，
于是 `test_github.py` 的 8 个用例失败。**这不是业务 bug，是环境漂移** —— 恰好证明了
第 2 阶段"环境自检"存在的意义。

在你自己的机器上（`.venv` 由 Python 3.13 创建），场景 1、2 会是全绿：
`uv sync` 之后直接 `run_pipeline.ps1 -AutoApprove` 即可。

本机演练时可以用这两个参数（真实 CI 绝不使用）：

```powershell
-AllowVersionMismatch    # 版本不匹配降级为警告
-PythonPath <依赖目录>    # 给备用解释器补依赖（例如本会话的 work\pyboot）
```

---

## 4. 目录结构

```
cicd-lab/                         ← 项目根（git 仓库）
├── .github/workflows/
│   ├── spec-check.yml            # 原有：SDD 规范检查
│   ├── ci.yml                    # 新增：规范/环境/lint/测试矩阵/安全/构建
│   ├── cd.yml                    # 新增：预发 → 冒烟 → 审批 → 生产 → 验证
│   ├── daily-report.yml          # 新增：每天 08:00 生成并推送日报
│   └── rollback.yml              # 新增：一键回滚（也走流水线）
├── pipeline/
│   ├── run_pipeline.ps1          # 本地流水线执行器（13 阶段）
│   ├── fixtures/sample-day.json  # 冒烟用的固定数据集
│   └── tools/
│       ├── env_check.py          # 环境/依赖/配置自检
│       ├── spec_check.py         # 规范完整性（本地复刻 spec-check.yml）
│       ├── lint_check.py         # 轻量静态检查（零依赖）
│       ├── dep_scan.py           # 依赖漏洞扫描（读 uv.lock）
│       ├── secret_scan.py        # 密钥与配置卫生扫描
│       ├── build_artifact.py     # 可复现构建（固定 zip 时间戳）
│       ├── deploy.py             # 部署到环境目录 + 发布记录
│       ├── rollback.py           # 按发布历史回滚
│       ├── report_smoke.py       # 冒烟：用固定数据调用已部署的生成层
│       └── report_check.py       # 日报质量门禁（11 项断言）
├── Dockerfile                    # 运行镜像（定时任务型应用）
├── dist/                         # 构建产物（.gitignore）
├── environments/                 # 本机模拟环境（.gitignore）
└── reports/                      # 运行报告与日志（.gitignore）
```

---

## 5. 移植回 `D:\projectsdd`

把 4 类东西拷过去即可（其余都是原有文件）：

```powershell
$src = "C:\Users\liuhao\Documents\Codex\2026-10-17\ji\outputs\cicd-lab"
$dst = "D:\projectsdd"

Copy-Item "$src\.github\workflows\ci.yml"          "$dst\.github\workflows\" -Force
Copy-Item "$src\.github\workflows\cd.yml"          "$dst\.github\workflows\" -Force
Copy-Item "$src\.github\workflows\daily-report.yml" "$dst\.github\workflows\" -Force
Copy-Item "$src\.github\workflows\rollback.yml"    "$dst\.github\workflows\" -Force
Copy-Item "$src\pipeline"                          "$dst\" -Recurse -Force
Copy-Item "$src\Dockerfile"                        "$dst\" -Force
Copy-Item "$src\.dockerignore"                     "$dst\" -Force

# 然后
cd D:\projectsdd
git checkout -b ci/add-pipeline
git add .github pipeline Dockerfile .dockerignore
git commit -m "ci: 增加 CI/CD 流水线与本地执行器"
git push -u origin ci/add-pipeline
```

在 GitHub 上开 PR，就能看到 CI 真正跑起来；合并到 main 后 CD 自动进入预发。

### GitHub 仓库要配置的东西

**Settings → Secrets and variables → Actions**：

| Secret | 用途 |
|---|---|
| `CONFIG_YAML` | 整个 `config.yaml` 的内容（含内部 ID，按敏感信息处理） |
| `GH_PAT` | 采集 GitHub 提交用的 PAT（比默认 token 权限更可控） |
| `LARK_APP_ID` / `LARK_APP_SECRET` | 飞书自建应用凭据 |
| `SMTP_USER` / `SMTP_PASSWORD` / `SMTP_FROM` | 邮件推送 |
| `LARK_BOT_WEBHOOK_URL` | 飞书机器人 webhook |

**Settings → Environments**：

- `staging`：不需要审批（或只允许指定分支）
- `production`：勾选 **Required reviewers**（这就是"生产审批门禁"的云端实现）

**Settings → Branches**：给 `main` 开分支保护，要求 CI 通过才能合并。

---

## 6. 从"目录部署"到"真实部署"

本实验里的 `deploy.py` 把制品解压到 `environments/<env>/releases/`，只是**模拟**环境。
真实部署时替换它的"落地"部分即可，前面的流程一点不用变：

| 目标形态 | 替换方式 |
|---|---|
| 定时任务主机 | `scp` 制品到主机 + 解压 + 注册 cron/计划任务 |
| 容器 | `docker build` → 推送镜像仓库（tag 用 git sha）→ `kubectl set image` / `docker compose up -d` |
| Serverless | 上传 zip 到函数计算 / 云函数，切别名与流量 |
| K8s + GitOps | 把新镜像 tag 写进部署仓库，由 Argo CD / Flux 同步 |

要点始终一样：**部署的是制品、部署前校验 sha256、发布后验证、随时能回滚。**

---

## 7. 定时任务（这个系统的真实生产形态）

`daily-report.yml` 用 `schedule` 每天触发：

```yaml
on:
  schedule:
    - cron: "0 0 * * *"   # UTC 00:00 = 北京时间 08:00
```

注意：**GitHub Actions 的 cron 用 UTC**，写 `0 8 * * *` 会变成北京时间 16:00。
Windows 本机则用任务计划：

```powershell
schtasks /Create /TN "daily-report" /SC DAILY /ST 08:00 `
  /TR "cd /d D:\projectsdd && .venv\Scripts\python.exe main.py"
```

---

## 8. 练习题

1. **让场景 1 变绿**：在能执行 3.11+ 的机器上跑 `uv sync` 与整条流水线，观察 13 个阶段全绿。
2. **制造一次回归**：把 `NO_RECORDS_TEXT` 改成"今日无数据"，只跑 `-Only test`，看 CI 怎么拦。
3. **收紧 lint**：把流水线里的 `--report-only` 去掉，先修完那 10 条（见第 9 节），再观察 lint 变红/变绿。
4. **加一条测试**：给 `generator/formatter.py` 的"未接入不渲染"补一个用例，看测试数量变化。
5. **加一个门禁**：要求日报里"工时统计"必须有数据，否则失败（改 `report_check.py` 加一条 K008）。
6. **演练回滚**：连续发布两次（改一处文案即可换提交号），然后 `python pipeline/tools/rollback.py --env prod`。
7. **加一个 stage**：把 `docker build` 加进流水线（需要本机装 Docker），只在 main 上跑。
8. **做一次成本/时长优化**：把 `test` 阶段改成 `-n auto`（pytest-xdist）或按目录拆成两个 job，比较耗时。

---

## 9. 当前待改进项（真实测量，不是估计）

`lint_check.py --project .` 在当前代码上报告 **10 条**：

- `L001` 行长度超过 100：`collector/github.py` ×2、`generator/formatter.py` ×2、`generator/template.py` ×1（模板里的 HTML 长行，建议直接豁免该文件）
- `L002` 缺 docstring：`collector/_lark.py::request_json`、四个 `scripts/*.py::main`

建议的收紧路线（三步）：

1. 给入口函数 `main()` 加一行 docstring（4 条），给 `request_json()` 补 docstring（1 条）；
2. 把 4 个超长代码行换行；模板文件用 ruff 的 `per-file-ignores` 排除；
3. 清零后把 CI 与本地流水线的 `--report-only` 去掉，lint 变成硬门禁。

---

## 10. 常见问题

**Q：`pytest` 报 `PermissionError: ...\Temp\pytest-of-...`？**
A：把 pytest 临时目录指到工作区：`--basetemp=<可写目录>`。流水线已经默认这么做。

**Q：`.venv\Scripts\python.exe` 报 "Unable to create process"？**
A：这是环境问题（解释器被移动/权限受限/venv 损坏）。重新 `uv sync`，或用 `-Python` 指定可用解释器。

**Q：PowerShell 里中文乱码？**
A：`chcp 65001` + `[Console]::OutputEncoding = [System.Text.Encoding]::UTF8`，并给 `.ps1` 文件加 UTF-8 BOM（PowerShell 5.1 才会按 UTF-8 解析）。

**Q：故障注入没还原怎么办？**
A：备份文件是同目录的 `*.cicd-backup`，手动拷回即可；正常情况下脚本会自动还原。