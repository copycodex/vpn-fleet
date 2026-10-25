# vpn-watchdog.ps1 - runs every 5 minutes from the "VPN watchdog" task.
#
# The startup task only restarts sing-box when the process dies, which is not
# the failure that actually happens: the process stays alive while its
# transport is blocked. And because the client is a full tunnel with a kill
# switch, that state leaves the machine with no usable network at all.
#
# So: verify that traffic really leaves through the node and restart the VPN
# task when it does not. A manual VPN-OFF disables the task, and a disabled
# task is never resurrected here.

$ErrorActionPreference = 'Stop'
. (Join-Path $PSScriptRoot 'vpn-common.ps1')

$logPath = Join-Path $PSScriptRoot 'watchdog.log'
$statePath = Join-Path $PSScriptRoot 'watchdog-state.json'
# One failed check is normal right after boot while the TUN comes up.
$failuresBeforeRestart = 2
# A restart cannot fix a network that is merely absent, so cap how often the
# watchdog is allowed to churn the tunnel.
$minimumRestartIntervalMinutes = 15

function Get-WatchdogState {
    $empty = [pscustomobject]@{ failures = 0; lastRestart = [datetime]::MinValue }
    try {
        $state = Get-Content -Raw -LiteralPath $statePath -ErrorAction Stop |
            ConvertFrom-Json
    } catch {
        return $empty
    }

    # ConvertFrom-Json revives an ISO-8601 timestamp as [datetime] on its own,
    # but not always, so normalise both shapes here. Keeping it typed is what
    # stops the value from being re-serialised in the local culture's format,
    # which no invariant parse would read back.
    $lastRestart = [datetime]::MinValue
    if ($state.lastRestart -is [datetime]) {
        $lastRestart = $state.lastRestart
    } elseif ($state.lastRestart) {
        [datetime]::TryParse($state.lastRestart,
            [Globalization.CultureInfo]::InvariantCulture,
            [Globalization.DateTimeStyles]::RoundtripKind,
            [ref]$lastRestart) | Out-Null
    }
    return [pscustomobject]@{
        failures    = [int]$state.failures
        lastRestart = $lastRestart
    }
}

function Set-WatchdogState {
    param([int]$Failures, [datetime]$LastRestart = [datetime]::MinValue)

    $stamp = $null
    if ($LastRestart -gt [datetime]::MinValue) { $stamp = $LastRestart.ToString('o') }
    [pscustomobject]@{ failures = $Failures; lastRestart = $stamp } |
        ConvertTo-Json | Set-Content -LiteralPath $statePath
}

function Restart-VpnTask {
    Stop-ScheduledTask -TaskName $VpnTaskName -ErrorAction SilentlyContinue
    Start-Sleep -Seconds 3
    # Task Scheduler kills cmd.exe; sing-box occasionally outlives it and would
    # keep the TUN adapter, which makes the fresh instance fail to start.
    Get-Process -Name 'sing-box' -ErrorAction SilentlyContinue |
        Stop-Process -Force -ErrorAction SilentlyContinue
    Start-Sleep -Seconds 2
    Start-ScheduledTask -TaskName $VpnTaskName
}

$state = Get-WatchdogState

$taskState = Get-VpnTaskState -Name $VpnTaskName
if ($taskState -eq 'Disabled') { exit 0 }
if ($taskState -eq 'Missing') {
    Write-VpnLog -Path $logPath -Message "FAIL task=$VpnTaskName missing, reinstall needed"
    exit 1
}

if ($taskState -ne 'Running') {
    Write-VpnLog -Path $logPath -Message "RESTART reason=task-not-running state=$taskState"
    Start-ScheduledTask -TaskName $VpnTaskName
    Set-WatchdogState -Failures 0 -LastRestart (Get-Date)
    exit 0
}

$configPath = Get-VpnConfigPath -Directory $PSScriptRoot
if (-not $configPath) {
    Write-VpnLog -Path $logPath -Message 'FAIL no single *-win.json in the install directory'
    exit 1
}
$nodeAddress = Get-VpnNodeAddress -ConfigPath $configPath
if (-not $nodeAddress) {
    Write-VpnLog -Path $logPath -Message 'FAIL config has no node address'
    exit 1
}

# No link, no point: with the cable out or Wi-Fi off the tunnel is expected to
# be down and a restart changes nothing.
if (-not (Test-VpnPhysicalLink)) {
    Write-VpnLog -Path $logPath -Message 'SKIP reason=no-physical-link'
    exit 0
}

$egress = Get-VpnEgressAddress
if ($egress -eq $nodeAddress) {
    if ($state.failures -gt 0) {
        Write-VpnLog -Path $logPath -Message "OK egress=$egress recovered"
        Set-WatchdogState -Failures 0 -LastRestart $state.lastRestart
    }
    exit 0
}

$failures = $state.failures + 1
$observed = if ($egress) { $egress } else { 'none' }
if ($failures -lt $failuresBeforeRestart) {
    Write-VpnLog -Path $logPath -Message "WARN egress=$observed expected=$nodeAddress failures=$failures"
    Set-WatchdogState -Failures $failures -LastRestart $state.lastRestart
    exit 0
}

if ($state.lastRestart -gt [datetime]::MinValue) {
    $since = (Get-Date) - $state.lastRestart
    if ($since.TotalMinutes -lt $minimumRestartIntervalMinutes) {
        Write-VpnLog -Path $logPath -Message ("SKIP reason=restarted-recently minutes={0:N0} egress=$observed" -f $since.TotalMinutes)
        Set-WatchdogState -Failures $failures -LastRestart $state.lastRestart
        exit 0
    }
}

Write-VpnLog -Path $logPath -Message "RESTART reason=tunnel-dead egress=$observed expected=$nodeAddress"
Restart-VpnTask
Set-WatchdogState -Failures 0 -LastRestart (Get-Date)
exit 0
