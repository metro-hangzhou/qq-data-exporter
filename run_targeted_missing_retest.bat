@echo off
setlocal EnableDelayedExpansion
cd /d "%~dp0"

echo Running targeted missing retest...
echo Repo: %cd%

if exist ".venv\Scripts\python.exe" (
  ".venv\Scripts\python.exe" targeted_missing_retest.py %*
  goto :end
)

if exist "python_runtime\python.exe" (
  set "BUNDLE_ROOT=%cd%"
  set "SRC_PATH=!BUNDLE_ROOT!\src"
  set "SITE_PACKAGES=!BUNDLE_ROOT!\runtime_site_packages"
  set "PYTHONPATH=!SRC_PATH!;!SITE_PACKAGES!;!PYTHONPATH!"
  set "PATH=!BUNDLE_ROOT!\python_runtime;!PATH!"
  set "PYTHONHOME=!BUNDLE_ROOT!\python_runtime"
  "python_runtime\python.exe" -c "import pydantic,typer,httpx,rich,qrcode,websockets" >nul 2>nul
  if not "!errorlevel!"=="0" (
    echo Bundled Python runtime found but bundled dependencies are incomplete.
    echo Expected runtime_site_packages to contain the minimal CLI dependency set.
    goto :end
  )
  "python_runtime\python.exe" targeted_missing_retest.py %*
  goto :end
)

set "BUNDLE_ROOT=%cd%"
set "SRC_PATH=!BUNDLE_ROOT!\src"
set "SITE_PACKAGES=!BUNDLE_ROOT!\runtime_site_packages"
set "PYTHONPATH=!SRC_PATH!;!SITE_PACKAGES!;!PYTHONPATH!"
py -3.13 targeted_missing_retest.py %*

:end
echo.
echo Done. Check state\targeted_retests\latest.path for the newest run directory.
pause
