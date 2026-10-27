@echo off
setlocal DisableDelayedExpansion
set "PYTHONUTF8=1"
set "PYTHONIOENCODING=utf-8"
cd /d "%~dp0"
call "%~dp0scripts\find_python.cmd"
if errorlevel 1 goto failed
"%SAR_BOOT_PYTHON%" %SAR_BOOT_SWITCH% -B "%~dp0scripts\setup_dem.py" %*
if errorlevel 1 goto failed
if /i not "%SAR_NO_PAUSE%"=="1" pause
exit /b 0
:failed
echo [ERROR] DEM setup did not complete. Check network access and the message above.
if /i not "%SAR_NO_PAUSE%"=="1" pause
exit /b 1
