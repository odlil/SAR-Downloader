@echo off
setlocal DisableDelayedExpansion
set "PYTHONUTF8=1"
set "PYTHONIOENCODING=utf-8"
cd /d "%~dp0"
if errorlevel 1 goto failed
call "%~dp0scripts\find_python.cmd"
if errorlevel 1 goto failed
echo [SAR] Python: %SAR_BOOT_PYTHON%
"%SAR_BOOT_PYTHON%" %SAR_BOOT_SWITCH% -B "%~dp0check_environment.py" --quiet
if errorlevel 1 goto missing_dependencies
echo [SAR] Starting SAR. Keep this window open; close it to stop the service.
"%SAR_BOOT_PYTHON%" %SAR_BOOT_SWITCH% -B "%~dp0sar_app.py" %*
if errorlevel 1 goto failed
exit /b 0

:missing_dependencies
echo [ERROR] Run the first-time setup CMD file in this folder, then open the launcher again.

:failed
echo [ERROR] SAR could not start. See the message above.
if /i not "%SAR_NO_PAUSE%"=="1" pause
exit /b 1
