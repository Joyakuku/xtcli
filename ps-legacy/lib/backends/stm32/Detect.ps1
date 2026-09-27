# =============================================================================
#  xtcli / backends / stm32 / Detect.ps1
#
#  STM32 探测器链 + 归一化工程模型组装。
#
#  设计要点: 参数不靠"猜", 靠"读"。工程里已有的构建配置才是权威来源:
#     .cproject (CubeIDE/TrueSTUDIO/SW4STM32) -> 首选, 宏/包含/库/链接脚本全在里面
#     .ioc      (CubeMX)                       -> 兜底, 推导宏并按约定扫目录
#     根 Makefile                              -> A 档, 直接复用, 不转译
#  读不出来时明确拒绝, 而不是生成一个能编但错的固件。
# =============================================================================

# ----------------------------------------------------------------------------
# 工程特征 / 置信度
# ----------------------------------------------------------------------------
function Get-XtStm32Traits {
    param([Parameter(Mandatory)][string]$Dir)

    # 根 Makefile 要区分"用户自己的"和"xtcli 生成的转发壳" —— 否则第二次
    # init 会把自己上一次的产物误判成"工程自带构建系统", 直接跳过初始化,
    # 留下过期的 xtcli/rules.mk。
    $mkPath = Join-Path $Dir 'Makefile'
    $hasMk = Test-Path -LiteralPath $mkPath
    $ownedMk = $false
    if ($hasMk) {
        $head = (Get-Content -LiteralPath $mkPath -TotalCount 12 -ErrorAction SilentlyContinue) -join "`n"
        if ($head -match 'xtcli:generated' -or $head -match 'include\s+xtcli/Makefile') { $ownedMk = $true }
    }

    return [pscustomobject]@{
        Root      = $Dir
        CProject  = [bool](Test-Path -LiteralPath (Join-Path $Dir '.cproject'))
        Makefile  = ($hasMk -and -not $ownedMk)   # 用户自己的构建系统
        MakefileOwned = $ownedMk                  # xtcli 生成的转发壳
        XtSkeleton = [bool](Test-Path -LiteralPath (Join-Path $Dir 'xtcli\Makefile'))
        # IAR/Keil 的工程文件通常在 IDE 子目录里 (EWARM\*.ewp, MDK-ARM\*.uvprojx),
        # 所以必须限深递归找; 目录深度取 3, 够覆盖常见布局又不至于扫全树。
        Ewp       = [bool](Get-ChildItem -LiteralPath $Dir -Recurse -Depth 3 -Filter '*.ewp' -File -Force -ErrorAction SilentlyContinue |
                          Where-Object { $_.FullName -notmatch '[\\/]xtcli[\\/]' })
        Uvprojx   = [bool](Get-ChildItem -LiteralPath $Dir -Recurse -Depth 3 -Filter '*.uvprojx' -File -Force -ErrorAction SilentlyContinue |
                          Where-Object { $_.FullName -notmatch '[\\/]xtcli[\\/]' })
        Ioc       = [bool](Get-ChildItem -LiteralPath $Dir -Filter '*.ioc' -File -Force -ErrorAction SilentlyContinue)
        CoreSrc   = [bool](Test-Path -LiteralPath (Join-Path $Dir 'Core\Src'))
        CoreStart = [bool](Test-Path -LiteralPath (Join-Path $Dir 'Core\Startup'))
        LdFiles   = @(Get-ChildItem -LiteralPath $Dir -Filter '*.ld' -File -Force -ErrorAction SilentlyContinue)
        PlatformIo = [bool](Test-Path -LiteralPath (Join-Path $Dir 'platformio.ini'))
        CMake     = [bool](Test-Path -LiteralPath (Join-Path $Dir 'CMakeLists.txt'))
    }
}

function Get-XtStm32DetectScore {
    param([Parameter(Mandatory)][string]$Dir)
    if (-not (Test-Path -LiteralPath $Dir)) { return 0 }
    $t = Get-XtStm32Traits -Dir $Dir
    $score = 0
    if ($t.CProject) { $score += 60 }
    if ($t.Ioc) { $score += 30 }
    if ($t.CoreSrc) { $score += 25 }
    if ($t.Makefile) { $score += 15 }
    if ($t.LdFiles.Count -gt 0) { $score += 10 }
    if ($t.Ewp -or $t.Uvprojx) { $score += 10 }
    # 只有 STM32 特征才给分, 避免误判其他平台
    if ($score -gt 0 -and ($t.CProject -or $t.Ioc -or $t.CoreSrc)) { return [Math]::Min($score, 100) }
    return 0
}

# ----------------------------------------------------------------------------
# 路径形式归一化
#   CubeIDE 的 .cproject 里混用三种写法, 而且基准目录还不一样:
#     ../Core/Inc                              相对 <工程>/<配置名> (编译器 CWD!)
#     "${workspace_loc:/${ProjName}/App}"      相对工程根
#     ${workspace_loc:/${ProjName}/xxx.ld}      相对工程根
#   CubeIDE 调用编译器时的 CWD 是 <工程>/Debug, 所以 "../Core/Inc" 才等价于
#  <工程>/Core/Inc。用"哪个基准下真的存在"来判定, 比猜前缀可靠。
# ----------------------------------------------------------------------------
function ConvertFrom-XtEclipsePath {
    param(
        [Parameter(Mandatory)][AllowEmptyString()][string]$Raw,
        [Parameter(Mandatory)][string]$Root,
        [string]$ConfigName = 'Debug',
        [ValidateSet('auto', 'root', 'build')][string]$RelativeTo = 'auto'
    )
    $v = $Raw.Trim().Trim('"').Trim()
    if (-not $v) { return $null }

    # 去掉 Eclipse 路径变量前缀
    $v = $v -replace '^\$\{workspace_loc:/\$\{ProjName\}/', ''
    $v = $v -replace '^\$\{workspace_loc:/[^/]+/', ''
    $v = $v -replace '^\$\{ProjName\}/', ''
    $v = $v -replace '^\$\{workspace_loc:', ''
    $v = $v -replace '\}$', ''
    if (-not $v) { return $null }

    if ([System.IO.Path]::IsPathRooted($v)) {
        try { return (Resolve-Path -LiteralPath $v -ErrorAction Stop).Path } catch { return $v }
    }

    $buildBase = Join-Path $Root $ConfigName
    $rootBase = $Root

    if ($RelativeTo -eq 'root') {
        return [System.IO.Path]::GetFullPath((Join-Path $rootBase $v))
    }
    if ($RelativeTo -eq 'build') {
        return [System.IO.Path]::GetFullPath((Join-Path $buildBase $v))
    }

    # auto: 编译器 CWD 优先, 再退回工程根, 取第一个真实存在的
    $c1 = [System.IO.Path]::GetFullPath((Join-Path $buildBase $v))
    $c2 = [System.IO.Path]::GetFullPath((Join-Path $rootBase $v))
    if (Test-Path -LiteralPath $c1) { return $c1 }
    if (Test-Path -LiteralPath $c2) { return $c2 }
    return $c1
}

# ----------------------------------------------------------------------------
# .cproject 解析
# ----------------------------------------------------------------------------
function Read-XtStm32CProject {
    param(
        [Parameter(Mandatory)][string]$Root,
        [string]$ConfigName = 'Debug'
    )
    $path = Join-Path $Root '.cproject'
    $warn = New-Object System.Collections.Generic.List[string]

    try {
        [xml]$xml = Get-Content -LiteralPath $path -Raw -Encoding UTF8
    } catch {
        throw ".cproject 解析失败 (XML 损坏?): $($_.Exception.Message)"
    }

    # --- 选配置: 取带 toolChain + sourceEntries 的 <configuration> 节点 ---
    $cfg = $null
    foreach ($c in $xml.SelectNodes('//configuration')) {
        if ($c.GetAttribute('name') -eq $ConfigName) { $cfg = $c; break }
    }
    if (-not $cfg) {
        foreach ($sm in $xml.SelectNodes('//storageModule')) {
            if ($sm.GetAttribute('name') -eq $ConfigName) {
                $child = $sm.SelectSingleNode('./configuration')
                if ($child) { $cfg = $child; break }
            }
        }
    }
    if (-not $cfg) {
        $names = @()
        foreach ($c in $xml.SelectNodes('//configuration')) {
            $n = $c.GetAttribute('name'); if ($n) { $names += $n }
        }
        throw "配置 '$ConfigName' 在 .cproject 中不存在 (可选: $(@($names | Select-Object -Unique) -join ', '))"
    }

    $cDefines = @(); $asmDefines = @()
    $cIncludes = @(); $asmIncludes = @()
    $libs = @(); $libPaths = @(); $ldScriptRaw = $null
    $device = $null; $optRaw = $null; $dbgRaw = $null; $defaults = $null
    $prebuild = $cfg.GetAttribute('prebuildStep')

    foreach ($o in $cfg.SelectNodes('.//option')) {
        $sc = [string]$o.GetAttribute('superClass')
        if (-not $sc) { continue }
        switch -Regex ($sc) {
            'option\.target_mcu$'                        { $device = $o.GetAttribute('value') }
            'c\.compiler\.option\.definedsymbols$'       { foreach ($v in $o.SelectNodes('./listOptionValue')) { $cDefines += $v.GetAttribute('value') } }
            'assembler\.option\.definedsymbols$'         { foreach ($v in $o.SelectNodes('./listOptionValue')) { $asmDefines += $v.GetAttribute('value') } }
            'c\.compiler\.option\.includepaths$'         { foreach ($v in $o.SelectNodes('./listOptionValue')) { $cIncludes += $v.GetAttribute('value') } }
            'assembler\.option\.includepaths$'           { foreach ($v in $o.SelectNodes('./listOptionValue')) { $asmIncludes += $v.GetAttribute('value') } }
            'c\.linker\.option\.script$'                 { $ldScriptRaw = $o.GetAttribute('value') }
            'c\.linker\.option\.libraries$'              { foreach ($v in $o.SelectNodes('./listOptionValue')) { $libs += $v.GetAttribute('value') } }
            'c\.linker\.option\.directories$'            { foreach ($v in $o.SelectNodes('./listOptionValue')) { $libPaths += $v.GetAttribute('value') } }
            'c\.compiler\.option\.optimization\.level$'  { $optRaw = $o.GetAttribute('value') }
            'c\.compiler\.option\.debuglevel$'           { $dbgRaw = $o.GetAttribute('value') }
            'option\.defaults$'                          { $defaults = $o.GetAttribute('value') }
        }
    }

    # 顺序很关键: 头文件搜索顺序由 C 编译器那份列表决定 (与 CubeIDE 一致),
    # 汇编器独有的项追加在后面。若把汇编器列表排在前面, 同名头文件会解析到
    # 不同副本, 生成代码出现细微差异 (实测 .text 差 16 字节)。
    $defines = @($cDefines + @($asmDefines | Where-Object { $cDefines -notcontains $_ }))
    $includes = @($cIncludes + @($asmIncludes | Where-Object { $cIncludes -notcontains $_ }))

    # --- 兜底: 从 Defaults 那个管道串里补 (部分 CubeMX 版本不写结构化选项) ---
    if ($defaults) {
        $p = $defaults -split '\s*\|\|\s*'
        if (-not $device -and $p.Count -gt 5 -and $p[5]) { $device = $p[5] }
        if ($defines.Count -eq 0 -and $p.Count -gt 13 -and $p[13]) {
            $defines += ($p[13] -split '\s*\|\s*' | Where-Object { $_ })
            $warn.Add("宏定义取自 .cproject 的 Defaults 字段 (结构化选项缺失)")
        }
        if ($includes.Count -eq 0 -and $p.Count -gt 10 -and $p[10]) {
            $includes += ($p[10] -split '\s*\|\s*' | Where-Object { $_ })
        }
        if (-not $ldScriptRaw -and $p.Count -gt 18 -and $p[18]) { $ldScriptRaw = $p[18] }
    }

    # --- sourceEntries -> 源码目录 ---
    $srcDirs = @()
    foreach ($e in $cfg.SelectNodes('./sourceEntries/entry')) {
        if ($e.GetAttribute('kind') -eq 'sourcePath') {
            $n = $e.GetAttribute('name')
            if ($n) { $srcDirs += $n }
        }
    }

    # --- 路径归一化 + 存在性校验 ---
    $incAbs = @(); $incMissing = @()
    foreach ($i in ($includes | Select-Object -Unique)) {
        $abs = ConvertFrom-XtEclipsePath -Raw $i -Root $Root -ConfigName $ConfigName
        if (-not $abs) { continue }
        if (Test-Path -LiteralPath $abs) { $incAbs += $abs }
        else { $incMissing += $i }
    }
    if ($incMissing.Count -gt 0) {
        $warn.Add("头文件路径不存在, 已剔除: $($incMissing -join ' | ')  (CubeMX 重新生成后可能残留, 属于工程内部不一致)")
    }

    $srcAbs = @(); $srcMissing = @()
    foreach ($d in ($srcDirs | Select-Object -Unique)) {
        # sourceEntries 是 Eclipse 资源路径, 一律相对工程根
        $abs = ConvertFrom-XtEclipsePath -Raw $d -Root $Root -RelativeTo 'root'
        if ($abs -and (Test-Path -LiteralPath $abs)) { $srcAbs += $abs } else { $srcMissing += $d }
    }
    if ($srcMissing.Count -gt 0) {
        $warn.Add("sourceEntry 声明的目录不存在: $($srcMissing -join ' | ')")
    }

    $libDirAbs = @()
    foreach ($d in ($libPaths | Select-Object -Unique)) {
        $abs = ConvertFrom-XtEclipsePath -Raw $d -Root $Root -ConfigName $ConfigName
        if ($abs) { $libDirAbs += $abs }
    }

    $ldAbs = $null
    if ($ldScriptRaw) { $ldAbs = ConvertFrom-XtEclipsePath -Raw $ldScriptRaw -Root $Root -ConfigName $ConfigName }

    return [pscustomobject]@{
        Config       = $ConfigName
        Device       = $device
        Defines      = @($defines | Select-Object -Unique)
        AsmDefines   = @($asmDefines | Select-Object -Unique)
        Includes     = @($incAbs | Select-Object -Unique)
        SrcDirs      = @($srcAbs | Select-Object -Unique)
        Libs         = @($libs | Select-Object -Unique)
        LibDirs      = @($libDirAbs | Select-Object -Unique)
        LdScript     = $ldAbs
        Optimize     = $optRaw
        DebugLevel   = $dbgRaw
        PrebuildStep = $prebuild
        Warnings     = $warn.ToArray()
    }
}

# ----------------------------------------------------------------------------
# .ioc 解析 (兜底来源)
# ----------------------------------------------------------------------------
function Read-XtStm32Ioc {
    param([Parameter(Mandatory)][string]$Root)
    $ioc = Get-ChildItem -LiteralPath $Root -Filter '*.ioc' -File -Force -ErrorAction SilentlyContinue | Select-Object -First 1
    if (-not $ioc) { return $null }
    $kv = @{}
    foreach ($line in (Get-Content -LiteralPath $ioc.FullName -Encoding UTF8)) {
        if ($line -notmatch '=' -or $line.TrimStart().StartsWith('#')) { continue }
        $i = $line.IndexOf('=')
        if ($i -lt 1) { continue }
        $kv[$line.Substring(0, $i).Trim()] = $line.Substring($i + 1).Trim()
    }
    $device = $null
    foreach ($k in @('ProjectManager.DeviceId', 'Mcu.UserName', 'Mcu.CPN')) {
        if ($kv.ContainsKey($k) -and $kv[$k]) { $device = $kv[$k]; break }
    }
    return [pscustomobject]@{
        Path          = $ioc.FullName
        ProjectName   = $kv['ProjectManager.ProjectName']
        Device        = $device
        Toolchain     = $kv['ProjectManager.TargetToolchain']
        FreeRTOS      = ($kv.Keys | Where-Object { $_ -like 'Mcu.IP*' -and $kv[$_] -like '*FREERTOS*' }).Count -gt 0
        KeyValues     = $kv
    }
}

# ----------------------------------------------------------------------------
# .ewp 解析 (IAR)
#   价值在于拿到"精确的"宏定义 / 头文件路径 / 源文件清单 —— 比按目录约定推导准。
#   两个刻意不用的东西:
#     * .icf 链接脚本: IAR 私有格式, 无法可靠翻译成 GCC .ld → 用同芯片 .ld 模板
#     * IAR 汇编启动文件: 语法与 GNU as 不兼容 (__iar_program_start 等)
#       → 改用 CMSIS 的 gcc/startup_*.s
#   注意: 编译器不同 (IAR vs GCC), 固件体积/代码布局必然不同。这里保证的是
#   宏定义、头文件路径、源文件集合与原工程一致。
# ----------------------------------------------------------------------------
function Read-XtStm32Ewp {
    param(
        [Parameter(Mandatory)][string]$Root,
        [string]$ConfigName = ''
    )
    $ewp = Get-ChildItem -LiteralPath $Root -Recurse -Filter '*.ewp' -File -Force -ErrorAction SilentlyContinue |
           Where-Object { $_.FullName -notmatch '[\\/]xtcli[\\/]' } | Select-Object -First 1
    if (-not $ewp) { return $null }

    try {
        [xml]$xml = Get-Content -LiteralPath $ewp.FullName -Raw -Encoding UTF8
    } catch {
        throw ".ewp 解析失败 (XML 损坏?): $($_.Exception.Message)"
    }
    $projDir = $ewp.DirectoryName
    $warn = New-Object System.Collections.Generic.List[string]

    # --- 选配置 (IAR 的配置名在 <configuration><name> 里) ---
    $cfg = $null
    foreach ($c in $xml.SelectNodes('//configuration')) {
        $n = $c.SelectSingleNode('./name')
        $nm = if ($n) { $n.InnerText.Trim() } else { '' }
        if (-not $cfg) { $cfg = $c }
        if ($ConfigName -and $nm -eq $ConfigName) { $cfg = $c; break }
    }
    if (-not $cfg) { throw '未找到 <configuration> 节点' }

    # --- 选项字典: "settingsName/optionName" -> state 数组 ---
    $optMap = @{}
    foreach ($s in $cfg.SelectNodes('./settings')) {
        $sn = $s.SelectSingleNode('./name')
        if (-not $sn) { continue }
        $sname = $sn.InnerText.Trim()
        foreach ($o in $s.SelectNodes('./data/option')) {
            $on = $o.SelectSingleNode('./name')
            if (-not $on) { continue }
            $optMap["$sname/$($on.InnerText.Trim())"] = @($o.SelectNodes('./state') | ForEach-Object { $_.InnerText })
        }
    }
    $getOpt = {
        param($s, $n)
        $k = "$s/$n"
        if ($optMap.ContainsKey($k)) { return $optMap[$k] }
        return @()
    }

    # --- 芯片: OGChipSelectEditMenu 形如 "STM32F103RC<TAB>ST STM32F103RC" ---
    # 必须用 @() 包住: scriptblock 里 return 单元素数组会被 PowerShell 解包成标量,
    # 那样 $chip[0] 就变成字符串首字符。
    $device = $null
    $chip = @(& $getOpt 'General' 'OGChipSelectEditMenu')
    if ($chip.Count -gt 0 -and $chip[0]) { $device = ($chip[0] -split '\s+')[0].Trim() }

    # --- IAR 路径变量替换 ---
    $subst = {
        param([string]$Raw)
        $v = $Raw.Trim()
        if (-not $v) { return $null }
        if ($v -match '\$TOOLKIT_DIR\$|\$EW_DIR\$|\$CMSIS_PACK_ROOT\$') { return $null }  # 工具链自带, GCC 用不上
        $v = $v -replace '\$PROJ_DIR\$', $projDir
        try { return [System.IO.Path]::GetFullPath($v) } catch { return $null }
    }

    # --- 宏定义 / 头文件路径 (ICCARM = C 编译器块) ---
    $defines = @(& $getOpt 'ICCARM' 'CCDefines' | Where-Object { $_ })
    $incRaw = @(& $getOpt 'ICCARM' 'CCIncludePath2' | Where-Object { $_ })
    $incAbs = @(); $incMissing = @()
    foreach ($i in $incRaw) {
        $abs = & $subst $i
        if (-not $abs) { continue }
        if (Test-Path -LiteralPath $abs) { $incAbs += $abs } else { $incMissing += $i }
    }
    if ($incMissing.Count -gt 0) {
        $warn.Add("头文件路径不存在, 已剔除: $($incMissing -join ' | ')")
    }

    # --- 源文件清单 ---
    $cFiles = @(); $skippedAsm = @(); $missingSrc = @()
    foreach ($fn in $xml.SelectNodes('//file/name')) {
        $p = & $subst $fn.InnerText
        if (-not $p) { continue }
        if ($p -match '\.(c|C)$') {
            if (Test-Path -LiteralPath $p) { $cFiles += $p } else { $missingSrc += (Split-Path -Path $p -Leaf) }
        }
        elseif ($p -match '\.(s|S|asm)$') {
            $skippedAsm += (Split-Path -Path $p -Leaf)
        }
    }
    if ($skippedAsm.Count -gt 0) {
        $warn.Add("已跳过 IAR 汇编文件 (语法与 GNU as 不兼容), 启动文件改用 CMSIS 的 gcc 版本: $($skippedAsm -join ', ')")
    }
    if ($missingSrc.Count -gt 0) {
        $warn.Add("清单里的源文件不存在, 已跳过: $($missingSrc -join ', ')")
    }

    # .icf 链接脚本: 明确告知不能用
    $icf = @(& $getOpt 'ILINK' 'IlinkIcfFile' | Where-Object { $_ })
    if ($icf.Count -eq 0) { $icf = @(& $getOpt 'ILINK' 'IlinkConfigDefines' | Where-Object { $_ -match '\.icf$' }) }
    if ($icf.Count -gt 0) {
        $icfAbs = & $subst $icf[0]
        if (-not $icfAbs) { $icfAbs = $icf[0] }
        $warn.Add("IAR 链接脚本 (.icf) 无法翻译成 GCC .ld, 已改用同芯片的 .ld: $(Split-Path -Path $icfAbs -Leaf)")
    }

    return [pscustomobject]@{
        Source   = 'ewp'
        Config   = $ConfigName
        Device   = $device
        Defines  = @($defines | Select-Object -Unique)
        Includes = @($incAbs | Select-Object -Unique)
        SrcFiles = @($cFiles | Select-Object -Unique)
        Libs     = @()
        LibDirs  = @()
        LdScript = $null
        Warnings = $warn.ToArray()
    }
}

# ----------------------------------------------------------------------------
# 芯片 -> 系列 / CPU / FPU / 定义 / openocd target
# ----------------------------------------------------------------------------
function Get-XtStm32Family {
    param([string]$Device)
    if (-not $Device) { return $null }
    if ($Device -match '^(STM32[A-Z]\d)') { return $Matches[1] }
    if ($Device -match '^(STM32[A-Z]{2})') { return $Matches[1] }
    return $null
}

function Get-XtStm32DensityDefine {
    <# 由型号推导 HAL 密度宏。仅在配置里没有现成宏时使用。
       STM32F103RCTx -> 系列段 STM32F103, 容量码 C -> STM32F103xE (F1 的 high density) #>
    param(
        [string]$Device = '',
        [string]$Family = '',
        $Devices
    )
    if (-not $Device) { return $null }
    if ($Device -match '^STM32([A-Z])(\d+)([A-Z])([A-Z])([A-Z])') {
        $series   = "STM32$($Matches[1])$($Matches[2])"
        $flashCode = $Matches[4]
        if ($Devices -and $Devices['density'] -and $Devices['density'][$Family]) {
            $tbl = $Devices['density'][$Family]
            if ($tbl.ContainsKey($flashCode)) { return "$series$($tbl[$flashCode])" }
        }
        # 其他系列 HAL 的约定是 <型号>xx
        return "${series}xx"
    }
    return $null
}

function Get-XtStm32DeviceInfo {
    param(
        [string]$Device = '',
        $Devices
    )
    $family = Get-XtStm32Family -Device $Device
    $cpu = $null; $fpu = $null; $ocd = $null
    if ($family -and $Devices['families'] -and $Devices['families'][$family]) {
        $f = $Devices['families'][$family]
        $cpu = $f['cpu']; $fpu = $f['fpu']; $ocd = $f['ocd']
    }
    return [pscustomobject]@{ Family = $family; Cpu = $cpu; Fpu = $fpu; OcdTarget = $ocd }
}

# ----------------------------------------------------------------------------
# 源码文件收集 (与 rules.mk 的排除规则保持一致)
#   厂商模板目录必须排除, 否则会把全型号 startup 和重复的 system_*.c 编进来。
# ----------------------------------------------------------------------------
$script:XtStm32ExcludeSegments = @('Templates', 'Template', 'Core_A')

function Test-XtStm32Excluded {
    param([Parameter(Mandatory)][string]$FullPath)
    foreach ($seg in $script:XtStm32ExcludeSegments) {
        if ($FullPath -match "[\\/]$seg[\\/]") { return $true }
    }
    # ST 的 HAL 模板文件 (与 rules.mk 的 XT_EXCLUDE 保持一致)
    $name = Split-Path -Path $FullPath -Leaf
    if ($name -match '_template\.(c|s)$') { return $true }
    return $false
}

function Get-XtStm32SourceFiles {
    param([Parameter(Mandatory)]$Model)
    # 显式清单优先 (.ewp 等 IDE 工程给出的是"精确的"文件集合,
    # 比按目录扫描更贴近原工程, 也避免把 _template.c 之类收进来)
    if ($Model.SrcFiles -and $Model.SrcFiles.Count -gt 0) {
        return [pscustomobject]@{ C = @($Model.SrcFiles); S = @($Model.AsmSrcs) }
    }
    $c = @(); $s = @()
    foreach ($d in $Model.SrcDirs) {
        if (-not (Test-Path -LiteralPath $d)) { continue }
        foreach ($f in (Get-ChildItem -LiteralPath $d -Recurse -File -Include '*.c', '*.s' -Force -ErrorAction SilentlyContinue)) {
            if (Test-XtStm32Excluded -FullPath $f.FullName) { continue }
            if ($f.Extension -eq '.c') { $c += $f.FullName } else { $s += $f.FullName }
        }
    }
    return [pscustomobject]@{ C = @($c | Sort-Object -Unique); S = @($s | Sort-Object -Unique) }
}

function Find-XtStm32Startup {
    <# 启动文件优先级:
       1) 工程自己的 Core/Startup/*.s
       2) CMSIS 包里的 Templates/gcc/startup_<密度宏小写>.s  (EWARM-only 工程走这条)
       3) 名字里带同系列型号的任意 startup_*.s (最后兜底) #>
    param(
        [Parameter(Mandatory)][string]$Root,
        [string]$DensityDefine
    )
    $own = Join-Path $Root 'Core\Startup'
    if (Test-Path -LiteralPath $own) {
        $f = Get-ChildItem -LiteralPath $own -File -Filter '*.s' -Force -ErrorAction SilentlyContinue | Select-Object -First 1
        if ($f) { return $f.FullName }
    }
    if ($DensityDefine) {
        $want = 'startup_' + $DensityDefine.ToLowerInvariant() + '.s'
        $hits = Get-ChildItem -LiteralPath $Root -Recurse -File -Filter $want -Force -ErrorAction SilentlyContinue
        if ($hits) {
            $gcc = $hits | Where-Object { $_.FullName -match '[\\/]gcc[\\/]' } | Select-Object -First 1
            if ($gcc) { return $gcc.FullName }
            return $hits[0].FullName
        }
    }
    return $null
}

function Resolve-XtStm32LdScript {
    <# 链接脚本优先级:
       1) 构建配置里声明的
       2) 工程根下的 *.ld
       3) xtcli 模板库 templates/stm32/ld/<device>_FLASH.ld (同芯片工程之间复用) #>
    param(
        [Parameter(Mandatory)][string]$Root,
        [string]$Declared,
        [string]$Device
    )
    if ($Declared -and (Test-Path -LiteralPath $Declared)) { return $Declared }
    $own = Get-ChildItem -LiteralPath $Root -Filter '*.ld' -File -Force -ErrorAction SilentlyContinue
    if ($own) { return $own[0].FullName }
    if ($Device) {
        foreach ($cand in @("$Device`_FLASH.ld", "$Device.ld")) {
            $tpl = Join-Path (Join-Path (Get-XtTemplateDir) 'stm32\ld') $cand
            if (Test-Path -LiteralPath $tpl) { return $tpl }
        }
    }
    return $null
}

# ----------------------------------------------------------------------------
# 模型组装
# ----------------------------------------------------------------------------
function New-XtStm32Model {
    param(
        [Parameter(Mandatory)][string]$Root,
        [hashtable]$Opt = @{}
    )
    $configName = 'Debug'
    if ($Opt.ContainsKey('Config') -and $Opt['Config']) { $configName = [string]$Opt['Config'] }

    $devices = Get-XtDevices
    $traits = Get-XtStm32Traits -Dir $Root
    $model = New-XtModel -Root $Root -BackendName 'stm32'
    $model.Config = $configName
    $warnings = New-Object System.Collections.Generic.List[string]
    $refusals = New-Object System.Collections.Generic.List[object]

    # ---------- 1) 来源判定 ----------
    $cp = $null
    if ($traits.CProject) {
        try {
            $cp = Read-XtStm32CProject -Root $Root -ConfigName $configName
            $model.Source = 'cproject'
        } catch {
            $warnings.Add(".cproject 不可用: $($_.Exception.Message)")
        }
    }

    # .ewp (IAR) —— 只在没有 .cproject 时读, 用来拿精确的宏/包含/文件清单
    $ewp = $null
    if (-not $cp -and $traits.Ewp) {
        try {
            $ewp = Read-XtStm32Ewp -Root $Root -ConfigName $configName
        } catch {
            $warnings.Add(".ewp 不可用: $($_.Exception.Message)")
        }
    }

    $ioc = $null
    if ($traits.Ioc) { $ioc = Read-XtStm32Ioc -Root $Root }

    # ---------- 2) 来源选择 + 确定性拒绝 ----------
    #  原则: 能用可用路径就用, 只在确实无路可走时才拒绝。
    #  优先级: .cproject (参数最全, 与 CubeIDE 逐项对齐)
    #        > 根 Makefile (直接复用, 不转译)
    #        > .ioc + 目录约定 (IAR/Keil/纯 CubeMX 的兜底 —— 能编就先让它能编)
    #  拒绝只留给"真的建不起来"的输入, 不留给"来源不理想"的输入。
    if (-not $cp -and -not $ewp -and -not $traits.Makefile) {
        if ($traits.CoreSrc -and $ioc) {
            if (-not $ewp -and ($traits.Ewp -or $traits.Uvprojx)) {
                $warnings.Add('该工程是 IAR/Keil 工具链; 参数由 .ioc + 目录约定推导 (未读取 .ewp/.uvprojx), 可能与原 IDE 构建有差异 —— 建议用 xtcli-doctor 核对宏定义, 或核对 size')
            }
            if ($traits.PlatformIo) {
                $warnings.Add('检测到 platformio.ini; 本工具不使用 PlatformIO, 直接走 arm-none-eabi 构建')
            }
        }
        elseif ($traits.CoreSrc -and -not $ioc) {
            $refusals.Add((New-XtRefusal -Reason '有源码但缺芯片信息: 找不到 .ioc 也找不到 .cproject' `
                        -Missing @('*.ioc', '.cproject') `
                        -Next '用 CubeMX 打开工程并保存一次(会生成 .ioc), 或恢复 .cproject/Makefile'))
        }
        elseif ($ioc -and -not $traits.CoreSrc) {
            $refusals.Add((New-XtRefusal -Reason 'CubeMX 尚未生成代码 (缺 Core/Src)' `
                        -Missing @('Core/Src/', 'Core/Inc/', 'Drivers/') `
                        -Next "先用 CubeMX 打开 $($ioc.Path) 生成代码, 再运行 stm32-init-pj"))
        }
        else {
            $refusals.Add((New-XtRefusal -Reason '不是 STM32 工程: 找不到 .cproject / Makefile / .ioc / Core/Src' `
                        -Missing @('.cproject', 'Makefile', '*.ioc', 'Core/Src')))
        }
    }

    # ---------- 3) 填充模型 ----------
    if ($cp) {
        $model.Device = $cp.Device
        $model.Defines = $cp.Defines
        $model.Includes = $cp.Includes
        $model.SrcDirs = $cp.SrcDirs
        $model.Libs = $cp.Libs            # -l 项, 例如 :lcd.a
        $model.LibPaths = $cp.LibDirs     # -L 项
        $model.Extra['PrebuildStep'] = $cp.PrebuildStep
        foreach ($w in $cp.Warnings) { $warnings.Add($w) }

        # 优化/调试等级: CubeIDE 用枚举值, 映射成 gcc 旗标
        switch -Regex ([string]$cp.Optimize) {
            'value\.o0$'   { $model.Opt = '-O0' }
            'value\.og$'   { $model.Opt = '-Og' }
            'value\.os$'   { $model.Opt = '-Os' }
            'value\.o2$'   { $model.Opt = '-O2' }
            'value\.o3$'   { $model.Opt = '-O3' }
            default        { $model.Opt = '-O0' }
        }
        switch -Regex ([string]$cp.DebugLevel) {
            'value\.g0$'    { $model.Dbg = '-g0' }
            'value\.g1$'    { $model.Dbg = '-g1' }
            'value\.g2$'    { $model.Dbg = '-g2' }
            'value\.g3$'    { $model.Dbg = '-g3' }
            'value\.gnone$' { $model.Dbg = '' }
            default         { $model.Dbg = '-g3' }
        }
    }
    elseif ($ewp) {
        # IAR 工程: 宏/包含/源文件清单取 .ewp 精确值; 优化等级不映射
        # (IAR 与 GCC 的 -O 语义不可比), 保持 GCC 的 Debug 默认 -O0 -g3。
        $model.Source = 'ewp'
        $model.Device = $ewp.Device
        $model.Defines = $ewp.Defines
        $model.Includes = $ewp.Includes
        $model.SrcFiles = $ewp.SrcFiles
        $model.SrcDirs = @()
        # GCC 运行时桩: 原 IDE (IAR/Keil) 用自己的运行库, 清单里不会有这两个文件。
        # init 会把它们补进 Core/Src, 这里再把"已存在的"并进源文件清单 —— 否则
        # 用了 printf/malloc 的工程会链接失败 (undefined _write / _sbrk)。
        # 未被引用时 --gc-sections 会自动丢掉, 所以并入是无害的。
        foreach ($stub in @('syscalls.c', 'sysmem.c')) {
            $sp = Join-Path $Root "Core\Src\$stub"
            if ((Test-Path -LiteralPath $sp) -and ($model.SrcFiles -notcontains $sp)) {
                $model.SrcFiles = @($model.SrcFiles) + @($sp)
            }
        }
        foreach ($w in $ewp.Warnings) { $warnings.Add($w) }
        $warnings.Add('原工程用 IAR 编译器; 本构建用 GCC, 固件体积/代码布局必然不同。此处保证的是宏定义、头文件路径、源文件集合与原工程一致')
    }
    elseif ($traits.Makefile) {
        # A 档: 直接用工程自己的 Makefile, 不转译。模型只做信息展示。
        $model.Source = 'makefile'
        if ($ioc) { $model.Device = $ioc.Device }
    }
    elseif ($ioc -and $traits.CoreSrc) {
        # 兜底: 从 .ioc + 目录约定推导
        $model.Source = 'ioc'
        $model.Device = $ioc.Device
        $warnings.Add('未找到 .cproject, 参数由 .ioc + 目录约定推导 —— 建议核对 size 是否与参考构建一致')
        $d = Get-XtStm32DensityDefine -Device $ioc.Device -Family (Get-XtStm32Family -Device $ioc.Device) -Devices $devices
        $model.Defines = @(@('USE_HAL_DRIVER') + @($d | Where-Object { $_ }))
        # 源码目录: 不写死系列名, 扫 Drivers/<系列>_HAL_Driver/Src
        $srcCandidates = @(
            (Join-Path $Root 'Core\Src'),
            (Join-Path $Root 'FreeRTOS\Src'),
            (Join-Path $Root 'App')
        )
        $drvRoot = Join-Path $Root 'Drivers'
        if (Test-Path -LiteralPath $drvRoot) {
            foreach ($sd in (Get-ChildItem -LiteralPath $drvRoot -Directory -ErrorAction SilentlyContinue)) {
                $srcCandidates += (Join-Path $sd.FullName 'Src')
            }
        }
        $model.SrcDirs = @($srcCandidates | Where-Object { Test-Path -LiteralPath $_ })
        # 包含路径: 扫描工程内含 .h 的目录 (排除厂商模板), 比手工列更稳
        $incDirs = @()
        foreach ($base in @('Core', 'Drivers', 'FreeRTOS', 'App', 'Middlewares')) {
            $b = Join-Path $Root $base
            if (-not (Test-Path -LiteralPath $b)) { continue }
            foreach ($h in (Get-ChildItem -LiteralPath $b -Recurse -File -Filter '*.h' -Force -ErrorAction SilentlyContinue)) {
                if (Test-XtStm32Excluded -FullPath $h.FullName) { continue }
                $incDirs += $h.DirectoryName
            }
        }
        $model.Includes = @($incDirs | Sort-Object -Unique)
    }

    if (-not $model.Device -and $ioc) { $model.Device = $ioc.Device }

    # 已经有拒绝结论就不再往下推导。芯片信息缺失时, 下面的设备表查询会抛
    # 参数绑定异常, 那会把真正的拒绝理由盖掉 —— 看起来像"解析失败",
    # 而不是"确定性拒绝 + 缺什么"。契约要求后者。
    if ($refusals.Count -gt 0) {
        $model.Warnings = $warnings.ToArray()
        $model.Refusals = $refusals.ToArray()
        return $model
    }

    # ---------- 4) 芯片参数 ----------
    $info = Get-XtStm32DeviceInfo -Device $model.Device -Devices $devices
    $model.Family = $info.Family
    $model.Cpu = $info.Cpu
    $model.Fpu = $info.Fpu
    $model.Extra['OcdTarget'] = $info.OcdTarget
    $model.Extra['OpenOcdIf'] = 'interface/stlink.cfg'
    if ($model.Device -and -not $info.Cpu) {
        $warnings.Add("设备表里没有系列 '$($info.Family)', CPU/FPU 无法确定 (请在 data/stm32-devices.json 补一条)")
    }

    # 密度宏: 配置里有就信配置, 没有才推导
    if ($model.Device) {
        $hasDensity = $false
        foreach ($d in $model.Defines) { if ($d -match '^STM32[A-Z]\d+x[A-Z]$') { $hasDensity = $true } }
        if (-not $hasDensity) {
            $derived = Get-XtStm32DensityDefine -Device $model.Device -Family $model.Family -Devices $devices
            if ($derived) {
                $model.Defines = @($model.Defines + $derived)
                $warnings.Add("密度宏由型号推导: $derived")
            }
        }
    }

    # ---------- 5) 启动文件 / 链接脚本 / 运行时桩 ----------
    if ($model.Source -ne 'makefile') {
        $density = $null
        foreach ($d in $model.Defines) { if ($d -match '^STM32[A-Z]\d+x[A-Z]$') { $density = $d } }

        $startup = Find-XtStm32Startup -Root $Root -DensityDefine $density
        if ($startup) {
            $model.AsmSrcs = @($startup)
        } else {
            $warnings.Add('找不到启动文件 (Core/Startup/*.s 或 CMSIS Templates/gcc/startup_*.s) —— 链接会缺 Reset_Handler')
        }

        $ld = Resolve-XtStm32LdScript -Root $Root -Declared $cp.LdScript -Device $model.Device
        if ($ld) {
            $model.LdScript = $ld
            if (-not (Test-XtWithin -Child $ld -Parent $Root)) {
                $warnings.Add("链接脚本来自 xtcli 模板库 (工程内没有 .ld): $ld —— 已复制进工程")
            }
        } else {
            $refusals.Add((New-XtRefusal -Reason '找不到链接脚本 (.ld), 且模板库里没有该芯片的可用脚本' `
                        -Missing @('*.ld', "$($model.Device)_FLASH.ld") `
                        -Next '用 CubeMX 生成 CubeIDE/Makefile 工具链工程会自带 .ld; 或手工放入一个'))
        }
        $model.Extra['NeedsSyscalls'] = -not (Test-Path -LiteralPath (Join-Path $Root 'Core\Src\syscalls.c'))
    }

    # ---------- 6) 源码非空校验 ----------
    if ($model.Source -ne 'makefile') {
        $files = Get-XtStm32SourceFiles -Model $model
        $model.Extra['SourceFiles'] = $files
        if ($files.C.Count -eq 0) {
            $refusals.Add((New-XtRefusal -Reason '没有任何可编译的 .c 源文件' `
                        -Missing @("在 $($model.SrcDirs -join ', ') 下未找到 .c") `
                        -Next '确认工程未损坏, 或 .cproject 的 sourceEntries 是否正确'))
        }
    }

    # ---------- 7) 路径空格告警 (make 无法处理) ----------
    foreach ($p in @($model.SrcDirs) + @($model.Includes)) {
        if ($p -match '\s') { $warnings.Add("路径含空格, make 无法安全处理: $p"); break }
    }

    $model.Warnings = $warnings.ToArray()
    $model.Refusals = $refusals.ToArray()
    return $model
}
