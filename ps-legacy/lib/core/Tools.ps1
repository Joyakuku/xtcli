# =============================================================================
#  xtcli / core / Tools.ps1
#
#  工具发现 + 进程执行。完全不含芯片知识。
#
#  发现策略 (按用户约定): 主根 E:\env (与 E:\tools 同级), 次根 E:\tools\msys64
#  (make / openocd 实际所在)。可被环境变量 XTCLI_ROOTS 或 xtcli.json 覆盖。
#  发现结果会缓存到 xtcli.json, 每次运行只做存在性校验; 失效则自动重发现。
# =============================================================================

function Get-XtSearchRoots {
    $roots = New-Object System.Collections.Generic.List[string]
    if ($env:XTCLI_ROOTS) {
        foreach ($r in ($env:XTCLI_ROOTS -split ';')) { if ($r.Trim()) { $roots.Add($r.Trim()) } }
    }
    $cfg = Read-XtConfig
    if ($cfg.ContainsKey('roots') -and $cfg['roots']) {
        foreach ($r in $cfg['roots']) { $roots.Add([string]$r) }
    }
    $roots.Add('E:\env')           # 工具链主根
    $roots.Add('E:\tools\msys64')  # make / openocd
    $resolved = @()
    foreach ($r in $roots) {
        if (Test-Path -LiteralPath $r) { $resolved += (Resolve-Path -LiteralPath $r).Path }
    }
    return @($resolved | Select-Object -Unique)
}

# ----------------------------------------------------------------------------
# gcc (arm-none-eabi)
# ----------------------------------------------------------------------------
function Find-XtGccCandidate {
    param([Parameter(Mandatory)][string[]]$Roots)

    $found = New-Object System.Collections.Generic.List[object]
    foreach ($r in $Roots) {
        # 1) 稳定别名优先。用户升级工具链时只需重指 junction, 工程配置无需改动。
        foreach ($alias in @('Arm\cortex-m', 'Arm\arm-none-eabi', 'Arm\gcc-arm-none-eabi', 'gcc-arm-none-eabi')) {
            $p = Join-Path $r "$alias\bin\arm-none-eabi-gcc.exe"
            if (Test-Path -LiteralPath $p) {
                $found.Add([pscustomobject]@{ Path = $p; VersionDir = $null; Alias = $true; Root = $r })
            }
        }
        # 2) 版本化目录 (<root>\Arm\<version>\bin)
        $arm = Join-Path $r 'Arm'
        if (Test-Path -LiteralPath $arm) {
            foreach ($d in (Get-ChildItem -LiteralPath $arm -Directory -Force -ErrorAction SilentlyContinue)) {
                $p = Join-Path $d.FullName 'bin\arm-none-eabi-gcc.exe'
                if (Test-Path -LiteralPath $p) {
                    $found.Add([pscustomobject]@{ Path = $p; VersionDir = $d.Name; Alias = $false; Root = $r })
                }
            }
        }
        # 3) 浅层兜底 (<root>\<x>\<y>\bin), 例如 esp 等 SDK 自带的工具链
        foreach ($d1 in (Get-ChildItem -LiteralPath $r -Directory -Force -ErrorAction SilentlyContinue)) {
            foreach ($d2 in (Get-ChildItem -LiteralPath $d1.FullName -Directory -Force -ErrorAction SilentlyContinue)) {
                $p = Join-Path $d2.FullName 'bin\arm-none-eabi-gcc.exe'
                if (Test-Path -LiteralPath $p) {
                    $found.Add([pscustomobject]@{ Path = $p; VersionDir = $d2.Name; Alias = $false; Root = $r })
                }
            }
        }
    }

    # 去重: 按解析 junction 后的真实路径。别名先入, 所以同一真实路径上别名胜出。
    $seen = @{}
    $uniq = @()
    foreach ($f in $found) {
        $real = Resolve-XtRealPath $f.Path
        if (-not $seen.ContainsKey($real)) {
            $seen[$real] = $true
            $uniq += [pscustomobject]@{ Path = $f.Path; RealPath = $real; VersionDir = $f.VersionDir; Alias = $f.Alias; Root = $f.Root }
        }
    }
    # 别名优先, 其次版本号倒序
    return @($uniq | Sort-Object -Property @{ Expression = { -not $_.Alias } }, @{ Expression = { $_.VersionDir }; Descending = $true })
}

# ----------------------------------------------------------------------------
# make / openocd
# ----------------------------------------------------------------------------
function Find-XtMakeCandidate {
    param([Parameter(Mandatory)][string[]]$Roots)
    $rels = @('usr\bin\make.exe', 'mingw64\bin\make.exe', 'mingw64\bin\mingw32-make.exe', 'bin\make.exe', 'usr\bin\mingw32-make.exe')
    foreach ($r in $Roots) {
        foreach ($rel in $rels) {
            $p = Join-Path $r $rel
            if (Test-Path -LiteralPath $p) { return $p }
        }
    }
    $c = Get-Command make -ErrorAction SilentlyContinue
    if ($c) { return $c.Source }
    return $null
}

function Find-XtOpenOcdCandidate {
    param([Parameter(Mandatory)][string[]]$Roots)
    $rels = @('mingw64\bin\openocd.exe', 'bin\openocd.exe', 'mingw32\bin\openocd.exe')
    foreach ($r in $Roots) {
        foreach ($rel in $rels) {
            $p = Join-Path $r $rel
            if (Test-Path -LiteralPath $p) { return $p }
        }
    }
    $c = Get-Command openocd -ErrorAction SilentlyContinue
    if ($c) { return $c.Source }
    return $null
}

function Get-XtOpenOcdScripts {
    <# openocd.exe 同级的 ../share/openocd/scripts #>
    param([Parameter(Mandatory)][string]$OpenOcdExe)
    $binDir = Split-Path -Path $OpenOcdExe -Parent
    foreach ($rel in @('..\share\openocd\scripts', '..\..\share\openocd\scripts')) {
        $p = Join-Path $binDir $rel
        if (Test-Path -LiteralPath (Join-Path $p 'target\stm32f1x.cfg')) { return (Resolve-Path -LiteralPath $p).Path }
        if (Test-Path -LiteralPath (Join-Path $p 'interface\stlink.cfg')) { return (Resolve-Path -LiteralPath $p).Path }
    }
    return $null
}

# ----------------------------------------------------------------------------
# 版本探测 (缓存)
# ----------------------------------------------------------------------------
function Get-XtToolVersion {
    param(
        [Parameter(Mandatory)][string]$Exe,
        [switch]$Refresh
    )
    $cfg = Read-XtConfig
    if (-not $cfg.ContainsKey('versions')) { $cfg['versions'] = @{} }
    $key = $Exe.ToLowerInvariant()
    if (-not $Refresh -and $cfg['versions'].ContainsKey($key)) {
        return [string]$cfg['versions'][$key]
    }
    $ver = $null
    try {
        $first = (& $Exe --version 2>&1 | Select-Object -First 1)
        if ($first) { $ver = ([string]$first).Trim() }
    } catch { }
    if ($ver) {
        $cfg['versions'][$key] = $ver
        Save-XtConfig -Config $cfg
    }
    return $ver
}

# ----------------------------------------------------------------------------
# 汇总
# ----------------------------------------------------------------------------
function Get-XtToolchain {
    param([switch]$Refresh)
    $roots = Get-XtSearchRoots

    $tc = [ordered]@{
        Roots          = $roots
        GccRoot        = $null
        Prefix         = $null
        CC             = $null; AS = $null; CP = $null
        SZ             = $null; OD = $null; NM = $null; AR = $null
        GccPath        = $null; GccVersion = $null; GccAlias = $false
        Make           = $null; MakeVersion = $null
        OpenOcd        = $null; OpenOcdVersion = $null; OpenOcdScripts = $null
        Missing        = @()
    }

    $gcc = Find-XtGccCandidate -Roots $roots
    if ($gcc.Count -gt 0) {
        $g = $gcc[0]
        $tc.GccPath = $g.Path
        $tc.GccAlias = $g.Alias
        $tc.GccRoot = To-XtPosix (Split-Path -Path $g.Path -Parent)
        $tc.Prefix = "$($tc.GccRoot)/arm-none-eabi-"
        $tc.CC = "$($tc.GccRoot)/arm-none-eabi-gcc.exe"
        $tc.AS = $tc.CC
        $tc.CP = "$($tc.GccRoot)/arm-none-eabi-objcopy.exe"
        $tc.SZ = "$($tc.GccRoot)/arm-none-eabi-size.exe"
        $tc.OD = "$($tc.GccRoot)/arm-none-eabi-objdump.exe"
        $tc.NM = "$($tc.GccRoot)/arm-none-eabi-nm.exe"
        $tc.AR = "$($tc.GccRoot)/arm-none-eabi-ar.exe"
        $tc.GccVersion = Get-XtToolVersion -Exe $g.Path -Refresh:$Refresh
    } else {
        $tc.Missing += 'arm-none-eabi-gcc (在 E:\env 下未找到)'
    }

    $mk = Find-XtMakeCandidate -Roots $roots
    if ($mk) {
        $tc.Make = $mk
        $tc.MakeVersion = Get-XtToolVersion -Exe $mk -Refresh:$Refresh
    } else {
        $tc.Missing += 'make (在 E:\tools\msys64 下未找到)'
    }

    $ocd = Find-XtOpenOcdCandidate -Roots $roots
    if ($ocd) {
        $tc.OpenOcd = $ocd
        $tc.OpenOcdVersion = Get-XtToolVersion -Exe $ocd -Refresh:$Refresh
        $tc.OpenOcdScripts = Get-XtOpenOcdScripts -OpenOcdExe $ocd
    } else {
        $tc.Missing += 'openocd (在 E:\tools\msys64 下未找到)'
    }

    return [pscustomobject]$tc
}

# ----------------------------------------------------------------------------
# 设备数据表
# ----------------------------------------------------------------------------
function Get-XtDevices {
    $p = Join-Path (Get-XtDataDir) 'stm32-devices.json'
    $d = Read-XtJsonFile $p
    if (-not $d) { throw "设备数据表缺失或损坏: $p" }
    return $d
}

# ----------------------------------------------------------------------------
# 探针缓存 (doctor -Probe 识别到后写入, burn 直接复用)
# ----------------------------------------------------------------------------
function Get-XtCachedProbe {
    $cfg = Read-XtConfig
    if ($cfg.ContainsKey('probe') -and $cfg['probe']) { return $cfg['probe'] }
    return $null
}

function Set-XtCachedProbe {
    param(
        [Parameter(Mandatory)][string]$Name,
        [Parameter(Mandatory)][string]$InterfaceCfg
    )
    try {
        $cfg = Read-XtConfig
        $cfg['probe'] = @{ name = $Name; interface = $InterfaceCfg; at = (Get-Date).ToString('s') }
        Save-XtConfig -Config $cfg
    } catch { }
}

# ----------------------------------------------------------------------------
# 进程执行
# ----------------------------------------------------------------------------
function Invoke-XtProcess {
    <# 执行原生程序并捕获输出。
       -PrependPath 用于把 msys64\usr\bin 放到 PATH 前面, 让 make 稳定地找到
       sh.exe —— 这样 rules.mk 里的 shell 分支判定不依赖用户 PATH 的偶然状态。 #>
    param(
        [Parameter(Mandatory)][string]$File,
        [string[]]$Arguments = @(),
        [string]$WorkingDirectory = $null,
        [string[]]$PrependPath = @(),
        [switch]$Quiet
    )
    if (-not $WorkingDirectory) { $WorkingDirectory = (Get-Location).Path }
    $oldPath = $env:PATH
    $oldLoc = (Get-Location).Path
    try {
        if ($PrependPath.Count -gt 0) {
            $env:PATH = (($PrependPath -join ';') + ';' + $oldPath)
        }
        Set-Location -LiteralPath $WorkingDirectory
        $out = & $File @Arguments 2>&1
        $code = $LASTEXITCODE
        $lines = @()
        if ($null -ne $out) { $lines = @($out | ForEach-Object { [string]$_ }) }
        if (-not $Quiet) { foreach ($l in $lines) { Write-XtLog -Level raw -Message $l } }
        return [pscustomobject]@{ ExitCode = [int]$code; Lines = $lines }
    } catch {
        return [pscustomobject]@{ ExitCode = -1; Lines = @($_.Exception.Message) }
    } finally {
        $env:PATH = $oldPath
        Set-Location -LiteralPath $oldLoc
    }
}
