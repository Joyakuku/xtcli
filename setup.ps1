<#
.SYNOPSIS
  xtcli 环境引导: 创建/修复项目 venv, 并生成 bin 下的命令 shim。

.DESCRIPTION
  必须在 Python 环境存在之前运行, 所以用 PowerShell。

  1) 用 `py -0p` 枚举已注册的独立 CPython, 逐个验证 venv + ensurepip 可用
  2) **拒绝 conda/anaconda 底座** —— venv 只隔离 site-packages, 它的
     python.exe 加载的仍是 base 的 pythonXY.dll 与标准库。所以"建在
     anaconda 上的 venv"其实是 anaconda + 一层包目录覆盖, 不是独立环境。
  3) 创建 <home>\.venv
  4) 把选择结果记入 xtcli.json 的 env 段（便于 doctor 追溯）
  5) 生成 bin\*.cmd。运行时零第三方依赖, 因此**不需要 pip install**。
  6) -Dev 时才安装 requirements-dev.txt

.EXAMPLE
  xtcli-setup
  xtcli-setup -Force
  xtcli-setup -Dev
#>
[CmdletBinding()]
param(
    [switch]$Force,
    [switch]$Dev,
    [switch]$Pyocd
)

$ErrorActionPreference = 'Stop'

$XtHome  = (Resolve-Path -LiteralPath $PSScriptRoot).Path
$SrcDir  = Join-Path $XtHome 'src'
# 被 dot-source (`. .\setup.ps1`) 时不能 exit —— 那会结束用户的整个会话。
# 正常用法是 `.\setup.ps1` 或 xtcli-setup.cmd; 这里两个用法都照顾到。
$XtDotSourced = ($MyInvocation.InvocationName -eq '.')

$VenvDir = Join-Path $XtHome '.venv'
$VenvPy  = Join-Path $VenvDir 'Scripts\python.exe'
$BinDir  = Join-Path $XtHome 'bin'
$CondaMarkers = @('anaconda', 'miniconda', 'conda')

function Info([string]$m)  { Write-Host "    $m" }
function Ok([string]$m)    { Write-Host "[ok] $m" -ForegroundColor Green }
function Warn2([string]$m) { Write-Host "[!]  $m" -ForegroundColor Yellow }
function Err2([string]$m)  { Write-Host "[x]  $m" -ForegroundColor Red }
function Is-Conda([string]$p) {
    if (-not $p) { return $false }
    $low = $p.ToLower()
    foreach ($m in $CondaMarkers) { if ($low.Contains($m)) { return $true } }
    return $false
}

$Probe = 'import json,sys;print(json.dumps({"exe":sys.executable,"base":getattr(sys,"base_prefix",sys.prefix),"ver":"%d.%d.%d"%sys.version_info[:3]}))'

Write-Host ''
Write-Host "==> xtcli 环境引导" -ForegroundColor Cyan
Info "home : $XtHome"
Info "venv : $VenvDir"

# ---------------------------------------------------------------------------
# 1) 枚举候选解释器
# ---------------------------------------------------------------------------
$candidates = @()
$pyList = & py -0p 2>$null
foreach ($line in $pyList) {
    if ($line -match '^\s*-V:([\d.]+)\s*\*?\s*(.+?python\.exe)\s*$') {
        $candidates += [pscustomobject]@{ Ver = $Matches[1]; Exe = $Matches[2].Trim() }
    }
}
if ($candidates.Count -eq 0) {
    Warn2 'py launcher 未列出任何解释器, 回退到 PATH 上的 python'
    $onPath = (Get-Command python -ErrorAction SilentlyContinue).Source
    if ($onPath) { $candidates += [pscustomobject]@{ Ver = '0.0.0'; Exe = $onPath } }
}
if ($candidates.Count -eq 0) {
    Err2 '找不到可用的 Python 解释器'
    Info '请安装独立 CPython (python.org) 后重试; 不要用 anaconda 作为底座'
    if ($XtDotSourced) { return } else { exit 3 }
}
# 版本倒序: 优先新版本
$candidates = $candidates | Sort-Object -Property @{ Expression = { [version]$_.Ver }; Descending = $true }

$chosen = $null
foreach ($c in $candidates) {
    if (Is-Conda $c.Exe) {
        Warn2 "跳过 conda 解释器: $($c.Exe)"
        continue
    }
    $json = & $c.Exe -c $Probe 2>$null
    if ($LASTEXITCODE -ne 0 -or -not $json) {
        Warn2 "跳过 (探测失败): $($c.Exe)"
        continue
    }
    $info = ($json | Select-Object -Last 1) | ConvertFrom-Json
    if (Is-Conda $info.base) {
        Warn2 "跳过 (底座是 conda): $($c.Exe)  base=$($info.base)"
        continue
    }
    & $c.Exe -c 'import venv, ensurepip' 2>$null
    if ($LASTEXITCODE -ne 0) {
        Warn2 "跳过 (缺 venv/ensurepip): $($c.Exe)"
        continue
    }
    $chosen = [pscustomobject]@{ Exe = $info.exe; Base = $info.base; Ver = $info.ver }
    break
}

if (-not $chosen) {
    Err2 '没有可用的独立 CPython (全部候选都被排除)'
    Info '被排除的原因会在上面逐条列出; 典型是底座为 conda 或缺 ensurepip'
    if ($XtDotSourced) { return } else { exit 3 }
}
Ok "选定解释器: $($chosen.Exe)  (Python $($chosen.Ver), 底座 $($chosen.Base))"

# ---------------------------------------------------------------------------
# 2) 创建 venv
# ---------------------------------------------------------------------------
if ($Force -and (Test-Path -LiteralPath $VenvDir)) {
    Warn2 'Force: 删除已有 venv'
    Remove-Item -LiteralPath $VenvDir -Recurse -Force
}
if (Test-Path -LiteralPath $VenvPy) {
    $existing = & $VenvPy -c 'import sys;print("%d.%d.%d"%sys.version_info[:3])' 2>$null
    Ok "venv 已存在 (Python $existing) —— 跳过创建 (需要重建请加 -Force)"
} else {
    Write-Host '    创建 venv ...'
    & $chosen.Exe -m venv $VenvDir
    if ($LASTEXITCODE -ne 0 -or -not (Test-Path -LiteralPath $VenvPy)) {
        Err2 'venv 创建失败'
        if ($XtDotSourced) { return } else { exit 3 }
    }
    Ok "venv 创建完成: $VenvDir"
}

# ---------------------------------------------------------------------------
# 3) 记录到 xtcli.json（用 venv 里的解释器执行, 复用 config.py 的 schema）
# ---------------------------------------------------------------------------
$env:XTCLI_SRC = $SrcDir
$env:XTCLI_BASE = $chosen.Exe
$env:XTCLI_BASE_PREFIX = $chosen.Base
$env:XTCLI_BASE_VER = $chosen.Ver
$record = @'
import os, sys
sys.path.insert(0, os.environ["XTCLI_SRC"])
from xtcli import config
cfg = config.load()
config.remember_env(cfg, base=os.environ["XTCLI_BASE"],
                    base_prefix=os.environ["XTCLI_BASE_PREFIX"],
                    version=os.environ["XTCLI_BASE_VER"])
config.save(cfg)
print("env recorded")
'@
$recorded = $record | & $VenvPy - 2>&1
if ($LASTEXITCODE -eq 0) { Ok '已记录环境信息到 xtcli.json' } else { Warn2 "记录环境信息失败: $recorded" }

# ---------------------------------------------------------------------------
# 4) 生成 shim
# ---------------------------------------------------------------------------
New-Item -ItemType Directory -Force -Path $BinDir | Out-Null

$shimBody = @'
@echo off
setlocal
for %%I in ("%~dp0..") do set "XTCLI_HOME=%%~fI"
if not exist "%XTCLI_HOME%\.venv\Scripts\python.exe" (
  echo [x]  venv 未就绪: %XTCLI_HOME%\.venv
  echo     请先运行: xtcli-setup
  exit /b 3
)
set "PYTHONPATH=%XTCLI_HOME%\src;%PYTHONPATH%"
"%XTCLI_HOME%\.venv\Scripts\python.exe" -X utf8 -m xtcli __VERB__ %*
exit /b %ERRORLEVEL%
'@

$shims = [ordered]@{
    'stm32-init-pj.cmd'  = 'init -Target stm32'
    'stm32-build-pj.cmd' = 'build -Target stm32'
    'stm32-burn-pj.cmd'  = 'burn -Target stm32'
    'esp32-init-pj.cmd'  = 'init -Target espidf'
    'esp32-build-pj.cmd' = 'build -Target espidf'
    'esp32-burn-pj.cmd'  = 'burn -Target espidf'
    'pj-init.cmd'        = 'init'
    'pj-build.cmd'       = 'build'
    'pj-burn.cmd'        = 'burn'
    'xtcli-doctor.cmd'   = 'doctor'
    'xtcli.cmd'          = ''
}
foreach ($name in $shims.Keys) {
    $body = $shimBody.Replace('__VERB__', $shims[$name])
    Set-Content -LiteralPath (Join-Path $BinDir $name) -Value $body -Encoding ascii -NoNewline
}
$setupShim = @'
@echo off
pwsh -NoProfile -ExecutionPolicy Bypass -File "%~dp0..\setup.ps1" %*
exit /b %ERRORLEVEL%
'@
Set-Content -LiteralPath (Join-Path $BinDir 'xtcli-setup.cmd') -Value $setupShim -Encoding ascii -NoNewline
Ok "已生成 $($shims.Count + 1) 个命令 shim -> $BinDir"

# ---------------------------------------------------------------------------
# 4b) MSYS2 侧的无后缀包装
#     bash 查 PATH **不会**自动补 .cmd（只对 .exe 隐式补齐），所以 MSYS 终端里
#     裸名 stm32-build-pj 找不到文件。这里为每个 shim 生成同名无后缀包装:
#     内容只有 4 行, 按自身位置推出 ../bin/<name>.cmd（位置无关, 搬目录也不坏）。
#     不需要 chmod: MSYS2 的 noacl 模式按 shebang 判定可执行（已实测）。
# ---------------------------------------------------------------------------
$MsysBinDir = Join-Path $XtHome 'bin-msys'
New-Item -ItemType Directory -Force -Path $MsysBinDir | Out-Null
$wrapperBody = @'
#!/usr/bin/env bash
# xtcli 的 MSYS 包装: 让裸名可用 (bash 查 PATH 不会自动补 .cmd)
# 位置无关: 由自身位置推出 ../bin/__NAME__.cmd
_xt_here=$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)
exec "$_xt_here/../bin/__NAME__.cmd" "$@"
'@
$wrapperNames = @($shims.Keys | ForEach-Object { $_ -replace '\.cmd$', '' }) + 'xtcli-setup'
foreach ($name in $wrapperNames) {
    $body = $wrapperBody.Replace('__NAME__', $name)
    $target = Join-Path $MsysBinDir $name
    try {
        # UTF-8 且**不带 BOM**（带 BOM 会让 bash 把 shebang 认成非法行）
        [System.IO.File]::WriteAllText($target, $body, (New-Object System.Text.UTF8Encoding($false)))
    } catch {
        Set-Content -LiteralPath $target -Value $body -Encoding ascii -NoNewline
    }
}
Ok "已生成 $($wrapperNames.Count) 个 MSYS 包装 (裸名可用) -> $MsysBinDir"

# ---------------------------------------------------------------------------
# 5) 开发依赖（可选）
# ---------------------------------------------------------------------------
if ($Dev) {
    Write-Host '    安装开发依赖 (requirements-dev.txt) ...'
    & $VenvPy -m pip install -q -r (Join-Path $XtHome 'requirements-dev.txt')
    if ($LASTEXITCODE -eq 0) { Ok '开发依赖安装完成' } else { Warn2 '开发依赖安装失败 (不影响运行时)' }
}

# ---------------------------------------------------------------------------
# 5b) 可选: pyOCD（独立 venv, 不污染核心的零依赖）
#     价值: 用 CMSIS-Pack 自动获取 flash 算法, 覆盖更多 Cortex-M 厂商
# ---------------------------------------------------------------------------
# -Force 的语义是"干净重建": 隔离的 pyOCD venv 也要一起清掉。否则一个旧环境
# (可能建在 conda 底座上、或依赖已失效) 会被 flash.py 优先选中, "隔离"名存实亡。
$PyocdVenvPath = Join-Path $XtHome '.venv-pyocd'
if ($Force -and (Test-Path -LiteralPath $PyocdVenvPath)) {
    Warn2 'Force: 删除已有 .venv-pyocd (隔离的 pyOCD 环境)'
    Remove-Item -LiteralPath $PyocdVenvPath -Recurse -Force
    if (-not $Pyocd) { Info '如需 pyOCD 请加 -Pyocd 重新安装; 不加则仍可用 openocd 烧录' }
}

if ($Pyocd) {
    $PyocdVenv = Join-Path $XtHome '.venv-pyocd'
    $PyocdPy = Join-Path $PyocdVenv 'Scripts\python.exe'
    if (-not (Test-Path -LiteralPath $PyocdPy)) {
        Write-Host '    创建隔离 venv (.venv-pyocd) ...'
        & $chosen.Exe -m venv $PyocdVenv
    }    if (Test-Path -LiteralPath $PyocdPy) {
        Write-Host '    安装 pyocd ...'
        & $PyocdPy -m pip install -q --disable-pip-version-check pyocd
        if ($LASTEXITCODE -eq 0) {
            $v = & (Join-Path $PyocdVenv 'Scripts\pyocd.exe') --version 2>$null
            Ok "pyocd 安装完成 (v$v), 位于 $PyocdVenv"
            Info '用它烧录: pj-burn -Flasher pyocd'
        } else {
            Warn2 'pyocd 安装失败 (不影响核心功能; 仍可用 openocd)'
        }
    }
}

# ---------------------------------------------------------------------------
# 6) 自检
# ---------------------------------------------------------------------------
# 自检要临时改 PYTHONPATH; 结束必须**还原**而不是一律删除 ——
# `$env:` 写的是**进程环境**, 所以即便用 `.\setup.ps1` 正常运行, 改动也会留在
# 调用者的 shell 里; 原来无条件 Remove-Item 会把用户自己的 PYTHONPATH 抹掉。
$prevPythonPath = $env:PYTHONPATH
$env:PYTHONPATH = $SrcDir
try {
    $check = & $VenvPy -c 'import xtcli, sys; print(xtcli.__version__); print(sys.prefix)' 2>&1
    $checkCode = $LASTEXITCODE
} finally {
    if ($null -eq $prevPythonPath) {
        Remove-Item env:PYTHONPATH -ErrorAction SilentlyContinue
    } else {
        $env:PYTHONPATH = $prevPythonPath
    }
}

Write-Host ''
if ($checkCode -eq 0) {
    Ok "自检通过: xtcli $($check[0])"
    Info "解释器: $VenvPy"
    Info '把 bin 目录加入 PATH 后即可使用: stm32-init-pj / stm32-build-pj / stm32-burn-pj / xtcli-doctor'
    if ($XtDotSourced) { return } else { exit 0 }
} else {
    Err2 "自检失败: $check"
    if ($XtDotSourced) { return } else { exit 3 }
}
