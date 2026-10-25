@echo off
REM Desktop shortcut: turn the VPN off, including autostart, until VPN-ON.
REM Elevates itself first - stopping a SYSTEM task needs admin.
net session >nul 2>&1
if not errorlevel 1 goto :run
powershell.exe -NoProfile -ExecutionPolicy Bypass -Command ^
  "Start-Process -FilePath '%~f0' -Verb RunAs"
exit /b 0

:run
powershell.exe -NoProfile -ExecutionPolicy Bypass ^
  -File "%ProgramData%\VPN\vpn-control.ps1" -Action Off
exit /b %ERRORLEVEL%
