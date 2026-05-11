@echo off
setlocal EnableExtensions EnableDelayedExpansion
cd /d "%~dp0"

set "NAPCAT_LAUNCHER=%~dp0NapCat\napcat\launcher-win10.bat"
if defined NAPCAT_LAUNCHER_OVERRIDE set "NAPCAT_LAUNCHER=%NAPCAT_LAUNCHER_OVERRIDE%"
set "NAPCAT_LOG_DIR=%~dp0state\napcat_logs"
set "NAPCAT_QUICK_LOGIN_FILE=%~dp0state\config\napcat_quick_login_uin.txt"
for %%I in ("%NAPCAT_LAUNCHER%") do set "NAPCAT_LAUNCHER_DIR=%%~dpI"

if not exist "%NAPCAT_LAUNCHER%" (
  echo NapCat launcher not found:
  echo   %NAPCAT_LAUNCHER%
  pause
  exit /b 1
)

if not defined NAPCAT_QUICK_ACCOUNT if defined NAPCAT_QUICK_LOGIN_UIN set "NAPCAT_QUICK_ACCOUNT=%NAPCAT_QUICK_LOGIN_UIN%"
if not defined NAPCAT_QUICK_ACCOUNT if exist "%NAPCAT_QUICK_LOGIN_FILE%" (
  set /p NAPCAT_QUICK_ACCOUNT=<"%NAPCAT_QUICK_LOGIN_FILE%"
)
if defined NAPCAT_QUICK_ACCOUNT set "NAPCAT_QUICK_LOGIN_UIN=%NAPCAT_QUICK_ACCOUNT%"
set "NAPCAT_ARGS=%*"
set "NAPCAT_HAS_QUICK_ARG="
echo %NAPCAT_ARGS%| findstr /I /C:"-q " >nul && set "NAPCAT_HAS_QUICK_ARG=1"
if defined NAPCAT_QUICK_ACCOUNT if not defined NAPCAT_HAS_QUICK_ARG set "NAPCAT_ARGS=-q %NAPCAT_QUICK_ACCOUNT% %NAPCAT_ARGS%"

if not exist "%NAPCAT_LOG_DIR%" mkdir "%NAPCAT_LOG_DIR%"

if /I "%NAPCAT_SKIP_ADMIN_CHECK%"=="1" goto admin_ready

net session >nul 2>&1
if %ERRORLEVEL% neq 0 (
  echo Requesting administrator mode for NapCat startup...
  set "NAPCAT_ELEVATED_WRAPPER=%NAPCAT_LOG_DIR%\napcat_elevated_%RANDOM%%RANDOM%.cmd"
  > "!NAPCAT_ELEVATED_WRAPPER!" echo @echo off
  >> "!NAPCAT_ELEVATED_WRAPPER!" echo set "NAPCAT_SKIP_ADMIN_CHECK=1"
  if defined NAPCAT_QUICK_ACCOUNT >> "!NAPCAT_ELEVATED_WRAPPER!" echo set "NAPCAT_QUICK_ACCOUNT=%NAPCAT_QUICK_ACCOUNT%"
  >> "!NAPCAT_ELEVATED_WRAPPER!" echo cd /d "%cd%"
  >> "!NAPCAT_ELEVATED_WRAPPER!" echo call "%~f0" %NAPCAT_ARGS%
  powershell -NoProfile -Command "Start-Process -FilePath '!NAPCAT_ELEVATED_WRAPPER!' -Verb RunAs"
  exit /b 0
)

:admin_ready
for /f "usebackq delims=" %%I in (`powershell -NoProfile -Command "(Get-Date).ToString('yyyyMMdd_HHmmss')"`) do set "NAPCAT_LOG_STAMP=%%I"
set "NAPCAT_LOG_FILE=%NAPCAT_LOG_DIR%\napcat_%NAPCAT_LOG_STAMP%.log"
set "NAPCAT_LOG_POINTER=%NAPCAT_LOG_DIR%\latest.path"

> "%NAPCAT_LOG_POINTER%" echo %NAPCAT_LOG_FILE%
echo NapCat log: %NAPCAT_LOG_FILE%
>> "%NAPCAT_LOG_FILE%" echo [launcher] started=%DATE% %TIME% launcher=%NAPCAT_LAUNCHER%
if defined NAPCAT_QUICK_ACCOUNT >> "%NAPCAT_LOG_FILE%" echo [launcher] quick_login_account=%NAPCAT_QUICK_ACCOUNT%
>> "%NAPCAT_LOG_FILE%" echo [launcher] args=%NAPCAT_ARGS%

pushd "%NAPCAT_LAUNCHER_DIR%"
if defined NAPCAT_QUICK_ACCOUNT set "NAPCAT_QUICK_ACCOUNT=%NAPCAT_QUICK_ACCOUNT%"
call "%NAPCAT_LAUNCHER%" %NAPCAT_ARGS% >> "%NAPCAT_LOG_FILE%" 2>&1
set "NAPCAT_EXIT_CODE=%ERRORLEVEL%"
popd

>> "%NAPCAT_LOG_FILE%" echo [launcher] exited=%DATE% %TIME% exit_code=%NAPCAT_EXIT_CODE%
if not "%NAPCAT_EXIT_CODE%"=="0" (
  echo NapCat launcher exited with code %NAPCAT_EXIT_CODE%.
  echo See log: %NAPCAT_LOG_FILE%
  pause
)
exit /b %NAPCAT_EXIT_CODE%
