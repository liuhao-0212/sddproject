# 一键运行智能日报（v1.15）：等价于 uv run python main.py
$ErrorActionPreference = "Stop"
Set-Location $PSScriptRoot
uv run python main.py @args
