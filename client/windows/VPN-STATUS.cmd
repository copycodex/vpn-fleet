@echo off
REM Show whether the VPN is on and whether traffic really leaves via the node.
net session >nul 2>&1
if not errorlevel 1 goto :run
powershell.exe -NoProfile -ExecutionPolicy Bypass -Command ^
  "Start-Process -FilePath '%~f0' -Verb RunAs"
exit /b 0

:run
powershell.exe -NoProfile -ExecutionPolicy Bypass ^
  -File "%ProgramData%\VPN\vpn-control.ps1" -Action Status
exit /b %ERRORLEVEL%
