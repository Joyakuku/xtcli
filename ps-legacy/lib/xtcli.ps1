# =============================================================================
#  xtcli —— 嵌入式工程 CLI 入口
#
#  bin\ 下的 shim 都调到这里:
#     stm32-init-pj / stm32-build-pj / stm32-burn-pj   = --target stm32
#     pj-init / pj-build / pj-burn                     = --target auto (自动探测芯片)
#     xtcli-doctor                                     = doctor
#
#  core 不含任何芯片知识; 芯片能力全部来自 lib\backends\<name>\ 下的注册。
# =============================================================================

[CmdletBinding()]
param(
    [Parameter(Position = 0)][string]$Verb = 'help',

    # 不要"工程目录"位置参数 —— 每个动作都作用于当前目录。
    # 多余的位置参数会被下面的 $ExtraArgs 接住, 给出明确提示,
    # 而不是抛出 PowerShell 默认的参数绑定错误。
    [Parameter(ValueFromRemainingArguments = $true)][string[]]$ExtraArgs = @(),

    [string]$Target = 'auto',        # auto | stm32 | stm32f4 ...
    [string]$Config = 'Debug',       # 构建配置名 (Debug / Release)
    [string]$Interface = '',        # 空 = 自动识别探针 (stlink -> cmsis-dap -> jlink)
    [string]$OcdTarget = '',
    [string]$ProbeSerial = '',
    [string]$Address = '',
    [string]$Elf = '',
    [string]$MakeTarget = '',

    [switch]$Bin,                    # 烧 .bin 而不是 .elf
    [switch]$Clean,
    [switch]$NoBuild,
    [switch]$Force,
    [switch]$Probe,                  # doctor: 逐个试 openocd 探针
    [switch]$Refresh,                # 重新探测工具版本
    [switch]$Quiet
)

$ErrorActionPreference = 'Stop'

# 先固定 lib 目录 —— dot-source 会改写 $PSScriptRoot
$XtLib = $PSScriptRoot

. "$XtLib\core\Bootstrap.ps1"
. "$XtLib\core\Tools.ps1"
. "$XtLib\core\Model.ps1"

# 加载全部 backend (自动注册)。加新芯片只需在这里放一个目录。
if (Test-Path -LiteralPath "$XtLib\backends") {
    foreach ($d in (Get-ChildItem -LiteralPath "$XtLib\backends" -Directory)) {
        foreach ($f in (Get-ChildItem -LiteralPath $d.FullName -Filter '*.ps1' -File | Sort-Object Name)) {
            . $f.FullName
        }
    }
}

function Show-XtHelp {
    Write-Host ''
    Write-Host 'xtcli —— 嵌入式工程 CLI' -ForegroundColor Cyan
    Write-Host ''
    Write-Host '用法:'
    Write-Host '  stm32-init-pj  [选项]    初始化直到可编译'
    Write-Host '  stm32-build-pj [选项]    构建'
    Write-Host '  stm32-burn-pj  [选项]    烧录'
    Write-Host '  xtcli-doctor   [选项]    体检环境与工程, 并给出确定性结论'
    Write-Host '  pj-<动词>                同上, 但自动探测芯片 (--target auto)'
    Write-Host ''
    Write-Host '全部命令作用于【当前目录】, 并会自动向上查找工程根'
    Write-Host '(.cproject / Makefile / *.ioc / *.ewp / *.uvprojx / CMakeLists.txt)。'
    Write-Host '所以先 cd 到工程目录 (或它的子目录) 再运行。'
    Write-Host ''
    Write-Host '用于 build/init:'
    Write-Host '  -Config <名>       构建配置, 默认 Debug'
    Write-Host '  -Clean             先清理'
    Write-Host '  -NoBuild           init 时不试编译 / burn 时不自动构建'
    Write-Host "  -MakeTarget <t>    传给 make 的目标 (size / disasm / all ...)"
    Write-Host ''
    Write-Host '用于 burn:'
    Write-Host '  -Interface <cfg>   强制指定 openocd 探针配置 (默认自动识别 stlink/cmsis-dap/jlink)'
    Write-Host '  -OcdTarget <cfg>   覆盖 openocd target 配置'
    Write-Host '  -ProbeSerial <sn>  指定探针序列号 (多探针时)'
    Write-Host '  -Bin               烧 .bin (需配合 -Address)'
    Write-Host '  -Address <addr>    bin 的烧录基址, 默认 0x08000000'
    Write-Host '  -Elf <路径>        指定要烧的固件'
    Write-Host ''
    Write-Host '通用:'
    Write-Host '  -Target <名>       auto(默认) | stm32 | stm32f4 ...'
    Write-Host '  -Probe             doctor 时逐个试探针 (带超时)'
    Write-Host '  -Refresh           重新探测工具链版本'
    Write-Host '  -Quiet             安静模式'
    Write-Host ''
    Write-Host '退出码: 0 成功 | 2 参数 | 3 环境 | 4 工程 | 5 不支持 | 6 构建 | 7 烧录'
    Write-Host ''
}

    $exit = $script:XtExit

try {
    if ($Verb -in @('help', '-h', '--help', '/?')) { Show-XtHelp; exit $exit.OK }
    if ($Verb -in @('version', '-v', '--version')) {
        Write-Host "xtcli $($script:XtCliVersion)"
        exit $exit.OK
    }

    if ($ExtraArgs.Count -gt 0) {
        Write-XtErr "无法识别或不支持的参数: $($ExtraArgs -join ' ')"
        Write-XtInfo '本命令不接受"工程目录"参数 —— 始终作用于当前目录, 并自动向上查找工程根'
        Write-XtInfo '用法: cd 到工程目录, 然后运行 stm32-init-pj / stm32-build-pj / stm32-burn-pj'
        Write-XtInfo '查看全部选项: xtcli help'
        exit $exit.USAGE
    }

    $verbMap = @{ 'init' = 'Init'; 'build' = 'Build'; 'burn' = 'Burn'; 'doctor' = 'Doctor' }
    if (-not $verbMap.ContainsKey($Verb)) {
        Write-XtErr "未知动作: $Verb"
        Show-XtHelp
        exit $exit.USAGE
    }
    $action = $verbMap[$Verb]

    if ($Quiet) { Set-XtQuiet $true }

    if ($Verb -in @('init', 'build')) { Write-XtInfo "xtcli $($script:XtCliVersion) / $Verb" }

    # --- 工程定位: 当前目录, 并自动向上查找工程根 ---
    $root = Resolve-XtProjectDir
    if (-not $root) {
        Write-XtErr "当前目录无效: $((Get-Location).Path)"
        exit $exit.PROJECT
    }

    # --- backend 选择 ---
    $backend = Resolve-XtBackend -Target $Target -Dir $root
    if (-not $backend) {
        Write-XtErr "没有 backend 能处理该目录 (--target $Target)"
        Write-XtInfo "目录: $root"
        $known = @(Get-XtBackends | ForEach-Object { $_.Name })
        Write-XtInfo "已注册 backend: $($known -join ', ')"
        exit $exit.UNSUPPORTED
    }
    if ($Verb -eq 'doctor') { Write-XtInfo "backend: $($backend.Name)" }

    # --- 环境 ---
    $tools = Get-XtToolchain -Refresh:$Refresh

    # --- 建模 (makefile 来源的工程也先建模, 用于展示与定位固件) ---
    $opt = @{
        Config = $Config; Interface = $Interface; OcdTarget = $OcdTarget
        ProbeSerial = $ProbeSerial; Address = $Address; Elf = $Elf
        Bin = [bool]$Bin; Clean = [bool]$Clean; NoBuild = [bool]$NoBuild
        Force = [bool]$Force; Probe = [bool]$Probe; Target = $MakeTarget
    }

    $model = $null
    try {
        $model = & $backend['Extract'] $root $opt
    } catch {
        Write-XtErr "工程解析失败: $($_.Exception.Message)"
        exit $exit.PROJECT
    }

    if ($Verb -in @('build', 'burn') -and $tools.Missing.Count -gt 0) {
        foreach ($m in $tools.Missing) { Write-XtErr "环境缺失: $m" }
        Write-XtInfo '提示: 工具搜索根可用 XTCLI_ROOTS 环境变量或 xtcli.json 的 roots 覆盖'
        exit $exit.ENVIRONMENT
    }

    $ctx = @{
        Root    = $root
        Opt     = $opt
        Tools   = $tools
        Model   = $model
        Backend = $backend
    }

    $fn = $backend[$action]
    $result = & $fn $ctx
    if ($null -eq $result) { $result = New-XtResult -Code 0 }
    exit ([int]$result.Code)
}
catch {
    Write-XtErr "未预期错误: $($_.Exception.Message)"
    if ($_.ScriptStackTrace) { Write-XtInfo $_.ScriptStackTrace }
    exit $exit.FAILURE
}
