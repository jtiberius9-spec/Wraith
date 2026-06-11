@echo off
REM ============================================================
REM  Wraith - integrated scrcpy mirror + keymapper
REM  Double-click this. First run sets things up; after that it
REM  just opens the launcher.
REM ============================================================
setlocal
cd /d "%~dp0"

REM --- Python ---
where python >nul 2>nul && goto :have_python
py -3 --version >nul 2>nul && goto :have_python
call :install_python
where python >nul 2>nul || py -3 --version >nul 2>nul || (
  echo.
  echo Python was just installed. Close this window and double-click Start-Wraith.bat again.
  pause & exit /b 1
)
:have_python

REM --- venv + Python deps (first run only) ---
if not exist ".venv\Scripts\python.exe" (
  echo First run: building the Python environment, please wait...
  python -m venv .venv 2>nul || py -3 -m venv .venv
  ".venv\Scripts\python.exe" -m pip install -q --upgrade pip
  ".venv\Scripts\python.exe" -m pip install -q -r requirements.txt
)

REM --- scrcpy (+adb) required ---
where scrcpy >nul 2>nul || call :install_scrcpy
where scrcpy >nul 2>nul || (
  echo.
  echo scrcpy was just installed. Close this window and double-click Start-Wraith.bat again.
  pause & exit /b 1
)

REM --- ffmpeg (only needed for recording) ---
where ffmpeg >nul 2>nul || call :install_ffmpeg

".venv\Scripts\python.exe" -m wraith.launcher
exit /b %errorlevel%

:install_python
where winget >nul 2>nul || (echo Install Python 3.10+ from https://www.python.org/downloads/windows/ ^(check "Add to PATH"^). & pause & exit /b 1)
echo Installing Python via winget...
winget install --id Python.Python.3.12 --exact --source winget --accept-source-agreements --accept-package-agreements
exit /b 0

:install_scrcpy
where winget >nul 2>nul || (echo Install scrcpy from https://github.com/Genymobile/scrcpy/releases and add it to PATH. & pause & exit /b 1)
echo Installing scrcpy via winget...
winget install --id Genymobile.scrcpy --exact --source winget --accept-source-agreements --accept-package-agreements
exit /b 0

:install_ffmpeg
where winget >nul 2>nul || (echo ffmpeg not found - recording will not work without it. & exit /b 0)
echo Installing ffmpeg via winget...
winget install --id Gyan.FFmpeg --exact --source winget --accept-source-agreements --accept-package-agreements
exit /b 0
