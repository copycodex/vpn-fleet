param(
    [ValidateSet('On', 'Off', 'Status')]
    [string]$Action = 'Status'
)

# vpn-control.ps1 - manual on/off/status for the installed VPN.
# Driven by the VPN-ON.cmd / VPN-OFF.cmd shortcuts on the desktop.
# Off is sticky: it disables the autostart task as well, so the VPN stays off
# across reboots until VPN-ON. The watchdog is disabled first, otherwise it
# would bring the tunnel back within five minutes.

$ErrorActionPreference = 'Stop'
. (Join-Path $PSScriptRoot 'vpn-common.ps1')

$Interactive = [Environment]::UserInteractive -and -not $env:VPN_NONINTERACTIVE

function Complete-Run {
    param([int]$Code = 0)
    if ($Interactive) { Read-Host 'Нажми Enter, чтобы закрыть окно' | Out-Null }
    exit $Code
}

$isAdmin = ([Security.Principal.WindowsPrincipal] `
    [Security.Principal.WindowsIdentity]::GetCurrent() `
    ).IsInRole([Security.Principal.WindowsBuiltInRole]::Administrator)
if (-not $isAdmin) {
    $elevated = Start-Process powershell.exe -Verb RunAs -Wait -PassThru `
        -ArgumentList @(
        '-NoProfile', '-ExecutionPolicy', 'Bypass',
        '-File', "`"$PSCommandPath`"",
        '-Action', $Action
    )
    exit $elevated.ExitCode
}

if ((Get-VpnTaskState -Name $VpnTaskName) -eq 'Missing') {
    Write-Host 'VPN не установлен: сначала запусти INSTALL-VPN.cmd.' -ForegroundColor Red
    Complete-Run 1
}

function Show-Status {
    param([switch]$SkipEgress)

    $vpnState = Get-VpnTaskState -Name $VpnTaskName
    $watchdogState = Get-VpnTaskState -Name $VpnWatchdogTaskName
    Write-Host ''
    Write-Host "Задача VPN:      $vpnState"
    Write-Host "Задача вотчдога: $watchdogState"
    if ($SkipEgress) { return }

    $configPath = Get-VpnConfigPath -Directory $PSScriptRoot
    $nodeAddress = if ($configPath) { Get-VpnNodeAddress -ConfigPath $configPath } else { $null }
    $egress = Get-VpnEgressAddress
    if ($egress -and $egress -eq $nodeAddress) {
        Write-Host "Трафик идёт через VPN, внешний адрес: $egress" -ForegroundColor Green
    } elseif ($egress) {
        Write-Host "ВНИМАНИЕ: трафик идёт МИМО VPN, внешний адрес: $egress" -ForegroundColor Yellow
    } else {
        Write-Host 'Интернет сейчас недоступен (или туннель ещё поднимается).' -ForegroundColor Yellow
    }
}

switch ($Action) {
    'On' {
        Write-Host 'Включаю VPN...' -ForegroundColor Cyan
        Enable-ScheduledTask -TaskName $VpnTaskName | Out-Null
        Enable-ScheduledTask -TaskName $VpnWatchdogTaskName -ErrorAction SilentlyContinue | Out-Null
        # Restart rather than start: ON must also repair a hung tunnel.
        Stop-ScheduledTask -TaskName $VpnTaskName -ErrorAction SilentlyContinue
        Start-Sleep -Seconds 2
        Get-Process -Name 'sing-box' -ErrorAction SilentlyContinue |
            Stop-Process -Force -ErrorAction SilentlyContinue
        Start-ScheduledTask -TaskName $VpnTaskName

        $configPath = Get-VpnConfigPath -Directory $PSScriptRoot
        $nodeAddress = if ($configPath) { Get-VpnNodeAddress -ConfigPath $configPath } else { $null }
        $connected = $false
        foreach ($attempt in 1..12) {
            Start-Sleep -Seconds 5
            if ((Get-VpnEgressAddress -TimeoutSec 5) -eq $nodeAddress) {
                $connected = $true
                break
            }
            Write-Host '.' -NoNewline
        }
        Write-Host ''
        if ($connected) {
            Write-Host "VPN включён, трафик идёт через $nodeAddress." -ForegroundColor Green
            Write-Host 'Он же будет включаться сам при каждой загрузке Windows.'
        } else {
            Write-Host 'VPN запущен, но туннель за минуту не поднялся.' -ForegroundColor Yellow
            Write-Host "Смотри лог: $(Join-Path $PSScriptRoot 'vpn.log')"
            Write-Host 'Если интернета нет совсем - выключи VPN ярлыком VPN-OFF на рабочем столе.'
        }
        Show-Status -SkipEgress
        Complete-Run 0
    }

    'Off' {
        Write-Host 'Выключаю VPN...' -ForegroundColor Cyan
        # Watchdog first: it is the thing that would undo the next two lines.
        Disable-ScheduledTask -TaskName $VpnWatchdogTaskName -ErrorAction SilentlyContinue | Out-Null
        Stop-ScheduledTask -TaskName $VpnWatchdogTaskName -ErrorAction SilentlyContinue
        Disable-ScheduledTask -TaskName $VpnTaskName | Out-Null
        Stop-ScheduledTask -TaskName $VpnTaskName -ErrorAction SilentlyContinue
        Start-Sleep -Seconds 2
        Get-Process -Name 'sing-box' -ErrorAction SilentlyContinue |
            Stop-Process -Force -ErrorAction SilentlyContinue
        Start-Sleep -Seconds 2

        if (Get-Process -Name 'sing-box' -ErrorAction SilentlyContinue) {
            Write-Host 'Не удалось остановить sing-box. Перезагрузи компьютер.' -ForegroundColor Red
            Complete-Run 1
        }
        Write-Host 'VPN выключен, интернет идёт напрямую.' -ForegroundColor Green
        Write-Host 'Сам он больше не включится - даже после перезагрузки.' -ForegroundColor Yellow
        Write-Host 'Включить обратно: ярлык VPN-ON на рабочем столе.'
        Show-Status -SkipEgress
        Complete-Run 0
    }

    default {
        Show-Status
        Complete-Run 0
    }
}
