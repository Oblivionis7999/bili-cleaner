#Requires -Version 5.1
<#
.SYNOPSIS
    用 PyInstaller 把 BiliCleaner 打成 Windows 单文件 exe（产物在 dist\）。

.DESCRIPTION
    - 默认复用/创建项目内的 .venv，并安装项目与开发依赖（含 pyinstaller）
    - CI 或已装好依赖的环境可用 -SkipVenv 直接使用当前 Python，不创建 .venv
    - 把前端静态资源 src\bilicleaner\web 一起塞进 exe
    - 单文件模式：--onefile，最终产物 dist\BiliCleaner.exe
    - 结束后打印产物路径与大小，并跑一次 exe --version 冒烟

.EXAMPLE
    .\scripts\build_exe.ps1

.EXAMPLE
    .\scripts\build_exe.ps1 -SkipInstall -Python "C:\Python312\python.exe"

.EXAMPLE
    # CI 里：依赖已由 pip install -e ".[dev]" 装到 runner 的 Python 上
    pwsh -File scripts/build_exe.ps1 -SkipVenv -SkipInstall
#>
[CmdletBinding()]
param(
    [string]$Name = "BiliCleaner",
    [switch]$SkipInstall,
    [switch]$SkipVenv,
    [string]$Python = "python"
)

$ErrorActionPreference = "Stop"

$Root = Split-Path -Parent $PSScriptRoot
Set-Location $Root

$VenvDir = Join-Path $Root ".venv"
$VenvPython = Join-Path $VenvDir "Scripts\python.exe"
$WebDir = Join-Path $Root "src\bilicleaner\web"
$Entry = Join-Path $Root "scripts\pyinstaller_entry.py"
$DistDir = Join-Path $Root "dist"
$WorkDir = Join-Path $Root "build"
$ExePath = Join-Path $DistDir "$Name.exe"

if (-not (Get-Command $Python -ErrorAction SilentlyContinue)) {
    throw "找不到 Python（$Python）。请先安装 Python 3.10+ 并把它加入 PATH。"
}
if (-not (Test-Path $WebDir)) {
    throw "找不到前端目录：$WebDir（请先确认 src\bilicleaner\web 已实现）"
}
if (-not (Test-Path $Entry)) {
    throw "找不到打包入口：$Entry"
}

# 1. 选择解释器：默认用项目内 .venv；CI / 已装依赖的环境用 -SkipVenv
if ($SkipVenv) {
    $BuildPython = $Python
    Write-Host "[build] -SkipVenv：直接使用当前 Python 环境（$Python），不创建 .venv" -ForegroundColor Cyan
    $PyVersion = & $BuildPython --version 2>&1
    Write-Host "        $PyVersion"
}
else {
    if (-not (Test-Path $VenvPython)) {
        Write-Host "[build] 创建虚拟环境：$VenvDir" -ForegroundColor Cyan
        & $Python -m venv $VenvDir
        if ($LASTEXITCODE -ne 0) { throw "创建虚拟环境失败（python -m venv）" }
    }
    $BuildPython = $VenvPython
}

if (-not $SkipInstall) {
    Write-Host '[build] 安装依赖：pip install -e ".[dev]"' -ForegroundColor Cyan
    & $BuildPython -m pip install --upgrade pip --quiet
    & $BuildPython -m pip install -e ".[dev]"
    if ($LASTEXITCODE -ne 0) { throw "安装依赖失败，请检查网络或 pyproject.toml" }
}

# 打包前确认 PyInstaller 可用（尤其 -SkipVenv 时给出清晰报错，而不是一行 ModuleNotFoundError）
$null = & $BuildPython -c "import PyInstaller" 2>&1
if ($LASTEXITCODE -ne 0) {
    throw "当前 Python 环境没有 PyInstaller。请先 pip install -e `".[dev]`"，或去掉 -SkipVenv / -SkipInstall 让脚本自己装。"
}

# 2. 清理旧的构建产物（保留 dist 里其它文件，只覆盖同名 exe）
Write-Host "[build] 清理旧的 build\ 与 $Name.exe ..." -ForegroundColor DarkGray

function Remove-PathWithRetry {
    param([string]$Path, [switch]$Recurse)
    if (-not (Test-Path $Path)) { return }
    for ($attempt = 1; $attempt -le 5; $attempt++) {
        try {
            if ($Recurse) { Remove-Item -Recurse -Force $Path -ErrorAction Stop }
            else { Remove-Item -Force $Path -ErrorAction Stop }
            return
        }
        catch {
            if ($attempt -eq 5) {
                throw "无法删除 $Path（可能 $Name.exe 还在运行，请先关闭它再重试）：$($_.Exception.Message)"
            }
            Start-Sleep -Milliseconds 500
        }
    }
}

Remove-PathWithRetry -Path $WorkDir -Recurse
Remove-PathWithRetry -Path $ExePath
New-Item -ItemType Directory -Force -Path $WorkDir | Out-Null
New-Item -ItemType Directory -Force -Path $DistDir | Out-Null

# 3. 调用 PyInstaller（单文件，把 web 静态资源一起打进去）
#    --paths src：用 src layout 时让 PyInstaller 能直接找到 bilicleaner 包，
#    不依赖 editable install 的 .pth 钩子。
$PyInstallerArgs = @(
    "-m", "PyInstaller",
    "--noconfirm",
    "--clean",
    "--onefile",
    "--name", $Name,
    "--paths", (Join-Path $Root "src"),
    "--add-data", "$WebDir;bilicleaner/web",
    "--collect-submodules", "uvicorn",
    "--distpath", $DistDir,
    "--workpath", $WorkDir,
    "--specpath", $WorkDir,
    $Entry
)

Write-Host "[build] 开始打包（PyInstaller，可能要 1~3 分钟）..." -ForegroundColor Cyan
& $BuildPython @PyInstallerArgs
if ($LASTEXITCODE -ne 0) { throw "PyInstaller 打包失败（exit code $LASTEXITCODE）" }

# 4. 校验产物并打印路径/大小
if (-not (Test-Path $ExePath)) {
    throw "打包命令结束了，但没有找到产物：$ExePath"
}

$SizeBytes = (Get-Item $ExePath).Length
$SizeMb = "{0:N2}" -f ($SizeBytes / 1MB)

Write-Host ""
Write-Host "构建完成" -ForegroundColor Green
Write-Host "  产物路径: $ExePath"
Write-Host "  产物大小: $SizeMb MB"

# 5. 冒烟验证：exe 能正常跑起来打印版本号
Write-Host ""
Write-Host "[build] 冒烟验证：$Name.exe --version" -ForegroundColor Cyan
try {
    $VersionOutput = & $ExePath --version 2>&1
    if ($LASTEXITCODE -eq 0) {
        Write-Host "  $VersionOutput" -ForegroundColor DarkGray
    }
    else {
        Write-Warning "exe --version 退出码为 $LASTEXITCODE（打包环境可能缺依赖，请手动运行确认）"
    }
}
catch {
    Write-Warning "exe 冒烟验证失败：$_（请手动运行 $ExePath 确认）"
}

exit 0
