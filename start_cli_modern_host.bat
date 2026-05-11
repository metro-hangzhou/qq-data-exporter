@echo off
setlocal EnableDelayedExpansion
cd /d "%~dp0"

set "CLI_AUTO_WT=0"
call "%~dp0start_cli.bat" --launched-in-modern-host %*
exit /b %errorlevel%
