@echo off
REM Entry point of the "VPN sing-box" scheduled task (SYSTEM, at startup).
REM Mode Auto: Reality stays primary, urltest fails over to Hysteria2 alone.
setlocal
set "VPNDIR=%ProgramData%\VPN"
set "VPNLOG=%VPNDIR%\vpn.log"
REM Tells run-vpn.ps1 that no console is attached, so it never waits on input.
set "VPN_NONINTERACTIVE=1"
cd /d "%VPNDIR%"

REM Rotate before the redirect below takes the handle: one 5 MB generation kept.
if exist "%VPNLOG%" for %%A in ("%VPNLOG%") do if %%~zA GTR 5242880 (
  move /y "%VPNLOG%" "%VPNLOG%.1" >nul
)

>>"%VPNLOG%" echo.
>>"%VPNLOG%" echo ==== VPN start %DATE% %TIME% ====
powershell.exe -NoProfile -NonInteractive -ExecutionPolicy Bypass ^
  -File "%VPNDIR%\run-vpn.ps1" -Mode Auto >>"%VPNLOG%" 2>&1
exit /b %ERRORLEVEL%
