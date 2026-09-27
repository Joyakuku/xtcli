# =============================================================================
#  xtcli / backends / stm32 / Actions.ps1
#
#  四个动作: init / build / burn / doctor, 外加 backend 注册。
#  动作一律返回 XtResult (Code/Message/Data), 由入口统一 exit。
# =============================================================================

function Get-XtStm32ModelFingerprint {
    <# 把决定构建产物的关键参数压成一个短指纹, 写进 config.mk 注释里。
       build 时对比, 用来发现"config.mk 是旧的、与工程当前配置不一致"。
       这类不一致会静默产出错误固件 —— 实测踩过: 芯片型号解析修好后忘记重新
       init, config.mk 里还留着 CPU 为空的旧值, 于是目标架构退化成 armv4t,
       报 "selected processor does not support `cpsid i' in Thumb mode"。 #>
    param([Parameter(Mandatory)]$Model)
    $parts = @(
        $Model.Source, $Model.Config, $Model.Device, $Model.Cpu, $Model.Fpu,
        $Model.Opt, $Model.Dbg, $Model.OutDir, $Model.BuildDir, $Model.Target,
        ($Model.Defines -join ','), ($Model.Includes -join ','),
        ($Model.SrcDirs -join ','), ($Model.SrcFiles -join ','),
        ($Model.AsmSrcs -join ','), [string]$Model.LdScript,
        ($Model.Libs -join ','), ($Model.LibPaths -join ',')
    ) -join '|'
    $sha = [System.Security.Cryptography.SHA256]::Create()
    try {
        $bytes = $sha.ComputeHash([System.Text.Encoding]::UTF8.GetBytes($parts))
        return (($bytes | ForEach-Object { $_.ToString('x2') }) -join '').Substring(0, 16)
    } finally {
        $sha.Dispose()
    }
}

function New-XtStm32ConfigMk {
    <# 模型 -> config.mk。路径写工程相对形式, 让工程可以整体搬走。 #>
    param([Parameter(Mandatory)]$Model)
    $rel = { param($p) Get-XtRelative -Base $Model.Root -Path $p }
    $L = New-Object System.Collections.Generic.List[string]

    $L.Add('# =============================================================================')
    $L.Add('#  xtcli 生成 —— 请勿手改; 重新生成请运行: stm32-init-pj')
    $L.Add("#  参数来源: $($Model.Source)   配置: $($Model.Config)   芯片: $($Model.Device)")
    $L.Add("#  XT_FINGERPRINT: $(Get-XtStm32ModelFingerprint -Model $Model)")
    $L.Add('#  (上面的指纹用于让 stm32-build-pj 发现本文件是否已与工程配置脱节)')
    $L.Add('# =============================================================================')
    $L.Add('')
    $L.Add("TARGET      := $($Model.Target)")
    $L.Add("OUT_DIR     := $($Model.OutDir)")
    $L.Add("BUILD_DIR   := $($Model.BuildDir)")
    $L.Add('')

    $cpu = '-mthumb'
    if ($Model.Cpu) { $cpu = "-mcpu=$($Model.Cpu) -mthumb" }
    $L.Add("CPU         := $cpu")
    $L.Add("FPU         := $($Model.Fpu)")
    $L.Add("OPT         := $($Model.Opt)")
    $L.Add("DBG         := $($Model.Dbg)")
    $L.Add('')

    $defs = ($Model.Defines | Where-Object { $_ } | ForEach-Object { "-D$_" })
    $L.Add("C_DEFS      := $($defs -join ' ')")
    $incs = ($Model.Includes | Where-Object { $_ } | ForEach-Object { "-I$(& $rel $_)" })
    if ($incs.Count -eq 0) { $L.Add('C_INCLUDES  :=') }
    else {
        $L.Add('C_INCLUDES  := \')
        for ($i = 0; $i -lt $incs.Count; $i++) {
            $tail = if ($i -eq $incs.Count - 1) { '' } else { ' \' }
            $L.Add("  $($incs[$i])$tail")
        }
    }
    $L.Add('')

    $srcs = @($Model.SrcDirs | Where-Object { $_ } | ForEach-Object { & $rel $_ })
    $L.Add("SRC_DIRS    := $($srcs -join ' ')")

    # 显式源文件清单 (来自 .ewp 等原 IDE 工程)。非空时 rules.mk 不再扫描 SRC_DIRS。
    $explicit = @($Model.SrcFiles | Where-Object { $_ } | ForEach-Object { & $rel $_ })
    if ($explicit.Count -gt 0) {
        $L.Add("# 原 IDE 工程的精确源文件清单 ($($explicit.Count) 个) —— 非空时 rules.mk 不扫描 SRC_DIRS")
        $L.Add('C_SRCS_EXPLICIT := \')
        for ($k = 0; $k -lt $explicit.Count; $k++) {
            $tail = if ($k -eq $explicit.Count - 1) { '' } else { ' \' }
            $L.Add("  $($explicit[$k])$tail")
        }
    } else {
        $L.Add('C_SRCS_EXPLICIT :=')
    }

    $asms = @($Model.AsmSrcs | Where-Object { $_ } | ForEach-Object { & $rel $_ })
    $L.Add("ASM_SRCS    := $($asms -join ' ')")
    if ($Model.LdScript) { $L.Add("LDSCRIPT    := $(& $rel $Model.LdScript)") }
    $L.Add('')

    # 库: ':lcd.a' -> '-l:lcd.a'; 补上 nano.specs 需要的 libc/libm
    # 注意: 循环变量不要用 $l —— PowerShell 变量名大小写不敏感, 会覆盖上面的 $L
    $libs = @()
    foreach ($libItem in $Model.Libs) {
        if (-not $libItem) { continue }
        if ($libItem.StartsWith(':')) { $libs += "-l$libItem" } else { $libs += "-l$libItem" }
    }
    foreach ($must in @('-lc', '-lm')) { if ($libs -notcontains $must) { $libs += $must } }
    $L.Add("LIBS        := $($libs -join ' ')")
    $libPaths = @($Model.LibPaths | Where-Object { $_ } | ForEach-Object { "-L$(& $rel $_)" })
    $L.Add("LIBDIR      := $($libPaths -join ' ')")
    $L.Add('')

    $L.Add("OPENOCD_IF  := $($Model.Extra['OpenOcdIf'])")
    $L.Add("OPENOCD_TGT := $(([string]$Model.Extra['OcdTarget']) -replace '^target/','target/')")
    $L.Add('')
    return ($L -join "`r`n")
}

function Write-XtStm32Skeleton {
    <# 生成 <root>/xtcli/{config.mk,rules.mk,Makefile}; 没有根 Makefile 时补一个转发壳, 让裸 make 也能用。 #>
    param(
        [Parameter(Mandatory)]$Model,
        [hashtable]$Opt = @{}
    )
    $xtDir = Join-Path $Model.Root 'xtcli'
    New-Item -ItemType Directory -Force -Path $xtDir | Out-Null

    $cfgPath = Join-Path $xtDir 'config.mk'
    $overwrote = Test-Path -LiteralPath $cfgPath
    Set-Content -LiteralPath $cfgPath -Value (New-XtStm32ConfigMk -Model $Model) -Encoding UTF8

    $rulesSrc = Join-Path (Get-XtTemplateDir) 'stm32\rules.mk'
    $rulesDst = Join-Path $xtDir 'rules.mk'
    Copy-Item -LiteralPath $rulesSrc -Destination $rulesDst -Force

    $mkPath = Join-Path $xtDir 'Makefile'
    $mk = @'
# xtcli:generated
# =============================================================================
#  xtcli 生成的构建入口 (请勿手改; 重新生成请运行 stm32-init-pj)
#    构建:  stm32-build-pj       等价于  make -C <工程根> -f xtcli/Makefile
#    清理:  stm32-build-pj -Clean
#  路径相对工程根解析, 所以必须在工程根目录调用。
# =============================================================================
ifeq ($(notdir $(CURDIR)),xtcli)
$(error 请在工程根目录执行 make, 或直接使用 stm32-build-pj)
endif

XT_DIR := $(patsubst %/,%,$(dir $(lastword $(MAKEFILE_LIST))))

include $(XT_DIR)/config.mk
include $(XT_DIR)/rules.mk
'@
    Set-Content -LiteralPath $mkPath -Value $mk -Encoding UTF8

    # 根目录没有 Makefile 时补一个转发壳, 让 `make` 直接可用 (不覆盖用户已有文件)。
    # 首行哨兵让下次 init 能认出这是自己的产物, 而不是"工程自带构建系统"。
    $rootMkPath = Join-Path $Model.Root 'Makefile'
    $rootMkCreated = $false
    if (-not (Test-Path -LiteralPath $rootMkPath)) {
        $rootMk = @'
# xtcli:generated
# 转发到 xtcli/Makefile, 让裸 `make` 也能用。
# 不需要就删掉本文件 —— stm32-build-pj 不依赖它。
include xtcli/Makefile
'@
        Set-Content -LiteralPath $rootMkPath -Value $rootMk -Encoding UTF8
        $rootMkCreated = $true
    }

    return [pscustomobject]@{
        XtDir        = $xtDir
        ConfigMk     = $cfgPath
        RulesMk      = $rulesDst
        Makefile     = $mkPath
        RootMakefile = $rootMkCreated
        Overwrote    = $overwrote
    }
}

function Copy-XtStm32RuntimeStubs {
    <# nano.specs 下可能需要 _write/_sbrk 桩。CubeIDE 工程自带, EWARM-only 工程没有。 #>
    param([Parameter(Mandatory)]$Model)
    $coreSrc = Join-Path $Model.Root 'Core\Src'
    if (-not (Test-Path -LiteralPath $coreSrc)) { return @() }
    $copied = @()
    foreach ($name in @('syscalls.c', 'sysmem.c')) {
        $dst = Join-Path $coreSrc $name
        if (Test-Path -LiteralPath $dst) { continue }
        $src = Join-Path (Join-Path (Get-XtTemplateDir) 'stm32') $name
        if (Test-Path -LiteralPath $src) {
            Copy-Item -LiteralPath $src -Destination $dst -Force
            $copied += $dst
        }
    }
    return $copied
}

function Write-XtStm32CompileCommands {
    param(
        [Parameter(Mandatory)]$Model,
        [Parameter(Mandatory)]$Tools
    )
    $files = $Model.Extra['SourceFiles']
    if (-not $files -or $files.C.Count -eq 0) { return $null }
    if (-not $Tools.CC) { return $null }

    $rel = { param($p) Get-XtRelative -Base $Model.Root -Path $p }
    # 不要用 $args —— 那是 PowerShell 的自动变量
    $cflags = @()
    if ($Model.Cpu) { $cflags += "-mcpu=$($Model.Cpu)" }
    $cflags += '-mthumb'
    if ($Model.Fpu) { $cflags += ($Model.Fpu -split '\s+' | Where-Object { $_ }) }
    foreach ($d in $Model.Defines) { if ($d) { $cflags += "-D$d" } }
    foreach ($i in $Model.Includes) { if ($i) { $cflags += "-I$(& $rel $i)" } }
    if ($Model.Opt) { $cflags += $Model.Opt }
    if ($Model.Dbg) { $cflags += $Model.Dbg }

    $entries = @()
    foreach ($f in $files.C) {
        $entries += [pscustomobject][ordered]@{
            directory = To-XtPosix $Model.Root
            file      = To-XtPosix $f
            arguments = @((To-XtPosix $Tools.CC)) + $cflags + @('-c', (To-XtPosix $f), '-o', ((To-XtPosix $f) -replace '\.c$', '.o'))
        }
    }
    $out = Join-Path $Model.Root 'compile_commands.json'
    Write-XtJsonFile -Path $out -Object ([object[]]$entries)
    return [pscustomobject]@{ Path = $out; Count = $entries.Count }
}

# ----------------------------------------------------------------------------
# init
# ----------------------------------------------------------------------------
function Invoke-XtStm32Init {
    param([Parameter(Mandatory)][hashtable]$Ctx)
    $model = $Ctx.Model
    $opt = $Ctx.Opt

    # 确定性拒绝优先于任何写入 —— 绝不产出"能编但错"的工程
    if ($model.Refusals.Count -gt 0) {
        $r = $model.Refusals[0]
        Write-XtErr $r.Message
        foreach ($m in $r.Missing) { Write-XtInfo ("缺少: " + $m) }
        if ($r.Next) { Write-XtInfo ("建议: " + $r.Next) }
        return (New-XtResult -Code $r.Code -Message $r.Message -Data @{ Refusals = $model.Refusals })
    }

    if ($model.Source -eq 'makefile') {
        Write-XtOk '该工程自带 Makefile, 按"优先复用已有构建系统"策略, 无需初始化'
        Write-XtInfo 'stm32-build-pj 会直接调用工程自己的 Makefile'
        return (New-XtResult -Code 0 -Message '复用已有 Makefile' -Data @{ Source = 'makefile' })
    }

    Write-XtStep "初始化 $($model.Root)"

    # 链接脚本来自模板库时, 复制进工程, 让它自包含
    if ($model.LdScript -and -not (Test-XtWithin -Child $model.LdScript -Parent $model.Root)) {
        $dst = Join-Path $model.Root (Split-Path -Path $model.LdScript -Leaf)
        Copy-Item -LiteralPath $model.LdScript -Destination $dst -Force
        Write-XtInfo "链接脚本已从模板库复制进工程: $(Split-Path -Path $dst -Leaf)"
        $model.LdScript = $dst
        $model.Warnings += "链接脚本原先不存在, 已用同芯片模板补齐 (请核对 flash/ram 长度是否符合你的芯片)"
    }

    $skel = Write-XtStm32Skeleton -Model $model -Opt $opt
    Write-XtOk "已生成 $($skel.XtDir)\{config.mk, rules.mk, Makefile}"
    if ($skel.Overwrote) { Write-XtInfo 'config.mk 已存在, 已被重新生成覆盖' }
    if ($skel.RootMakefile) { Write-XtInfo '工程根无 Makefile, 已补一个转发壳 (裸 make 可用)' }

    $stubs = Copy-XtStm32RuntimeStubs -Model $model
    foreach ($s in $stubs) { Write-XtInfo "已补齐运行时桩: $(Split-Path -Path $s -Leaf)" }

    $cc = Write-XtStm32CompileCommands -Model $model -Tools $Ctx.Tools
    if ($cc) { Write-XtOk "已生成 compile_commands.json ($($cc.Count) 条)" }

    foreach ($w in $model.Warnings) { Write-XtWarn $w }
    if ($model.Extra['PrebuildStep']) {
        Write-XtWarn "该工程在 CubeIDE 里配置了 prebuild 钩子, xtcli 不会执行它:"
        Write-XtInfo ([string]$model.Extra['PrebuildStep'])
    }

    # init 的承诺是"直到可编译", 所以默认真的编一次
    if ($opt['NoBuild']) {
        Write-XtInfo '已跳过试编译 (-NoBuild)'
        return (New-XtResult -Code 0 -Message '初始化完成 (未试编译)')
    }

    Write-XtStep '试编译'
    $build = Invoke-XtStm32Build -Ctx $Ctx
    if ($build.Code -eq 0) {
        return (New-XtResult -Code 0 -Message '初始化完成, 试编译通过')
    }
    Write-XtErr '初始化已生成构建系统, 但试编译失败 —— 参数可能需要微调 (见上面的编译器输出)'
    return (New-XtResult -Code $build.Code -Message '初始化完成, 但试编译失败' -Data $build.Data)
}

# ----------------------------------------------------------------------------
# build
# ----------------------------------------------------------------------------
function Get-XtStm32MakePlan {
    param([Parameter(Mandatory)]$Model)
    $xt = Join-Path $Model.Root 'xtcli\Makefile'
    if (Test-Path -LiteralPath $xt) { return [pscustomobject]@{ File = 'xtcli/Makefile'; Owned = $true } }
    $root = Join-Path $Model.Root 'Makefile'
    if (Test-Path -LiteralPath $root) { return [pscustomobject]@{ File = 'Makefile'; Owned = $false } }
    return $null
}

function Invoke-XtStm32Build {
    param([Parameter(Mandatory)][hashtable]$Ctx)
    $model = $Ctx.Model
    $opt = $Ctx.Opt
    $tools = $Ctx.Tools

    if (-not $tools.Make) {
        Write-XtErr '环境缺少 make'
        return (New-XtResult -Code $script:XtExit.ENVIRONMENT -Message 'make 不可用')
    }
    $plan = Get-XtStm32MakePlan -Model $model
    if (-not $plan) {
        Write-XtErr '找不到构建入口 (既无 xtcli/Makefile 也无根 Makefile)'
        Write-XtInfo '请先运行: stm32-init-pj'
        return (New-XtResult -Code $script:XtExit.PROJECT -Message '缺少构建入口')
    }

    $msysBin = Split-Path -Path $tools.Make -Parent
    $jobs = [Environment]::ProcessorCount

    # config.mk 陈旧检测: 生成物与工程当前配置脱节时会静默编出错误固件
    if ($plan.Owned) {
        $cfgMkPath = Join-Path $model.Root 'xtcli\config.mk'
        if (Test-Path -LiteralPath $cfgMkPath) {
            $m = [regex]::Match((Get-Content -LiteralPath $cfgMkPath -Raw), 'XT_FINGERPRINT:\s*([0-9a-f]+)')
            $want = Get-XtStm32ModelFingerprint -Model $model
            if (-not $m.Success) {
                Write-XtWarn 'xtcli/config.mk 由本工具旧版本生成 (无指纹), 建议重新运行 stm32-init-pj'
            } elseif ($m.Groups[1].Value -ne $want) {
                Write-XtWarn 'xtcli/config.mk 与工程当前配置不一致 (陈旧或被改过)'
                Write-XtInfo "  文件指纹 $($m.Groups[1].Value)   当前 $want"
                Write-XtInfo '  建议先运行 stm32-init-pj 重新生成, 否则可能编出与预期不符的固件'
            }
        }
    }
    # 把 msys 的 bin 放到 PATH 最前, 让 make 稳定找到 sh.exe。
    # 这样 rules.mk 的 shell 分支判定不依赖用户 PATH 的偶然状态。
    $prepend = @($msysBin)
    $common = @('-C', $model.Root, '-f', $plan.File, "-j$jobs",
                "XT_TOOLCHAIN_BIN=$($tools.GccRoot)")
    if ($tools.OpenOcd) { $common += "OPENOCD=$(To-XtPosix $tools.OpenOcd)" }

    if ($opt['Clean']) {
        Write-XtStep "清理 ($($plan.File))"
        $cr = Invoke-XtProcess -File $tools.Make -Arguments ($common + @('clean')) `
                               -WorkingDirectory $model.Root -PrependPath $prepend
        if ($cr.ExitCode -ne 0) {
            # 清理失败绝不能吞掉 —— 否则接下来 make 可能对旧产物报 "Nothing to be done",
            # 看起来构建成功, 实际什么都没重编。
            Write-XtErr "清理失败 (make clean 退出码 $($cr.ExitCode)), 已中止"
            return (New-XtResult -Code $script:XtExit.BUILD -Message '清理失败' -Data @{ ExitCode = $cr.ExitCode })
        }
    }

    $targets = @()
    if ($opt['Target']) { $targets = @([string]$opt['Target']) }
    Write-XtStep "构建 ($($plan.File)$(if ($plan.Owned) { ', xtcli 生成' } else { ', 工程自带' }))"
    $r = Invoke-XtProcess -File $tools.Make -Arguments ($common + $targets) `
                          -WorkingDirectory $model.Root -PrependPath $prepend

    if ($r.ExitCode -eq 0) {
        return (New-XtResult -Code 0 -Message '构建成功')
    }
    Write-XtErr "构建失败 (make 退出码 $($r.ExitCode))"
    return (New-XtResult -Code $script:XtExit.BUILD -Message '构建失败' -Data @{ ExitCode = $r.ExitCode })
}

# ----------------------------------------------------------------------------
# burn
# ----------------------------------------------------------------------------
function Invoke-XtProbeTest {
    <# 试连一个探针。必须带超时 —— openocd 探不到探针时是卡住而不是报错。
       也必须带上 target 配置: interface/*.cfg 里没有 `transport select`,
       只加载 interface 会报 "session transport was not selected" 而误判为
       "探针没连上"(实测: 有真机也一样报)。带上 target 就是真实烧录的加载方式,
       所以这个自检的结论才和 burn 一致。 #>
    param(
        [Parameter(Mandatory)][string]$OpenOcd,
        [string]$Scripts,
        [Parameter(Mandatory)][string]$InterfaceCfg,
        [string]$TargetCfg,
        [int]$TimeoutSec = 8
    )
    $outFile = Join-Path $env:TEMP ('xtcli_probe_' + [guid]::NewGuid().ToString('N') + '.log')
    $errFile = "$outFile.err"
    $argList = @()
    if ($Scripts) { $argList += @('-s', (To-XtPosix $Scripts)) }
    $argList += @('-f', $InterfaceCfg)
    if ($TargetCfg) { $argList += @('-f', $TargetCfg) }
    $argList += @('-c', 'init;exit')
    $proc = $null
    try {
        $proc = Start-Process -FilePath $OpenOcd -ArgumentList $argList -NoNewWindow -PassThru `
                              -RedirectStandardOutput $outFile -RedirectStandardError $errFile
        $exited = $proc.WaitForExit($TimeoutSec * 1000)
        if (-not $exited) {
            try { $proc.Kill() } catch { }
            return [pscustomobject]@{ Ok = $false; TimedOut = $true; ExitCode = $null
                                      Text = "超时 ${TimeoutSec}s —— 探针未连接或驱动异常" }
        }
        $text = ''
        foreach ($f in @($outFile, $errFile)) {
            if (Test-Path -LiteralPath $f) { $text += (Get-Content -LiteralPath $f -Raw -ErrorAction SilentlyContinue) }
        }
        return [pscustomobject]@{ Ok = ($proc.ExitCode -eq 0); TimedOut = $false; ExitCode = $proc.ExitCode; Text = $text }
    } catch {
        return [pscustomobject]@{ Ok = $false; TimedOut = $false; ExitCode = $null; Text = $_.Exception.Message }
    } finally {
        foreach ($f in @($outFile, $errFile)) { Remove-Item -LiteralPath $f -Force -ErrorAction SilentlyContinue }
    }
}

function Invoke-XtStm32Burn {
    param([Parameter(Mandatory)][hashtable]$Ctx)
    $model = $Ctx.Model
    $opt = $Ctx.Opt
    $tools = $Ctx.Tools

    if (-not $tools.OpenOcd) {
        Write-XtErr '环境缺少 openocd'
        return (New-XtResult -Code $script:XtExit.ENVIRONMENT -Message 'openocd 不可用')
    }
    $devices = Get-XtDevices

    $useBin = [bool]$opt['Bin']
    $artifact = $null
    if ($opt['Elf']) {
        $artifact = [string]$opt['Elf']
    } else {
        $ext = if ($useBin) { 'bin' } else { 'elf' }
        $artifact = Join-Path $model.Root (Join-Path $model.OutDir "$($model.Target).$ext")
    }

    if (-not (Test-Path -LiteralPath $artifact)) {
        if ($opt['NoBuild']) {
            Write-XtErr "固件不存在: $artifact"
            return (New-XtResult -Code $script:XtExit.PROJECT -Message '固件不存在且指定了 -NoBuild')
        }
        Write-XtInfo '固件尚未构建, 先构建'
        $b = Invoke-XtStm32Build -Ctx $Ctx
        if ($b.Code -ne 0) { return $b }
    }
    if (-not (Test-Path -LiteralPath $artifact)) {
        Write-XtErr "构建后仍找不到固件: $artifact"
        return (New-XtResult -Code $script:XtExit.PROJECT -Message '找不到固件')
    }

    # --- openocd target 配置 (先算出来: 探针自检也要用它) ---
    $tgtCfg = $null
    if ($opt['OcdTarget']) { $tgtCfg = [string]$opt['OcdTarget'] } else { $tgtCfg = $model.Extra['OcdTarget'] }
    if (-not $tgtCfg) {
        Write-XtErr '无法确定 openocd target 配置'
        Write-XtInfo '用 -OcdTarget target/stm32f1x.cfg 显式指定'
        return (New-XtResult -Code $script:XtExit.PROJECT -Message '缺少 openocd target')
    }

    # --- 探针配置: 显式指定 > 上次识别到的 > 现场自动识别 ---
    $ifCfg = $null
    if ($opt['Interface']) {
        $ifCfg = [string]$opt['Interface']
    } else {
        $cached = Get-XtCachedProbe
        if ($cached -and $cached['interface']) {
            $ifCfg = [string]$cached['interface']
            Write-XtInfo "使用已识别到的探针: $($cached['name'])  ($ifCfg)"
        }
    }
    if (-not $ifCfg) {
        Write-XtStep '自动识别探针 (每个 8s 超时)'
        foreach ($p in $devices['probes']) {
            $t = Invoke-XtProbeTest -OpenOcd $tools.OpenOcd -Scripts $tools.OpenOcdScripts `
                                    -InterfaceCfg $p['cfg'] -TargetCfg $tgtCfg
            if ($t.Ok) {
                $ifCfg = $p['cfg']
                Set-XtCachedProbe -Name $p['name'] -InterfaceCfg $ifCfg
                Write-XtOk "识别到探针: $($p['name'])  ($ifCfg)"
                break
            }
            Write-XtInfo "$($p['name']) 未响应"
        }
    }
    if (-not $ifCfg) {
        Write-XtErr '没有识别到任何调试探针'
        Write-XtInfo '依次检查: USB 连接 / ST-LINK 驱动 (WinUSB) / SWD 接线 / 是否被 CubeIDE 或 ST-Link Utility 占用'
        Write-XtInfo '也可用 -Interface 强制指定, 例如 -Interface interface/cmsis-dap.cfg'
        return (New-XtResult -Code $script:XtExit.ENVIRONMENT -Message '未识别到探针')
    }

    $a = @()
    if ($tools.OpenOcdScripts) { $a += @('-s', (To-XtPosix $tools.OpenOcdScripts)) }
    $a += @('-f', $ifCfg, '-f', $tgtCfg)
    if ($opt['ProbeSerial']) { $a += @('-c', "adapter serial $($opt['ProbeSerial'])") }

    $fwd = To-XtPosix $artifact
    if ($useBin) {
        $addr = '0x08000000'
        if ($opt['Address']) { $addr = [string]$opt['Address'] }
        $a += @('-c', "program `"$fwd`" $addr verify reset exit")
    } else {
        $a += @('-c', "program `"$fwd`" verify reset exit")
    }

    Write-XtStep "烧录 $(Split-Path -Path $artifact -Leaf)  [$ifCfg + $tgtCfg]"
    $r = Invoke-XtProcess -File $tools.OpenOcd -Arguments $a -WorkingDirectory $model.Root

    if ($r.ExitCode -eq 0) {
        Write-XtOk '烧录完成并已复位运行'
        return (New-XtResult -Code 0 -Message '烧录成功')
    }

    Write-XtErr "烧录失败 (openocd 退出码 $($r.ExitCode))"
    $joined = ($r.Lines -join "`n")
    if ($joined -match 'open failed|No such file|unable to find|Could not find') {
        Write-XtInfo "探针没连上。依次检查: USB 连接 / ST-LINK 驱动 (WinUSB) / SWD 接线 / 是否被 CubeIDE 或 ST-Link Utility 占用"
        Write-XtInfo "换探针可试: --interface interface/cmsis-dap.cfg  (先跑 xtcli-doctor -Probe 会自动逐个试)"
    }
    if ($joined -match 'read protection|RDP|protected') {
        Write-XtInfo '芯片可能开启了读保护, 需要先解除保护 (会全片擦除)'
    }
    return (New-XtResult -Code $script:XtExit.BURN -Message '烧录失败' -Data @{ ExitCode = $r.ExitCode })
}

# ----------------------------------------------------------------------------
# doctor
# ----------------------------------------------------------------------------
function Invoke-XtStm32Doctor {
    param([Parameter(Mandatory)][hashtable]$Ctx)
    $model = $Ctx.Model
    $tools = $Ctx.Tools
    $opt = $Ctx.Opt
    $devices = Get-XtDevices

    Write-XtStep 'xtcli 环境'
    Write-XtInfo "版本        : $($script:XtCliVersion)"
    Write-XtInfo "工具搜索根  : $($tools.Roots -join '  |  ')"
    $gc = if ($tools.GccAlias) { '  (稳定别名)' } else { '' }
    Write-XtInfo "gcc         : $($tools.GccPath)$gc"
    Write-XtInfo "gcc 版本    : $($tools.GccVersion)"
    Write-XtInfo "make        : $($tools.Make)   [$($tools.MakeVersion)]"
    Write-XtInfo "openocd     : $($tools.OpenOcd)   [$($tools.OpenOcdVersion)]"
    Write-XtInfo "ocd scripts : $($tools.OpenOcdScripts)"
    foreach ($m in $tools.Missing) { Write-XtWarn "缺失: $m" }

    $traits = Get-XtStm32Traits -Dir $model.Root
    Write-XtStep "工程: $($model.Root)"
    $kind = @()
    if ($traits.CProject) { $kind += '.cproject' }
    if ($traits.Makefile) { $kind += 'Makefile' }
    if ($traits.Ioc) { $kind += '*.ioc' }
    if ($traits.Ewp) { $kind += '*.ewp' }
    if ($traits.Uvprojx) { $kind += '*.uvprojx' }
    if ($traits.PlatformIo) { $kind += 'platformio.ini' }
    if ($traits.LdFiles.Count -gt 0) { $kind += '*.ld' }
    Write-XtInfo "识别到的构建配置: $(@($kind) -join ', ')"

    foreach ($line in (Get-XtModelSummary -Model $model)) { Write-XtInfo $line }

    if ($model.Refusals.Count -gt 0) {
        Write-XtStep '确定性拒绝'
        foreach ($r in $model.Refusals) {
            Write-XtErr $r.Message
            foreach ($m in $r.Missing) { Write-XtInfo ("缺少: " + $m) }
            if ($r.Next) { Write-XtInfo ("建议: " + $r.Next) }
        }
    }
    foreach ($w in $model.Warnings) { Write-XtWarn $w }

    if ($files = $model.Extra['SourceFiles']) {
        Write-XtInfo "可编译源文件: $($files.C.Count) 个 .c, $($files.S.Count) 个 .s"
    }

    $probeResult = $null
    if ($opt['Probe']) {
        Write-XtStep '探针探测 (openocd init;exit, 每个 8s 超时)'
        if (-not $tools.OpenOcd) { Write-XtErr 'openocd 不可用, 跳过' }
        else {
            # 自检必须带上 target 配置, 否则 stlink.cfg 里没有 transport select,
            # 会报 "session transport was not selected" 而误判成"探针没连上"
            $probeTgt = $null
            if ($opt['OcdTarget']) { $probeTgt = [string]$opt['OcdTarget'] } else { $probeTgt = $model.Extra['OcdTarget'] }
            if (-not $probeTgt) { Write-XtWarn '无法确定 openocd target 配置, 探针自检结论可能不准' }
            foreach ($p in $devices['probes']) {
                $res = Invoke-XtProbeTest -OpenOcd $tools.OpenOcd -Scripts $tools.OpenOcdScripts `
                                          -InterfaceCfg $p['cfg'] -TargetCfg $probeTgt
                if ($res.Ok) {
                    Write-XtOk "$($p['name'])  ($($p['cfg'])) 连上了"
                    Set-XtCachedProbe -Name $p['name'] -InterfaceCfg $p['cfg']
                    Write-XtInfo '已记住该探针, stm32-burn-pj 会直接复用'
                    $probeResult = $p['name']
                    break
                }
                $why = if ($res.TimedOut) { '超时' } else { "退出码 $($res.ExitCode)" }
                Write-XtWarn "$($p['name'])  ($($p['cfg'])) 未连上: $why"
            }
            if (-not $probeResult) {
                Write-XtInfo '没有探针响应。检查 USB / 驱动 (WinUSB) / SWD 接线 / 是否被其他工具占用'
            }
        }
    }

    $code = 0
    if ($model.Refusals.Count -gt 0) { $code = $model.Refusals[0].Code }
    elseif ($tools.Missing.Count -gt 0) { $code = $script:XtExit.ENVIRONMENT }
    return (New-XtResult -Code $code -Message '环境检查完成' -Data @{ Probe = $probeResult })

    # 说明: 上面的 return 在 finally 语义下已经结束函数, 这里不会执行
}

# ----------------------------------------------------------------------------
# 注册 backend —— 多芯片扩展点
# ----------------------------------------------------------------------------
Register-XtBackend @{
    Name    = 'stm32'
    Aliases = @(
        'stm32c0', 'stm32f0', 'stm32f1', 'stm32f2', 'stm32f3', 'stm32f4', 'stm32f7',
        'stm32g0', 'stm32g4', 'stm32h5', 'stm32h7', 'stm32l0', 'stm32l1', 'stm32l4',
        'stm32l5', 'stm32u5', 'stm32wb', 'stm32wl'
    )
    Detect  = { param($dir) Get-XtStm32DetectScore -Dir $dir }
    Extract = { param($dir, $opt) New-XtStm32Model -Root $dir -Opt $opt }
    Init    = { param($ctx) Invoke-XtStm32Init -Ctx $ctx }
    Build   = { param($ctx) Invoke-XtStm32Build -Ctx $ctx }
    Burn    = { param($ctx) Invoke-XtStm32Burn -Ctx $ctx }
    Doctor  = { param($ctx) Invoke-XtStm32Doctor -Ctx $ctx }
}
