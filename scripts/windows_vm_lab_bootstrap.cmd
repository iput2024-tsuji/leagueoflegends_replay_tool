@echo off
setlocal
powershell.exe -NoProfile -ExecutionPolicy Bypass -File "%~dp0windows_vm_lab_bootstrap.ps1" -GuestAddress 192.168.20.10 -HostAddress 192.168.20.1 -PrefixLength 24
set "exit_code=%ERRORLEVEL%"
echo.
if not "%exit_code%"=="0" echo VM lab bootstrap failed with exit code %exit_code%.
pause
exit /b %exit_code%
