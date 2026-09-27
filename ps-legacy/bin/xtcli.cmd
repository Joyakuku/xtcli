@echo off
setlocal
pwsh -NoProfile -ExecutionPolicy Bypass -File "%~dp0..\lib\xtcli.ps1"  %*
exit /b %ERRORLEVEL%
