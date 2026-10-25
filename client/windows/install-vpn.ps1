# install-vpn.ps1 - one-click install of the Windows VPN client.
#
# Installs into C:\ProgramData\VPN, registers two SYSTEM tasks - the VPN itself
# at startup and a five-minute watchdog - and drops VPN-ON / VPN-OFF shortcuts
# on the shared desktop. The issued *-win.json is picked up automatically, so
# the same bundle works for any client of the fleet.

$ErrorActionPreference = 'Stop'
. (Join-Path $PSScriptRoot 'vpn-common.ps1')

$Interactive = [Environment]::UserInteractive -and -not $env:VPN_NONINTERACTIVE

# The elevated instance owns its own window, so an unhandled error would close
# it before anyone could read what went wrong.
trap {
    Write-Host ''
    Write-Host "Установка не удалась: $($_.Exception.Message)" -ForegroundColor Red
    if ($Interactive) { Read-Host 'Нажми Enter, чтобы закрыть окно' | Out-Null }
    exit 1
}

$isAdmin = ([Security.Principal.WindowsPrincipal] `
    [Security.Principal.WindowsIdentity]::GetCurrent() `
    ).IsInRole([Security.Principal.WindowsBuiltInRole]::Administrator)

if (-not $isAdmin) {
    $elevated = Start-Process powershell.exe -Verb RunAs -Wait -PassThru `
        -ArgumentList @(
        '-NoProfile', '-ExecutionPolicy', 'Bypass',
        '-File', "`"$PSCommandPath`""
    )
    exit $elevated.ExitCode
}

$source = $PSScriptRoot
$destination = Join-Path $env:ProgramData 'VPN'
$requiredFiles = @(
    'sing-box.exe',
    'libcronet.dll',
    'run-vpn.ps1',
    'start-vpn-task.cmd',
    'vpn-common.ps1',
    'vpn-control.ps1',
    'vpn-watchdog.ps1',
    'VPN-ON.cmd',
    'VPN-OFF.cmd',
    'VPN-STATUS.cmd'
)
$optionalFiles = @('README.txt')

foreach ($file in $requiredFiles) {
    if (-not (Test-Path -LiteralPath (Join-Path $source $file) -PathType Leaf)) {
        throw "Required file is missing: $file"
    }
}

$sourceConfig = Get-VpnConfigPath -Directory $source
if (-not $sourceConfig) {
    throw 'Expected exactly one issued *-win.json next to this installer.'
}
$configName = Split-Path -Leaf $sourceConfig

if ([IO.Path]::GetFullPath($source).TrimEnd('\') -ieq
    [IO.Path]::GetFullPath($destination).TrimEnd('\')) {
    throw "Run the installer from the copied bundle folder, not from $destination."
}

# Reinstall over a live VPN: Windows locks a running sing-box.exe, so the copy
# below would fail halfway. Disable the watchdog first, or it would restart the
# VPN in the middle of the install.
Disable-ScheduledTask -TaskName $VpnWatchdogTaskName -ErrorAction SilentlyContinue | Out-Null
foreach ($name in @($VpnWatchdogTaskName, $VpnTaskName, $VpnLegacyTaskName)) {
    Stop-ScheduledTask -TaskName $name -ErrorAction SilentlyContinue
}
Get-Process -Name 'sing-box' -ErrorAction SilentlyContinue |
    Stop-Process -Force -ErrorAction SilentlyContinue
Start-Sleep -Seconds 2

New-Item -ItemType Directory -Path $destination -Force | Out-Null
foreach ($file in ($requiredFiles + $configName)) {
    Copy-Item -LiteralPath (Join-Path $source $file) `
        -Destination (Join-Path $destination $file) -Force
}
foreach ($file in $optionalFiles) {
    $path = Join-Path $source $file
    if (Test-Path -LiteralPath $path -PathType Leaf) {
        Copy-Item -LiteralPath $path -Destination (Join-Path $destination $file) -Force
    }
}

# A stale config from an earlier client would make run-vpn.ps1 refuse to start.
Get-ChildItem -LiteralPath $destination -Filter '*-win.json' -File |
    Where-Object { $_.Name -ne $configName } |
    Remove-Item -Force

$exe = Join-Path $destination 'sing-box.exe'
$config = Join-Path $destination $configName
$runner = Join-Path $destination 'start-vpn-task.cmd'
$watchdog = Join-Path $destination 'vpn-watchdog.ps1'

Write-Host "Конфигурация: $configName" -ForegroundColor Cyan
Write-Host 'Проверяю конфигурацию...' -ForegroundColor Cyan
& $exe check -c $config
if ($LASTEXITCODE -ne 0) {
    throw "sing-box config check failed with exit code $LASTEXITCODE"
}

# The config and the runtime copy hold VPN credentials, so only SYSTEM and
# administrators get in. The desktop shortcuts elevate before touching this
# directory, so ordinary users need no access of their own.
& icacls.exe $destination /inheritance:r `
    /grant:r '*S-1-5-18:(OI)(CI)F' `
             '*S-1-5-32-544:(OI)(CI)F' | Out-Null
if ($LASTEXITCODE -ne 0) {
    throw 'Failed to secure the VPN directory ACL.'
}

$principal = New-ScheduledTaskPrincipal -UserId 'SYSTEM' `
    -LogonType ServiceAccount -RunLevel Highest

# Pre-Auto installs pinned Reality in the task name and in its command line.
Stop-ScheduledTask -TaskName $VpnLegacyTaskName -ErrorAction SilentlyContinue
Unregister-ScheduledTask -TaskName $VpnLegacyTaskName -Confirm:$false `
    -ErrorAction SilentlyContinue

$vpnAction = New-ScheduledTaskAction -Execute 'cmd.exe' `
    -Argument "/d /c `"$runner`"" -WorkingDirectory $destination
$vpnTrigger = New-ScheduledTaskTrigger -AtStartup
$vpnTrigger.Delay = 'PT15S'
$vpnSettings = New-ScheduledTaskSettingsSet `
    -RestartCount 10 `
    -RestartInterval (New-TimeSpan -Minutes 1) `
    -MultipleInstances IgnoreNew `
    -ExecutionTimeLimit (New-TimeSpan -Days 3650) `
    -StartWhenAvailable `
    -AllowStartIfOnBatteries `
    -DontStopIfGoingOnBatteries

Stop-ScheduledTask -TaskName $VpnTaskName -ErrorAction SilentlyContinue
Register-ScheduledTask -TaskName $VpnTaskName -Action $vpnAction `
    -Trigger $vpnTrigger -Principal $principal -Settings $vpnSettings -Force | Out-Null

# Repeating -Once trigger rather than -AtStartup: the watchdog must also cover
# the case where the VPN task exhausted its ten restarts hours after boot.
$watchdogAction = New-ScheduledTaskAction -Execute 'powershell.exe' `
    -Argument ("-NoProfile -NonInteractive -ExecutionPolicy Bypass -File `"$watchdog`"") `
    -WorkingDirectory $destination
$watchdogTrigger = New-ScheduledTaskTrigger -Once -At (Get-Date).Date `
    -RepetitionInterval (New-TimeSpan -Minutes 5) `
    -RepetitionDuration (New-TimeSpan -Days 3650)
$watchdogSettings = New-ScheduledTaskSettingsSet `
    -MultipleInstances IgnoreNew `
    -ExecutionTimeLimit (New-TimeSpan -Minutes 10) `
    -StartWhenAvailable `
    -AllowStartIfOnBatteries `
    -DontStopIfGoingOnBatteries

Stop-ScheduledTask -TaskName $VpnWatchdogTaskName -ErrorAction SilentlyContinue
Register-ScheduledTask -TaskName $VpnWatchdogTaskName -Action $watchdogAction `
    -Trigger $watchdogTrigger -Principal $principal -Settings $watchdogSettings -Force | Out-Null

$desktop = Join-Path $env:PUBLIC 'Desktop'
if (Test-Path -LiteralPath $desktop) {
    foreach ($shortcut in @('VPN-ON.cmd', 'VPN-OFF.cmd')) {
        Copy-Item -LiteralPath (Join-Path $destination $shortcut) `
            -Destination (Join-Path $desktop $shortcut) -Force
    }
}

Start-ScheduledTask -TaskName $VpnTaskName

$nodeAddress = Get-VpnNodeAddress -ConfigPath $config
$connected = $false
Write-Host 'Поднимаю туннель...' -ForegroundColor Cyan
foreach ($attempt in 1..12) {
    Start-Sleep -Seconds 5
    if ((Get-VpnEgressAddress -TimeoutSec 5) -eq $nodeAddress) {
        $connected = $true
        break
    }
    Write-Host '.' -NoNewline
}
Write-Host ''

Write-Host ''
if ($connected) {
    Write-Host "VPN установлен и работает, трафик идёт через $nodeAddress." -ForegroundColor Green
} else {
    Write-Host 'VPN установлен, но туннель пока не поднялся.' -ForegroundColor Yellow
    Write-Host "Смотри лог: $(Join-Path $destination 'vpn.log')"
    Write-Host 'Если интернета нет совсем - выключи VPN ярлыком VPN-OFF на рабочем столе.'
}
Write-Host "Каталог установки: $destination"
Write-Host "Задачи: «$VpnTaskName» (автозапуск) и «$VpnWatchdogTaskName» (проверка раз в 5 минут)"
Write-Host 'На рабочем столе: VPN-ON.cmd и VPN-OFF.cmd'
Write-Host 'Перезагрузись один раз и проверь, что VPN поднялся сам.'
if ($Interactive) { Read-Host 'Нажми Enter, чтобы закрыть окно' | Out-Null }
