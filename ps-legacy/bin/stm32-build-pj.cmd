@echo off
setlocal
pwsh -NoProfile -ExecutionPolicy Bypass -File "%~dp0..\lib\xtcli.ps1" build --target stm32 %*
exit /b %ERRORLEVEL%
