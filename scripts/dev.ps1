#Requires -Version 5.1
<#
.SYNOPSIS
    开发模式一键启动：创建/复用 .venv，安装依赖，然后用 mock 数据启动 BiliCleaner。

.DESCRIPTION
    - 第一次运行会在项目根目录创建 .venv
    - 安装当前仓库（可编辑模式）与开发依赖：pip install -e ".[dev]"
    - 默认以 --mock 模式启动，不连接 B 站，方便调试前端与任务引擎
    - 数据目录默认是 %APPDATA%\BiliCleaner，可用 -DataDir 覆盖

.EXAMPLE
    .\scripts\dev.ps1

.EXAMPLE
    .\scripts\dev.ps1 -Port 8771 -NoBrowser
#>
[CmdletBinding()]
param(
    [int]$Port = 8770,
    [switch]$NoBrowser,
    [switch]$SkipInstall,
    [string]$DataDir = "",
    [string]$Python = "python"
)

$ErrorActionPreference = "Stop"

$Root = Split-Path -Parent $PSScriptRoot
Set-Location $Root

$VenvDir = Join-Path $Root ".venv"
$VenvPython = Join-Path $VenvDir "Scripts\python.exe"

if (-not (Get-Command $Python -ErrorAction SilentlyContinue)) {
    throw "找不到 Python（$Python）。请先安装 Python 3.10+ 并把它加入 PATH。"
}

# 1. 虚拟环境：有就复用，没有就创建
if (-not (Test-Path $VenvPython)) {
    Write-Host "[dev] 创建虚拟环境：$VenvDir" -ForegroundColor Cyan
    & $Python -m venv $VenvDir
    if ($LASTEXITCODE -ne 0) { throw "创建虚拟环境失败（python -m venv）" }
}
else {
    Write-Host "[dev] 复用已有虚拟环境：$VenvDir" -ForegroundColor DarkGray
}

# 2. 安装依赖（可编辑安装 + dev extras）
if (-not $SkipInstall) {
    Write-Host '[dev] 安装依赖：pip install -e ".[dev]"' -ForegroundColor Cyan
    & $VenvPython -m pip install --upgrade pip --quiet
    & $VenvPython -m pip install -e ".[dev]"
    if ($LASTEXITCODE -ne 0) { throw "安装依赖失败，请检查网络或 pyproject.toml" }
}
else {
    Write-Host "[dev] 已跳过依赖安装（-SkipInstall）" -ForegroundColor DarkGray
}

# 3. 以 mock 模式启动（不连 B 站）
$AppArgs = @("-m", "bilicleaner", "--mock", "--port", "$Port")
if ($NoBrowser) { $AppArgs += "--no-browser" }
if ($DataDir) { $AppArgs += @("--data-dir", $DataDir) }

Write-Host ""
Write-Host "[dev] 启动：python $($AppArgs -join ' ')" -ForegroundColor Green
Write-Host "[dev] 按 Ctrl+C 退出" -ForegroundColor DarkGray
Write-Host ""

& $VenvPython @AppArgs
exit $LASTEXITCODE
