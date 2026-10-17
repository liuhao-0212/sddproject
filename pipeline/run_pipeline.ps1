#requires -Version 5.1
<#
.SYNOPSIS
  智能日报生成系统 —— 本地 CI/CD 流水线执行器（教学用）。

.DESCRIPTION
  把 .github/workflows/ci.yml + cd.yml 里定义的 13 个阶段在本机真跑一遍。
  阶段、顺序、依赖、失败即停（fail-fast）、质量门禁、环境晋级的表现与云端一致，
  但每一步都是本机可执行的命令，产生的都是真实产物：
  真测试结果、真制品 zip（sha256）、真部署目录、真日报、真发布历史。

  阶段：1 检出 → 2 环境自检 → 3 规范检查 → 4 静态检查 → 5 测试 → 6 依赖扫描
        → 7 密钥扫描 → 8 构建制品 → 9 部署预发 → 10 冒烟测试
        → 11 生产审批门禁 → 12 部署生产 → 13 发布后验证

.PARAMETER Fault
  故意注入一个故障，观察流水线在哪一步把它拦住（跑完自动还原）：
    none   正常提交
    spec   规范检查失败（把 proposal 的"不做"章节删掉）
    lint   把静态检查切到严格模式（行宽 80）后出现大量违规
    test   真实回归：改动生成层的固定文案，让测试挂掉
    dep    真实漏洞：把 uv.lock 里的 pyyaml 降到 5.3.1（命中 CVE-2020-14343）
    secret 真实泄漏：往源码里写一个硬编码口令

.PARAMETER From
  从指定阶段开始执行，之前的阶段标记为"跳过"。
  对应真实场景：制品已经构建好了，现在只做"晋级到生产"。

.PARAMETER Only
  只执行指定阶段（逗号分隔），对应 CI 里的 "rerun single job"。

.PARAMETER AllowVersionMismatch
  解释器版本不满足 requires-python 时降级为警告（仅本机教学演练，真实 CI 绝不要用）。

.PARAMETER AutoApprove
  自动通过生产发布审批门禁（真实 CI 里对应 environment: production 的 required reviewers）。

.PARAMETER SkipProd
  只跑到预发为止 —— 这就是"持续交付（Continuous Delivery）"。

.EXAMPLE
  powershell -ExecutionPolicy Bypass -File pipeline\run_pipeline.ps1 -AutoApprove
.EXAMPLE
  powershell -ExecutionPolicy Bypass -File pipeline\run_pipeline.ps1 -Only test -Fault test
#>
[CmdletBinding()]
param(
    [ValidateSet('none', 'spec', 'lint', 'test', 'dep', 'secret')][string]$Fault = 'none',
    [string]$From = '',
    [string]$Only = '',
    [switch]$AllowVersionMismatch,
    [switch]$AutoApprove,
    [switch]$SkipProd,
    [string]$Project = '',
    [string]$Python = '',
    [string]$PythonPath = ''
)

$ErrorActionPreference = 'Stop'
try { [Console]::OutputEncoding = [System.Text.Encoding]::UTF8 } catch { }
$OutputEncoding = New-Object System.Text.UTF8Encoding($false)
$env:PYTHONIOENCODING = 'utf-8'
$env:PYTHONDONTWRITEBYTECODE = '1'

$RunnerDir = Split-Path -Parent $MyInvocation.MyCommand.Path
if (-not $Project) { $Project = Split-Path -Parent $RunnerDir }
$Project = (Resolve-Path -LiteralPath $Project).Path
Set-Location $Project

$Tools = Join-Path $RunnerDir 'tools'
$Fixture = Join-Path $RunnerDir 'fixtures\sample-day.json'
$Reports = Join-Path $Project 'reports'
New-Item -ItemType Directory -Force -Path $Reports | Out-Null
$LogPath = Join-Path $Reports 'pipeline.log'
$script:LogStarted = $false
$script:Results = New-Object System.Collections.ArrayList
$script:Failed = $false
$script:BuildInfo = $null
$script:Deployments = New-Object System.Collections.ArrayList
$script:Backups = @{}
$script:Restored = $true

# ---------------------------------------------------------------------------
# 基础工具函数
# ---------------------------------------------------------------------------
function Write-Log {
    param([string]$Text, [string]$Color = '')
    if ($Color) { Write-Host $Text -ForegroundColor $Color } else { Write-Host $Text }
    if (-not $script:LogStarted) {
        Set-Content -Path $LogPath -Value $Text -Encoding UTF8
        $script:LogStarted = $true
    }
    else {
        Add-Content -Path $LogPath -Value $Text -Encoding UTF8
    }
}

function Invoke-Native {
    <# 执行外部命令：stdout/stderr 都进日志，返回 @{ Code; Output }。 #>
    param(
        [Parameter(Mandatory = $true)][string]$Exe,
        [Parameter(Mandatory = $true)][string[]]$ArgList,
        [hashtable]$Env = @{}
    )
    $saved = @{}
    foreach ($key in $Env.Keys) {
        $saved[$key] = [Environment]::GetEnvironmentVariable($key)
        [Environment]::SetEnvironmentVariable($key, [string]$Env[$key])
    }
    $old = $ErrorActionPreference
    $ErrorActionPreference = 'Continue'
    $errFile = [System.IO.Path]::GetTempFileName()
    try {
        $stdout = & $Exe @ArgList 2>$errFile | ForEach-Object { "$_" }
        $code = $LASTEXITCODE
        if ($null -eq $code) { $code = 0 }
    }
    finally {
        $ErrorActionPreference = $old
        foreach ($key in $saved.Keys) { [Environment]::SetEnvironmentVariable($key, $saved[$key]) }
    }
    $stderr = @()
    if (Test-Path -LiteralPath $errFile) {
        $stderr = Get-Content -LiteralPath $errFile -Encoding UTF8 -ErrorAction SilentlyContinue
        Remove-Item -LiteralPath $errFile -Force -ErrorAction SilentlyContinue
    }
    $output = @($stdout) + @($stderr)
    foreach ($line in $output) { Write-Log ("      " + $line) }
    return @{ Code = [int]$code; Output = @($output) }
}

function Add-Result {
    param([string]$Id, [string]$Title, [string]$Status, [double]$Seconds, [string]$Detail)
    [void]$script:Results.Add([pscustomobject]@{
            Id = $Id; Title = $Title; Status = $Status
            Seconds = [math]::Round($Seconds, 2); Detail = $Detail
        })
}

function Add-Skipped {
    param([string]$Id, [string]$Title, [string]$Reason)
    Add-Result -Id $Id -Title $Title -Status '跳过' -Seconds 0 -Detail $Reason
}

function Get-PythonCommandVersion {
    param([string]$Exe)
    $old = $ErrorActionPreference
    $ErrorActionPreference = 'Continue'
    try {
        # 不依赖 $LASTEXITCODE（函数内的自动变量作用域不可靠），只看输出是否为版本号
        $text = & $Exe -c "import sys;print('%d.%d.%d' % sys.version_info[:3])" 2>$null
        $first = @($text) | Select-Object -First 1
        if ("$first" -match '^\d+\.\d+\.\d+$') { return "$first" }
    }
    catch { }
    finally { $ErrorActionPreference = $old }
    return $null
}

function Get-ReleaseDir {
    param([string]$Env)
    $marker = Join-Path $Project "environments\$Env\current.txt"
    if (-not (Test-Path $marker)) { return $null }
    $release = (Get-Content $marker -Raw).Trim()
    if (-not $release) { return $null }
    return (Join-Path $Project "environments\$Env\releases\$release")
}

function New-Stage {
    param([string]$Id, [string]$Title, [scriptblock]$Run)
    return [pscustomobject]@{ Id = $Id; Title = $Title; Run = $Run }
}

# ---------------------------------------------------------------------------
# 故障注入（演示用：只在工作副本内改动，结束时还原）
# ---------------------------------------------------------------------------
function Backup-File {
    param([string]$RelPath)
    $full = Join-Path $Project $RelPath
    if (-not (Test-Path $full)) { return }
    $backup = "$full.cicd-backup"
    Copy-Item -LiteralPath $full -Destination $backup -Force
    $script:Backups[$RelPath] = $backup
}

function Restore-Files {
    foreach ($rel in $script:Backups.Keys) {
        $full = Join-Path $Project $rel
        Copy-Item -LiteralPath $script:Backups[$rel] -Destination $full -Force
        Remove-Item -LiteralPath $script:Backups[$rel] -Force -ErrorAction SilentlyContinue
    }
    if ($script:Backups.Count -gt 0) {
        $script:Backups = @{}
        Write-Log '[fault] 故障注入的改动已全部还原' 'DarkGray'
    }
}

function Apply-Fault {
    $tree = Join-Path $Project 'specs\proposal.md'
    switch ($Fault) {
        'none' { return }
        'spec' {
            Backup-File 'specs/proposal.md'
            $text = Get-Content $tree -Raw -Encoding UTF8
            $text = $text -replace '(?m)^(#+.*不做.*)$', '## 范围边界（待补充）'
            Set-Content -Path $tree -Value $text -Encoding UTF8 -NoNewline
            Write-Log '[fault] 已把 proposal.md 的"不做"章节删掉（模拟范围边界失守）' 'Yellow'
        }
        'test' {
            Backup-File 'generator/formatter.py'
            $path = Join-Path $Project 'generator\formatter.py'
            $text = Get-Content $path -Raw -Encoding UTF8
            $text = $text -replace 'NO_RECORDS_TEXT = "今日无记录"', 'NO_RECORDS_TEXT = "今日暂无记录"'
            Set-Content -Path $path -Value $text -Encoding UTF8 -NoNewline
            Write-Log '[fault] 已改动生成层的固定文案（模拟一次真实代码回归）' 'Yellow'
        }
        'dep' {
            Backup-File 'uv.lock'
            $path = Join-Path $Project 'uv.lock'
            $text = Get-Content $path -Raw -Encoding UTF8
            $text = $text -replace '(?s)(name = "pyyaml"\r?\nversion = ")6\.0\.3(")', '${1}5.3.1${2}'
            Set-Content -Path $path -Value $text -Encoding UTF8 -NoNewline
            Write-Log '[fault] 已把锁文件里的 pyyaml 降到 5.3.1（命中 CVE-2020-14343）' 'Yellow'
        }
        'secret' {
            Backup-File 'shared/logger.py'
            $path = Join-Path $Project 'shared\logger.py'
            $text = Get-Content $path -Raw -Encoding UTF8
            $text = "SMTP_PASSWORD = `"Qq123456!abc`"  # 教学用：故意硬编码一个口令`r`n" + $text
            Set-Content -Path $path -Value $text -Encoding UTF8 -NoNewline
            Write-Log '[fault] 已往源码里写入一个硬编码口令（模拟密钥泄漏）' 'Yellow'
        }
        'lint' {
            Write-Log '[fault] 静态检查切换到严格模式（行宽 80 且阻断）' 'Yellow'
        }
    }
}

# ---------------------------------------------------------------------------
# 解释器选择：优先用项目自己的 .venv（这才是"在项目环境里跑"）
# ---------------------------------------------------------------------------
$VenvPython = Join-Path $Project '.venv\Scripts\python.exe'
$PythonSource = '命令行参数'
$PythonPathExtra = @()
if (-not $Python) {
    if (Test-Path $VenvPython) {
        $probe = Get-PythonCommandVersion $VenvPython
        if ($probe) {
            $Python = $VenvPython
            $PythonSource = '项目 .venv（uv sync 创建）'
        }
        else {
            Write-Log "[pipeline] 警告：$VenvPython 无法在本机执行，退回备用解释器" 'Yellow'
        }
    }
    if (-not $Python) {
        $Python = 'D:\Anaconda3\envs\multimodal\python.exe'
        $PythonSource = '备用解释器（本机默认，非项目环境）'
    }
}
# 注意：单元素数组在管道里会被解包成字符串，用 += 显式拼数组，避免路径被直接相连
$PathParts = @()
if ($PythonPath) { $PathParts += @($PythonPath.Split(';') | Where-Object { $_ }) }
$PathParts += $Project
$env:PYTHONPATH = ($PathParts -join ';')
$PythonVersion = Get-PythonCommandVersion $Python
if (-not $PythonVersion) {
    Write-Log "[pipeline] 找不到可执行解释器：$Python" 'Red'
    exit 2
}

# ---------------------------------------------------------------------------
# 阶段计划
# ---------------------------------------------------------------------------
$Plan = @(
    (New-Stage 'checkout' '代码检出' {
            $sha = (Invoke-Native 'git' @('rev-parse', '--short', 'HEAD')).Output
            $branch = (Invoke-Native 'git' @('rev-parse', '--abbrev-ref', 'HEAD')).Output
            $dirty = (Invoke-Native 'git' @('status', '--porcelain')).Output
            $dirtyText = if (@($dirty).Count -gt 0) { "工作区有 $((@($dirty)).Count) 处未提交改动" } else { '工作区干净' }
            return @{ Code = 0; Detail = "分支 $($branch[0]) @ $($sha[0])，$dirtyText" }
        })
    (New-Stage 'env' '环境与依赖自检' {
            $arguments = @((Join-Path $Tools 'env_check.py'), '--project', '.', '--json-out', (Join-Path $Reports 'env-check.json'))
            if ($AllowVersionMismatch) { $arguments += '--allow-version-mismatch' }
            $r = Invoke-Native $Python $arguments
            return @{ Code = $r.Code; Detail = if ($r.Code -eq 0) { "解释器 $PythonVersion（$PythonSource）满足项目要求" } else { '环境不符合项目声明：先修环境，不要急着改代码' } }
        })
    (New-Stage 'spec' '规范完整性检查' {
            $r = Invoke-Native $Python @((Join-Path $Tools 'spec_check.py'), '--project', '.', '--base-ref', 'HEAD~1')
            return @{ Code = $r.Code; Detail = if ($r.Code -eq 0) { '规范三件套齐全、范围边界清晰' } else { '规范失守：AI/人会开始自由发挥' } }
        })
    (New-Stage 'lint' '静态检查' {
            $arguments = @((Join-Path $Tools 'lint_check.py'), '--project', '.', '--json-out', (Join-Path $Reports 'lint-report.json'))
            if ($Fault -eq 'lint') { $arguments += @('--max-line', '80') } else { $arguments += '--report-only' }
            $r = Invoke-Native $Python $arguments
            $detail = if ($Fault -eq 'lint') { '严格模式（行宽 80）下有违规' } else { '报告级检查（当前不阻断，见 lint-report.json）' }
            return @{ Code = $r.Code; Detail = $detail }
        })
    (New-Stage 'test' '单元与集成测试' {
            $baseTemp = Join-Path $Reports 'pytest-tmp'
            $arguments = @('-m', 'pytest', 'tests', '-q', '--no-header', '-p', 'no:cacheprovider', "--basetemp=$baseTemp", '-rf')
            $r = Invoke-Native $Python $arguments
            $tail = (@($r.Output) | Where-Object { $_ -match 'passed|failed|error' } | Select-Object -Last 1)
            return @{ Code = $r.Code; Detail = if ($r.Code -eq 0) { "全部用例通过：$tail" } else { "测试未通过：$tail" } }
        })
    (New-Stage 'dep' '依赖漏洞扫描' {
            $r = Invoke-Native $Python @((Join-Path $Tools 'dep_scan.py'), '--project', '.', '--json-out', (Join-Path $Reports 'dep-report.json'))
            return @{ Code = $r.Code; Detail = if ($r.Code -eq 0) { '锁定的依赖版本无高危漏洞' } else { '命中 HIGH 级漏洞：禁止打包发布' } }
        })
    (New-Stage 'secret' '密钥与配置卫生扫描' {
            $r = Invoke-Native $Python @((Join-Path $Tools 'secret_scan.py'), '--project', '.', '--json-out', (Join-Path $Reports 'secret-report.json'))
            return @{ Code = $r.Code; Detail = if ($r.Code -eq 0) { '无硬编码密钥、无敏感文件入库' } else { '疑似密钥泄漏：立即改为环境变量注入并轮换密钥' } }
        })
    (New-Stage 'build' '构建制品' {
            $r = Invoke-Native $Python @((Join-Path $Tools 'build_artifact.py'), '--project', '.', '--out', 'dist')
            $infoPath = Join-Path $Project 'dist\build-info.json'
            if ($r.Code -eq 0 -and (Test-Path $infoPath)) {
                $script:BuildInfo = Get-Content $infoPath -Raw -Encoding UTF8 | ConvertFrom-Json
                $shaShort = $script:BuildInfo.artifact_sha256.Substring(0, 12)
                return @{ Code = 0; Detail = "版本 $($script:BuildInfo.version)：$($script:BuildInfo.artifact)（sha256 $shaShort...）" }
            }
            return @{ Code = 1; Detail = '打包失败' }
        })
    (New-Stage 'deploy-staging' '部署预发环境' {
            $r = Invoke-Native $Python @((Join-Path $Tools 'deploy.py'), '--project', '.', '--env', 'staging',
                '--artifact', $script:BuildInfo.artifact, '--expect-sha', $script:BuildInfo.artifact_sha256,
                '--json-out', (Join-Path $Reports 'deploy-staging.json'))
            if ($r.Code -eq 0) {
                $record = Get-Content (Join-Path $Reports 'deploy-staging.json') -Raw -Encoding UTF8 | ConvertFrom-Json
                [void]$script:Deployments.Add($record)
                return @{ Code = 0; Detail = "发布号 $($record.release)：部署的是制品，不是源码目录" }
            }
            return @{ Code = $r.Code; Detail = '部署失败或制品校验不通过' }
        })
    (New-Stage 'smoke' '冒烟测试（预发）' {
            $release = Get-ReleaseDir 'staging'
            $out = Join-Path $Reports 'smoke-staging'
            $r = Invoke-Native $Python @((Join-Path $Tools 'report_smoke.py'), '--app-dir', $release,
                '--fixture', $Fixture, '--out', $out, '--json-out', (Join-Path $Reports 'smoke-staging.json'))
            if ($r.Code -ne 0) { return @{ Code = $r.Code; Detail = '预发环境跑不通：发布已拦下，生产完好' } }
            $md = Join-Path $out 'daily-report-2026-10-16.md'   # 与 fixtures/sample-day.json 的日期一致
            $html = Join-Path $out 'daily-report-2026-10-16.html'
            $c = Invoke-Native $Python @((Join-Path $Tools 'report_check.py'), '--markdown', $md, '--html', $html,
                '--expect-team', '研发一组', '--expect-date', '2026-10-16', '--json-out', (Join-Path $Reports 'report-check-staging.json'))
            if ($c.Code -ne 0) { return @{ Code = $c.Code; Detail = '日报质量门禁未通过' } }
            return @{ Code = 0; Detail = '预发环境生成日报成功，四段式与降级标注全部达标' }
        })
    (New-Stage 'gate' '生产审批门禁' {
            if ($AutoApprove) {
                return @{ Code = 0; Detail = '自动批准（对应 environment: production 的 required reviewers 已放行）' }
            }
            Write-Log ("      待发布制品：{0}" -f $script:BuildInfo.artifact) 'Gray'
            $answer = Read-Host '      是否批准发布到生产？输入 yes 批准'
            if ($answer -match '^(y|yes)$') { return @{ Code = 0; Detail = '人工批准（yes）' } }
            return @{ Code = 0; Detail = '人工拒绝：生产保持不动（这就是门禁的作用）'; Skip = $true }
        })
    (New-Stage 'deploy-prod' '部署生产环境' {
            $r = Invoke-Native $Python @((Join-Path $Tools 'deploy.py'), '--project', '.', '--env', 'prod',
                '--artifact', $script:BuildInfo.artifact, '--expect-sha', $script:BuildInfo.artifact_sha256,
                '--json-out', (Join-Path $Reports 'deploy-prod.json'))
            if ($r.Code -eq 0) {
                $record = Get-Content (Join-Path $Reports 'deploy-prod.json') -Raw -Encoding UTF8 | ConvertFrom-Json
                [void]$script:Deployments.Add($record)
                return @{ Code = 0; Detail = "发布号 $($record.release)：与预发同一个 sha256（构建一次，部署多次）" }
            }
            return @{ Code = $r.Code; Detail = '生产部署失败：立即执行 pipeline\tools\rollback.py 回滚' }
        })
    (New-Stage 'verify' '发布后验证' {
            $release = Get-ReleaseDir 'prod'
            $out = Join-Path $Reports 'verify-prod'
            $r = Invoke-Native $Python @((Join-Path $Tools 'report_smoke.py'), '--app-dir', $release,
                '--fixture', $Fixture, '--out', $out, '--json-out', (Join-Path $Reports 'verify-prod.json'))
            if ($r.Code -ne 0) { return @{ Code = $r.Code; Detail = '发布后验证失败：应立即回滚并复盘' } }
            $md = Join-Path $out 'daily-report-2026-10-16.md'
            $html = Join-Path $out 'daily-report-2026-10-16.html'
            $c = Invoke-Native $Python @((Join-Path $Tools 'report_check.py'), '--markdown', $md, '--html', $html,
                '--expect-team', '研发一组', '--expect-date', '2026-10-16', '--json-out', (Join-Path $Reports 'report-check-prod.json'))
            if ($c.Code -ne 0) { return @{ Code = $c.Code; Detail = '生产日报质量不达标' } }
            return @{ Code = 0; Detail = '生产环境可正常运行，交付物已归档到 reports\verify-prod' }
        })
)

# 如果本次不跑 build，但 dist 里已有构建信息，就复用它（制品晋级场景）
$ExistingInfo = Join-Path $Project 'dist\build-info.json'
if (Test-Path $ExistingInfo) {
    $script:BuildInfo = Get-Content $ExistingInfo -Raw -Encoding UTF8 | ConvertFrom-Json
}

$StageIds = $Plan | ForEach-Object { $_.Id }
$OnlyList = @()
if ($Only) { $OnlyList = $Only.Split(',') | ForEach-Object { $_.Trim() } | Where-Object { $_ } }
$FromIndex = 0
if ($From) {
    $idx = [array]::IndexOf($StageIds, $From)
    if ($idx -lt 0) { Write-Log "[pipeline] -From 参数不是有效阶段：$From" 'Red'; exit 2 }
    $FromIndex = $idx
}

$FaultLabel = @{ none = 'none（正常提交）'; spec = 'spec（范围边界失守）'; lint = 'lint（规范收紧）'
    test = 'test（真实代码回归）'; dep = 'dep（引入已知漏洞依赖）'; secret = 'secret（硬编码密钥）'
}[$Fault]

$RunStart = Get-Date
Write-Log ''
Write-Log '================= 智能日报生成系统 · CI/CD 流水线 =================' 'Cyan'
Write-Log "[pipeline] 项目目录：$Project"
Write-Log "[pipeline] 解释器  ：$Python（$PythonVersion，$PythonSource）"
Write-Log "[pipeline] 故障注入：$FaultLabel"
if ($Only) { Write-Log "[pipeline] 本次只跑  ：$Only（对应 CI 的 rerun single job）" }
elseif ($From) { Write-Log "[pipeline] 从阶段开始：$From（之前的阶段视为已完成）" }
Write-Log "[pipeline] 生产部署：$(if ($SkipProd) { '只到预发（持续交付模式）' } else { '包含（持续部署模式）' })"
Write-Log ''

Apply-Fault

$Total = $Plan.Count
for ($i = 0; $i -lt $Total; $i++) {
    $stage = $Plan[$i]
    $index = $i + 1
    $skipReason = ''
    if ($script:Failed) { $skipReason = '上游阶段失败，按 fail-fast 跳过' }
    elseif ($OnlyList.Count -gt 0 -and ($OnlyList -notcontains $stage.Id)) { $skipReason = '-Only：本次未选中该阶段' }
    elseif ($i -lt $FromIndex) { $skipReason = "-From $From：视为已完成" }
    elseif ($SkipProd -and $stage.Id -in @('gate', 'deploy-prod', 'verify')) { $skipReason = '-SkipProd：持续交付模式停在预发' }

    if ($skipReason) {
        Write-Log ("--- [{0}/{1}] {2}（跳过）" -f $index, $Total, $stage.Title) 'DarkGray'
        Add-Skipped -Id $stage.Id -Title $stage.Title -Reason $skipReason
        continue
    }

    Write-Log ("--- [{0}/{1}] {2} " -f $index, $Total, $stage.Title).PadRight(70, '-') 'White'
    $sw = [System.Diagnostics.Stopwatch]::StartNew()
    $outcome = $null
    try {
        $outcome = & $stage.Run
    }
    catch {
        $outcome = @{ Code = 1; Detail = "阶段内部错误：$($_.Exception.Message)" }
    }
    $sw.Stop()

    $hasBuild = $stage.Id -in @('deploy-staging', 'deploy-prod', 'smoke', 'verify', 'gate')
    if ($hasBuild -and -not $script:BuildInfo -and $outcome.Code -eq 0) {
        $outcome = @{ Code = 1; Detail = '缺少制品信息：请先跑 build 阶段（或去掉 -From）' }
    }
    if ($stage.Id -eq 'gate' -and $outcome.Skip) {
        Add-Result -Id $stage.Id -Title $stage.Title -Status '跳过' -Seconds $sw.Elapsed.TotalSeconds -Detail $outcome.Detail
    }
    elseif ($outcome.Code -eq 0) {
        Add-Result -Id $stage.Id -Title $stage.Title -Status '成功' -Seconds $sw.Elapsed.TotalSeconds -Detail $outcome.Detail
    }
    else {
        Add-Result -Id $stage.Id -Title $stage.Title -Status '失败' -Seconds $sw.Elapsed.TotalSeconds -Detail $outcome.Detail
        $script:Failed = $true
        Write-Log "      ✖ $($outcome.Detail)" 'Red'
    }
}

Restore-Files

# ---------------------------------------------------------------------------
# 汇总与报告
# ---------------------------------------------------------------------------
$RunEnd = Get-Date
$TotalSeconds = [math]::Round(($RunEnd - $RunStart).TotalSeconds, 2)
$FailedItem = @($script:Results | Where-Object { $_.Status -eq '失败' }) | Select-Object -First 1
$ProdDeployed = @($script:Results | Where-Object { $_.Id -eq 'deploy-prod' -and $_.Status -eq '成功' }).Count -gt 0
$SkippedCount = @($script:Results | Where-Object { $_.Status -eq '跳过' }).Count

if ($FailedItem) {
    $Verdict = "流水线红了：在「$($FailedItem.Title)」阶段被拦下"
    $VerdictIcon = '❌'; $VerdictColor = 'Red'
}
elseif ($ProdDeployed) {
    $Verdict = '全流程通过：制品已发布到生产并通过发布后验证'
    $VerdictIcon = '✅'; $VerdictColor = 'Green'
}
elseif ($SkippedCount -gt 0) {
    $Verdict = '部分阶段未执行（见阶段表），已完成阶段全部通过'
    $VerdictIcon = '🟡'; $VerdictColor = 'Yellow'
}
else {
    $Verdict = '全流程通过'
    $VerdictIcon = '✅'; $VerdictColor = 'Green'
}

Write-Log ''
Write-Log '------------------------- 阶段汇总 -------------------------' 'Cyan'
foreach ($item in $script:Results) {
    $icon = switch ($item.Status) { '成功' { '✔' } '失败' { '✖' } default { '·' } }
    $color = switch ($item.Status) { '成功' { 'Green' } '失败' { 'Red' } default { 'DarkGray' } }
    Write-Log ("  {0} {1,-18} {2,-4} {3,6:N2}s  {4}" -f $icon, $item.Title, $item.Status, $item.Seconds, $item.Detail) $color
}
Write-Log ''
Write-Log "  $VerdictIcon $Verdict（总耗时 $TotalSeconds 秒）" $VerdictColor

$md = New-Object System.Collections.Generic.List[string]
$md.Add('# CI/CD 流水线运行报告')
$md.Add('')
$md.Add('> 由 `pipeline/run_pipeline.ps1` 自动生成；阶段与 `.github/workflows/ci.yml`、`cd.yml` 一一对应。')
$md.Add('')
$md.Add('| 项目 | 值 |')
$md.Add('| --- | --- |')
$md.Add("| 项目 | $Project |")
$md.Add("| 故障注入 | $FaultLabel |")
$md.Add("| 阶段范围 | $(if ($Only) { "仅 $Only" } elseif ($From) { "$From 起" } else { "全部 $Total 个阶段" }) |")
$md.Add("| 解释器 | $Python（$PythonVersion，$PythonSource） |")
$md.Add("| 开始时间 | $($RunStart.ToString('yyyy-MM-dd HH:mm:ss')) |")
$md.Add("| 总耗时 | $TotalSeconds 秒 |")
$md.Add("| 运行结果 | $VerdictIcon $Verdict |")
$md.Add('')
$md.Add('## 阶段明细')
$md.Add('')
$md.Add('| # | 阶段 | 状态 | 耗时(s) | 说明 |')
$md.Add('| --- | --- | --- | --- | --- |')
$n = 0
foreach ($item in $script:Results) {
    $n++
    $md.Add("| $n | $($item.Title) | $($item.Status) | $($item.Seconds) | $($item.Detail) |")
}
$md.Add('')
if ($script:BuildInfo) {
    $md.Add('## 制品（artifact）')
    $md.Add('')
    $md.Add('| 项目 | 值 |')
    $md.Add('| --- | --- |')
    $md.Add("| 制品文件 | ``$($script:BuildInfo.artifact)`` |")
    $md.Add("| 版本 | $($script:BuildInfo.version) |")
    $md.Add("| git 提交 | ``$($script:BuildInfo.git_sha)``（分支 $($script:BuildInfo.git_branch)） |")
    $md.Add("| 制品 sha256 | ``$($script:BuildInfo.artifact_sha256)`` |")
    $md.Add("| 文件数 / 体积 | $($script:BuildInfo.file_count) 个 / $([math]::Round($script:BuildInfo.size_bytes / 1KB, 1)) KB |")
    $md.Add("| 构建时间(UTC) | $($script:BuildInfo.built_at) |")
    $md.Add('')
}
if ($script:Deployments.Count -gt 0) {
    $md.Add('## 部署记录')
    $md.Add('')
    $md.Add('| 环境 | 发布号 | 制品 sha256 | 上一版本 | 时间(UTC) |')
    $md.Add('| --- | --- | --- | --- | --- |')
    foreach ($d in $script:Deployments) {
        $md.Add("| $($d.env) | $($d.release) | ``$($d.artifact_sha256.Substring(0, 16))...`` | $($d.previous_release) | $($d.at) |")
    }
    $md.Add('')
    $md.Add('> 预发与生产部署的是同一个制品、同一个 sha256 —— 这就是 build once, deploy many。')
    $md.Add('')
}
$md.Add('## 结论与观察点')
$md.Add('')
if ($FailedItem) {
    $md.Add("- 失败阶段：**$($FailedItem.Title)** —— $($FailedItem.Detail)")
    $md.Add('- 该阶段之后的所有阶段都没有执行（fail-fast）：没有构建制品、没有碰任何环境，代价被压到最小。')
    $md.Add('- 修复后再提交一次，流水线会从头重新验证；不要"绕过"失败的阶段。')
}
else {
    $md.Add('- 从代码到生产的每一步都有可核查的证据：测试输出、扫描报告、制品 sha256、部署历史、日报产物。')
    $md.Add('- 生产环境上前一个版本始终可回滚：`python pipeline/tools/rollback.py --env prod`。')
}
$md.Add('')
$md.Add('## 原始产物')
$md.Add('')
$md.Add('- `reports/pipeline.log`：逐阶段完整输出')
$md.Add('- `reports/env-check.json`、`reports/lint-report.json`、`reports/dep-report.json`、`reports/secret-report.json`：各门禁结果')
$md.Add('- `reports/smoke-staging/`、`reports/verify-prod/`：冒烟与发布后验证生成的日报（Markdown + HTML）')
$md.Add('- `environments/<env>/history.jsonl`：发布与回滚审计日志')
$ReportPath = Join-Path $Reports 'pipeline-report.md'
Set-Content -Path $ReportPath -Value ($md -join "`r`n") -Encoding UTF8

$json = [pscustomobject]@{
    fault = $Fault; faultLabel = $FaultLabel; verdict = $Verdict; failed = $script:Failed
    only = $Only; from = $From
    python = $Python; pythonVersion = $PythonVersion
    startedAt = $RunStart.ToString('o'); totalSeconds = $TotalSeconds
    stages = @($script:Results); artifact = $script:BuildInfo; deployments = @($script:Deployments)
}
Set-Content -Path (Join-Path $Reports 'pipeline-report.json') -Value ($json | ConvertTo-Json -Depth 6) -Encoding UTF8

Write-Log ''
Write-Log '[pipeline] 报告：reports\pipeline-report.md' 'Cyan'
Write-Log '[pipeline] 日志：reports\pipeline.log' 'Cyan'
Write-Log ''
if ($script:Failed) { exit 1 }
exit 0