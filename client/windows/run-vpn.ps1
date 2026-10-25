param(
    [ValidateSet('Auto', 'Reality', 'Hy2')]
    [string]$Mode = 'Auto',
    [string]$Config
)

# run-vpn.ps1 - start sing-box on Windows.
#
# Auto is the default and the only mode used by the autostart task: the
# urltest group keeps Reality primary (see $UrltestTolerance) and moves to
# Hysteria2 on its own as soon as Reality stops answering. Reality and Hy2
# stay available as manual overrides for a single session.
#
# Runs both interactively (double-click) and under the SYSTEM scheduled task,
# so no code path may block on input: a Read-Host under Task Scheduler hangs
# the task forever and its restart-on-failure never fires.

$ErrorActionPreference = 'Stop'
Set-Location -Path $PSScriptRoot

# Reality must be listed first in the urltest group: with a tolerance this
# large the group only leaves it when it is dead or catastrophically slow.
$UrltestTolerance = 3000
$UrltestInterval = '1m'
$UrltestIdleTimeout = '30m'

$Interactive = [Environment]::UserInteractive -and -not $env:VPN_NONINTERACTIVE

function Exit-WithMessage {
    param([string]$Message, [int]$Code = 1)

    Write-Host $Message -ForegroundColor Red
    if ($Interactive) { Read-Host 'Enter - close this window' | Out-Null }
    exit $Code
}

$exe = Join-Path $PSScriptRoot 'sing-box.exe'
$runtimeConfig = Join-Path $PSScriptRoot 'runtime-config.json'

if ($Config) {
    $configPath = $Config
    if (-not [IO.Path]::IsPathRooted($configPath)) {
        $configPath = Join-Path $PSScriptRoot $configPath
    }
} else {
    $issuedConfigs = @(Get-ChildItem -LiteralPath $PSScriptRoot `
            -Filter '*-win.json' -File)
    if ($issuedConfigs.Count -ne 1) {
        Exit-WithMessage ("Expected exactly one *-win.json next to this script, " +
            "found $($issuedConfigs.Count).")
    }
    $configPath = $issuedConfigs[0].FullName
}

# Self-elevate if needed: TUN with auto_route requires Administrator.
$isAdmin = ([Security.Principal.WindowsPrincipal] `
    [Security.Principal.WindowsIdentity]::GetCurrent() `
    ).IsInRole([Security.Principal.WindowsBuiltInRole]::Administrator)
if (-not $isAdmin) {
    Start-Process powershell.exe -Verb RunAs -ArgumentList @(
        '-NoExit', '-NoProfile', '-ExecutionPolicy', 'Bypass',
        '-File', "`"$PSCommandPath`"",
        '-Mode', $Mode,
        '-Config', "`"$configPath`""
    )
    exit 0
}

if (-not (Test-Path -LiteralPath $exe)) {
    Exit-WithMessage 'sing-box.exe not found next to this script.'
}
if (-not (Test-Path -LiteralPath $configPath)) {
    Exit-WithMessage "Config not found: $configPath"
}

$configObject = Get-Content -Raw -LiteralPath $configPath | ConvertFrom-Json

$selector = @($configObject.outbounds | Where-Object { $_.tag -eq 'proxy' })
if ($selector.Count -ne 1) {
    Exit-WithMessage "Config must contain exactly one outbound tagged 'proxy'."
}

$selectedOutbound = switch ($Mode) {
    'Reality' { 'vless-reality' }
    'Hy2' { 'hy2' }
    default { 'auto' }
}
if ($selector[0].outbounds -notcontains $selectedOutbound) {
    Exit-WithMessage "Config has no outbound '$selectedOutbound'."
}
$selector[0].default = $selectedOutbound

foreach ($group in @($configObject.outbounds | Where-Object { $_.type -eq 'urltest' })) {
    Add-Member -InputObject $group -Force `
        -NotePropertyName 'tolerance' -NotePropertyValue $UrltestTolerance
    Add-Member -InputObject $group -Force `
        -NotePropertyName 'interval' -NotePropertyValue $UrltestInterval
    Add-Member -InputObject $group -Force `
        -NotePropertyName 'idle_timeout' -NotePropertyValue $UrltestIdleTimeout
    # Without this a failover leaves every existing connection pinned to the
    # dead transport, so the machine looks online but nothing loads.
    Add-Member -InputObject $group -Force `
        -NotePropertyName 'interrupt_exist_connections' -NotePropertyValue $true
}

try {
    # The runtime copy carries the same credentials as the issued config, so it
    # is written next to it inside the ACL-protected install directory and
    # never into a world-writable temp directory.
    # Windows PowerShell 5.1 writes a BOM with Set-Content -Encoding UTF8 and
    # sing-box expects BOM-free JSON.
    [IO.File]::WriteAllText(
        $runtimeConfig,
        ($configObject | ConvertTo-Json -Depth 32),
        [Text.UTF8Encoding]::new($false)
    )

    Write-Host "Config: $configPath" -ForegroundColor Cyan
    Write-Host "Transport mode: $Mode" -ForegroundColor Cyan
    Write-Host 'Checking config...' -ForegroundColor Cyan
    & $exe check -c $runtimeConfig
    if ($LASTEXITCODE -ne 0) { Exit-WithMessage 'config check FAILED' }

    Write-Host 'VPN running. Close this window or press Ctrl+C to stop.' -ForegroundColor Green
    & $exe run -c $runtimeConfig
    $runExitCode = $LASTEXITCODE
    if ($runExitCode -ne 0) {
        Write-Host 'Copy the error lines above; do not send the JSON config because it contains credentials.' -ForegroundColor Yellow
        Exit-WithMessage "sing-box stopped with exit code $runExitCode" $runExitCode
    }
} finally {
    Remove-Item -LiteralPath $runtimeConfig -Force -ErrorAction SilentlyContinue
}
