@echo off
setlocal
pwsh -NoProfile -ExecutionPolicy Bypass -File "%~dp0..\lib\xtcli.ps1" burn  --target stm32 %*
exit /b %ERRORLEVEL%
