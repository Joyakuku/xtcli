# =============================================================================
#  xtcli / core / Bootstrap.ps1
#
#  基础环境: 路径常量、日志、退出码契约、结果对象、路径工具。
#  这一层完全不含芯片知识 —— 加新芯片不需要动这里。
# =============================================================================

Set-StrictMode -Version 1.0

$script:XtCliVersion = '0.1.0'

# ----------------------------------------------------------------------------
# 退出码契约 (对外承诺, 脚本化调用 stm32-build-pj && stm32-burn-pj 依赖它)
# ----------------------------------------------------------------------------
$script:XtExit = [ordered]@{
    OK          = 0   # 成功
    FAILURE     = 1   # 未分类失败
    USAGE       = 2   # 参数错误
    ENVIRONMENT = 3   # 工具链/openocd 等环境缺失
    PROJECT     = 4   # 不是工程, 或工程已被破坏
    UNSUPPORTED = 5   # 确定性拒绝: 本工具暂不支持该来源/形态
    BUILD       = 6   # 构建失败
    BURN        = 7   # 烧录失败
}

# ----------------------------------------------------------------------------
# 路径
#
#  注意: 必须在 dot-source 的"当时"就把 home 定下来。
#  $PSScriptRoot 是自动变量, 会被后续 dot-source 的文件改写 (最后被 source 的
#  文件目录胜出), 所以不能在函数里延迟读取它。
# ----------------------------------------------------------------------------
$script:XtHomeResolved = $null
$__xtBootstrapDir = $PSScriptRoot
if (-not $__xtBootstrapDir) { $__xtBootstrapDir = Split-Path -Parent $MyInvocation.MyCommand.Path }
$script:XtHomeResolved = (Resolve-Path -LiteralPath (Join-Path $__xtBootstrapDir '..\..')).Path
if ($env:XTCLI_HOME -and (Test-Path -LiteralPath $env:XTCLI_HOME)) {
    $script:XtHomeResolved = (Resolve-Path -LiteralPath $env:XTCLI_HOME).Path
}

function Get-XtHome { return $script:XtHomeResolved }

function Get-XtConfigPath { Join-Path (Get-XtHome) 'xtcli.json' }
function Get-XtTemplateDir { Join-Path (Get-XtHome) 'templates' }
function Get-XtDataDir { Join-Path (Get-XtHome) 'data' }

# ----------------------------------------------------------------------------
# 日志
# ----------------------------------------------------------------------------
$script:XtQuiet = $false

function Set-XtQuiet { param([bool]$Value) $script:XtQuiet = $Value }

function Write-XtLog {
    param(
        [Parameter(Mandatory)][ValidateSet('step', 'info', 'ok', 'warn', 'err', 'raw')][string]$Level,
        [Parameter(Mandatory)][AllowEmptyString()][string]$Message
    )
    if ($script:XtQuiet -and $Level -ne 'err' -and $Level -ne 'raw') { return }
    switch ($Level) {
        'step' { Write-Host ''; Write-Host "==> $Message" -ForegroundColor Cyan }
        'info' { Write-Host "    $Message" -ForegroundColor Gray }
        'ok'   { Write-Host "[ok] $Message" -ForegroundColor Green }
        'warn' { Write-Host "[!]  $Message" -ForegroundColor Yellow }
        'err'  { Write-Host "[x]  $Message" -ForegroundColor Red }
        'raw'  { Write-Host $Message }
    }
}

function Write-XtStep { param([string]$Message) Write-XtLog -Level step -Message $Message }
function Write-XtInfo { param([string]$Message) Write-XtLog -Level info -Message $Message }
function Write-XtOk   { param([string]$Message) Write-XtLog -Level ok   -Message $Message }
function Write-XtWarn { param([string]$Message) Write-XtLog -Level warn -Message $Message }
function Write-XtErr  { param([string]$Message) Write-XtLog -Level err  -Message $Message }

# ----------------------------------------------------------------------------
# 结果对象 —— 动词一律返回它, 由入口统一 exit。不用异常做流程控制。
# ----------------------------------------------------------------------------
function New-XtResult {
    param(
        [int]$Code = 0,
        [string]$Message = '',
        [hashtable]$Data
    )
    if (-not $PSBoundParameters.ContainsKey('Data')) { $Data = @{} }
    return [pscustomobject]@{ Code = $Code; Message = $Message; Data = $Data }
}

function New-XtRefusal {
    <# 确定性拒绝。
       Code 默认 PROJECT(4): 输入不是一个可识别的完整工程 (目录不对 / 缺代码 / 缺芯片信息)。
       只有"是完整工程但本工具不支持其形态"才用 UNSUPPORTED(5)。 #>
    param(
        [Parameter(Mandatory)][string]$Reason,
        [string[]]$Missing = @(),
        [string]$Next = '',
        [int]$Code = -1
    )
    if ($Code -lt 0) { $Code = $script:XtExit.PROJECT }
    return [pscustomobject]@{
        Code    = $Code
        Message = $Reason
        Missing = $Missing
        Next    = $Next
    }
}

# ----------------------------------------------------------------------------
# 路径工具
# ----------------------------------------------------------------------------
function Resolve-XtRealPath {
    <# 解析 junction / symlink 到真实路径。
       E:\env\Arm\cortex-m 是指向版本化目录的 junction, 不解析会重复发现同一工具链。 #>
    param([Parameter(Mandatory)][string]$Path)
    try {
        $p = (Resolve-Path -LiteralPath $Path -ErrorAction Stop).Path
    } catch {
        return $Path
    }
    $item = Get-Item -LiteralPath $p -Force -ErrorAction SilentlyContinue
    if ($item -and $item.LinkType) {
        try {
            $target = [System.IO.Directory]::ResolveLinkTarget($p, $true)
            if ($target) { return $target.FullName }
        } catch { }
    }
    return $p
}

function To-XtPosix {
    param([Parameter(Mandatory)][AllowEmptyString()][string]$Path)
    return ($Path -replace '\\', '/')
}

function Get-XtRelative {
    <# 绝对路径 -> 工程相对路径 (正斜杠)。不在工程内则原样返回绝对路径。 #>
    param(
        [Parameter(Mandatory)][string]$Base,
        [Parameter(Mandatory)][string]$Path
    )
    try {
        $rel = [System.IO.Path]::GetRelativePath($Base, $Path)
        return (To-XtPosix $rel)
    } catch {
        return (To-XtPosix $Path)
    }
}

function Test-XtWithin {
    param([string]$Child, [string]$Parent)
    $c = (Resolve-XtRealPath $Child)
    $p = (Resolve-XtRealPath $Parent)
    return $c.StartsWith($p, [System.StringComparison]::OrdinalIgnoreCase)
}

# ----------------------------------------------------------------------------
# 极简 JSON —— 直接用 ConvertFrom-Json 结果, 这里只负责宽容读写
# ----------------------------------------------------------------------------
function Read-XtJsonFile {
    param([Parameter(Mandatory)][string]$Path)
    if (-not (Test-Path -LiteralPath $Path)) { return $null }
    try {
        return (Get-Content -LiteralPath $Path -Raw -Encoding UTF8 | ConvertFrom-Json -AsHashtable)
    } catch {
        Write-XtWarn "配置文件解析失败, 已忽略: $Path ($($_.Exception.Message))"
        return $null
    }
}

function Write-XtJsonFile {
    param(
        [Parameter(Mandatory)][string]$Path,
        [Parameter(Mandatory)]$Object
    )
    $json = $Object | ConvertTo-Json -Depth 8
    Set-Content -LiteralPath $Path -Value $json -Encoding UTF8
}

function Read-XtConfig {
    $cfg = Read-XtJsonFile (Get-XtConfigPath)
    if (-not $cfg) { $cfg = @{} }
    return $cfg
}

function Save-XtConfig {
    param($Config)
    try {
        Write-XtJsonFile -Path (Get-XtConfigPath) -Object $Config
    } catch {
        # 配置缓存写不进去不应该让主流程失败
    }
}
