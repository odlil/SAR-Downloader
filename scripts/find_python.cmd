@echo off
rem Called by the public launchers; returns SAR_BOOT_PYTHON without changing PATH.
set "SAR_BOOT_PYTHON="
set "SAR_BOOT_SWITCH="
if defined SAR_PYTHON goto explicit_python
if exist "%~dp0..\.venv\Scripts\python.exe" set "SAR_BOOT_PYTHON=%~dp0..\.venv\Scripts\python.exe"
if defined SAR_BOOT_PYTHON exit /b 0
if defined CONDA_PREFIX for %%P in ("%CONDA_PREFIX%") do if /i "%%~nxP"=="gdal" if exist "%%~P\python.exe" set "SAR_BOOT_PYTHON=%%~P\python.exe"
if defined CONDA_PREFIX for %%P in ("%CONDA_PREFIX%") do if /i "%%~nxP"=="pytorch" if exist "%%~P\python.exe" set "SAR_BOOT_PYTHON=%%~P\python.exe"
if defined SAR_BOOT_PYTHON exit /b 0
if exist "%USERPROFILE%\.conda\environments.txt" for /f "usebackq delims=" %%P in ("%USERPROFILE%\.conda\environments.txt") do if /i "%%~nxP"=="gdal" if exist "%%~P\python.exe" set "SAR_BOOT_PYTHON=%%~P\python.exe"
if defined SAR_BOOT_PYTHON exit /b 0
if exist "%USERPROFILE%\.conda\environments.txt" for /f "usebackq delims=" %%P in ("%USERPROFILE%\.conda\environments.txt") do if /i "%%~nxP"=="pytorch" if exist "%%~P\python.exe" set "SAR_BOOT_PYTHON=%%~P\python.exe"
if defined SAR_BOOT_PYTHON exit /b 0
py -3.11 -c "import sys; assert sys.maxsize > 2**32" >nul 2>nul
if not errorlevel 1 goto launcher_311
py -3.12 -c "import sys; assert sys.maxsize > 2**32" >nul 2>nul
if not errorlevel 1 goto launcher_312
for /f "delims=" %%P in ('where python.exe 2^>nul ^| findstr /i /v WindowsApps') do if not defined SAR_BOOT_PYTHON if not exist "%%~dpPconda-meta" set "SAR_BOOT_PYTHON=%%P"
if defined SAR_BOOT_PYTHON exit /b 0
echo [ERROR] Python 3.11 or 3.12, 64-bit, was not found.
echo Install Python with Tcl/Tk support from https://www.python.org/downloads/windows/
echo Or set SAR_PYTHON to a Python executable in an existing gdal or pytorch environment.
exit /b 1

:launcher_311
set "SAR_BOOT_PYTHON=py.exe"
set "SAR_BOOT_SWITCH=-3.11"
exit /b 0

:launcher_312
set "SAR_BOOT_PYTHON=py.exe"
set "SAR_BOOT_SWITCH=-3.12"
exit /b 0

:explicit_python
if not exist "%SAR_PYTHON%" exit /b 1
set "SAR_BOOT_PYTHON=%SAR_PYTHON%"
exit /b 0
