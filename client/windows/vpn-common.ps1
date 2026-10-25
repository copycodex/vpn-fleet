# Shared helpers for the Windows VPN scripts. Dot-sourced by vpn-watchdog.ps1
# and vpn-control.ps1; keep it free of side effects.

$VpnTaskName = 'VPN sing-box'
$VpnWatchdogTaskName = 'VPN watchdog'
# Pre-Auto installations registered the task under this name.
$VpnLegacyTaskName = 'VPN sing-box Reality'

# Reached through the tunnel and answers with the egress address, so one
# request proves both that the tunnel is up and that traffic really exits
# through the node. An IP literal, so a broken tunnel DNS cannot mask it.
$VpnTraceUrl = 'https://1.1.1.1/cdn-cgi/trace'

function Get-VpnConfigPath {
    param([Parameter(Mandatory)][string]$Directory)

    $issued = @(Get-ChildItem -LiteralPath $Directory -Filter '*-win.json' -File)
    if ($issued.Count -ne 1) { return $null }
    return $issued[0].FullName
}

function Get-VpnNodeAddress {
    param([Parameter(Mandatory)][string]$ConfigPath)

    $config = Get-Content -Raw -LiteralPath $ConfigPath | ConvertFrom-Json
    $node = @($config.outbounds |
        Where-Object { $_.type -in @('vless', 'hysteria2') -and $_.server })
    if ($node.Count -eq 0) { return $null }
    return $node[0].server
}

function Test-VpnPhysicalLink {
    # Whether the machine has a cable or Wi-Fi link at all.
    #
    # Deliberately not a TCP probe of the node: while the TUN adapter is up its
    # stack completes the local handshake before it knows whether the address
    # is reachable, so a connect to any address and any port succeeds. Measured
    # against the live node - a closed port and a random high port both came
    # back "open" - so any such probe would report a healthy network forever.
    $up = @(Get-NetAdapter -Physical -ErrorAction SilentlyContinue |
        Where-Object { $_.Status -eq 'Up' })
    return $up.Count -gt 0
}

function Get-VpnEgressAddress {
    param([int]$TimeoutSec = 10)

    try {
        # Windows PowerShell 5.1 still negotiates TLS 1.0 by default, which
        # every endpoint worth probing now refuses.
        [Net.ServicePointManager]::SecurityProtocol = [Net.SecurityProtocolType]::Tls12
        # -UseBasicParsing: the SYSTEM account has no Internet Explorer profile.
        $response = Invoke-WebRequest -Uri $VpnTraceUrl -UseBasicParsing `
            -TimeoutSec $TimeoutSec
        if ($response.Content -match '(?m)^ip=(.+)$') {
            return $Matches[1].Trim()
        }
        return $null
    } catch {
        return $null
    }
}

function Get-VpnTaskState {
    param([Parameter(Mandatory)][string]$Name)

    $task = Get-ScheduledTask -TaskName $Name -ErrorAction SilentlyContinue
    if (-not $task) { return 'Missing' }
    return [string]$task.State
}

function Write-VpnLog {
    param(
        [Parameter(Mandatory)][string]$Path,
        [Parameter(Mandatory)][string]$Message,
        [int]$MaxBytes = 1048576
    )

    $existing = Get-Item -LiteralPath $Path -ErrorAction SilentlyContinue
    if ($existing -and $existing.Length -gt $MaxBytes) {
        Move-Item -LiteralPath $Path -Destination "$Path.1" -Force
    }
    $stamp = Get-Date -Format 'yyyy-MM-ddTHH:mm:ss'
    Add-Content -LiteralPath $Path -Value "$stamp $Message"
}
