# =============================================================================
#  xtcli / core / Model.ps1
#
#  Backend 注册表 + 工程定位 + 归一化工程模型 (ProjectModel)。
#
#  多芯片扩展点就在这里: 加一个芯片 = 写一个 backend 文件 + 调 Register-XtBackend。
#  core 不认识任何芯片, 只认识 Backend 约定的六个动作。
#
#  Backend 约定:
#     Name      : 'stm32'
#     Aliases   : @('stm32f1','stm32f4',...)  可选, 用于 --target 强制指定
#     Detect    : { param($dir) -> 0..100 置信度 }
#     Extract   : { param($dir,$opt) -> ProjectModel }
#     Init      : { param($ctx) -> XtResult }
#     Build     : { param($ctx) -> XtResult }
#     Burn      : { param($ctx) -> XtResult }
#     Doctor    : { param($ctx) -> XtResult }
#
#  ProjectModel 通用字段 (core 可见):
#     Backend Root Target Config Device Family Cpu Fpu Defines[] Includes[]
#     SrcDirs[] AsmSrcs[] LdScript Libs[] LibPaths[] Opt Dbg
#     OutDir BuildDir Source Warnings[] Refusals[] Extra{}
# =============================================================================

$script:XtBackends = @()

function Register-XtBackend {
    param([Parameter(Mandatory)][hashtable]$Backend)
    foreach ($k in @('Name', 'Detect', 'Extract', 'Init', 'Build', 'Burn', 'Doctor')) {
        if (-not $Backend.ContainsKey($k)) { throw "backend 注册失败: 缺少 '$k'" }
    }
    $script:XtBackends = @($script:XtBackends) + @($Backend)
}

function Get-XtBackends { return $script:XtBackends }

function Find-XtBackendByName {
    param([Parameter(Mandatory)][string]$Name)
    foreach ($b in $script:XtBackends) {
        if ($b.Name -eq $Name) { return $b }
        if ($b.ContainsKey('Aliases') -and ($b.Aliases -contains $Name)) { return $b }
    }
    return $null
}

function Resolve-XtBackend {
    <# --target auto 时按 Detect 置信度选; 显式指定时直接用。 #>
    param(
        [string]$Target = 'auto',
        [Parameter(Mandatory)][string]$Dir
    )
    if ($Target -and $Target -ne 'auto') { return (Find-XtBackendByName -Name $Target) }
    $best = $null; $bestScore = 0
    foreach ($b in $script:XtBackends) {
        $score = 0
        try { $score = [int](& $b.Detect $Dir) } catch { $score = 0 }
        if ($score -gt $bestScore) { $bestScore = $score; $best = $b }
    }
    return $best
}

# ----------------------------------------------------------------------------
# 工程定位
# ----------------------------------------------------------------------------
function Resolve-XtProjectDir {
    <# 接受目录或文件路径; 从子目录向上找工程根 (最多 6 层)。 #>
    param([string]$Dir)
    if (-not $Dir) { $Dir = (Get-Location).Path }
    if (-not (Test-Path -LiteralPath $Dir)) { return $null }
    $p = (Resolve-Path -LiteralPath $Dir).Path
    if (-not (Get-Item -LiteralPath $p -Force).PSIsContainer) { $p = Split-Path -Path $p -Parent }

    $markers = @('.cproject', 'Makefile', '*.ioc', '*.ewp', '*.uvprojx', 'CMakeLists.txt', 'platformio.ini')
    $d = Get-Item -LiteralPath $p -Force
    for ($i = 0; $i -lt 6 -and $d; $i++) {
        foreach ($m in $markers) {
            $hit = Get-ChildItem -LiteralPath $d.FullName -Filter $m -File -Force -ErrorAction SilentlyContinue
            if ($hit) { return $d.FullName }
        }
        $d = $d.Parent
    }
    return $p
}

function New-XtModel {
    param(
        [Parameter(Mandatory)][string]$Root,
        [Parameter(Mandatory)][string]$BackendName
    )
    return [ordered]@{
        Backend   = $BackendName
        Root      = $Root
        Target    = (Split-Path -Path $Root -Leaf)
        Config    = 'Debug'
        Device    = $null
        Family    = $null
        Cpu       = $null
        Fpu       = $null
        Defines   = @()
        Includes  = @()
        SrcDirs   = @()
        SrcFiles  = @()    # 显式源文件清单 (.ewp 等 IDE 工程提供); 非空时优先于 SrcDirs 扫描
        AsmSrcs   = @()
        LdScript  = $null
        Libs      = @()
        LibPaths  = @()
        Opt       = '-O0'
        Dbg       = '-g3'
        OutDir    = 'Debug'
        BuildDir  = 'build'
        Source    = $null      # cproject | makefile | ioc | ...
        Warnings  = @()
        Refusals  = @()
        Extra     = @{}
    }
}

function Get-XtModelSummary {
    param([Parameter(Mandatory)]$Model)
    $lines = @()
    $lines += "后端        : $($Model.Backend)"
    $lines += "工程根      : $($Model.Root)"
    $lines += "目标名      : $($Model.Target)"
    $lines += "来源        : $($Model.Source)"
    $lines += "配置        : $($Model.Config)"
    $lines += "芯片        : $($Model.Device)  (系列 $($Model.Family))"
    $lines += "CPU/FPU     : $($Model.Cpu)  $($Model.Fpu)"
    $lines += "优化/调试   : $($Model.Opt) $($Model.Dbg)"
    $lines += "宏定义      : $($Model.Defines -join ' ')"
    $lines += "头文件路径  : $($Model.Includes.Count) 条"
    $lines += "源码目录    : $($Model.SrcDirs -join ' ')"
    if ($Model.SrcFiles.Count -gt 0) {
        $lines += "源文件清单  : $($Model.SrcFiles.Count) 个 .c (来自原 IDE 工程, 精确)"
    }
    $lines += "启动文件    : $($Model.AsmSrcs -join ' ')"
    $lines += "链接脚本    : $($Model.LdScript)"
    $lines += "库          : $(@($Model.LibPaths + $Model.Libs) -join ' ')"
    return $lines
}
