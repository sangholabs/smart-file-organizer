@echo off
REM ============================================================
REM  File Organizer - one-click setup for a NEW PC
REM  1) detect Python  2) install via winget if missing
REM  3) pip install required libraries  4) environment check
REM  ASCII only (Korean breaks cmd code page). Local-only, no data sent.
REM ============================================================
setlocal enableextensions
cd /d "%~dp0"
chcp 65001 >nul

echo ============================================================
echo   File Organizer - Setup
echo ============================================================
echo.

REM ---- 1) find a working Python ----------------------------
set "PYCMD="
py -3 --version >nul 2>&1 && set "PYCMD=py -3"
if not defined PYCMD (
  python --version >nul 2>&1 && set "PYCMD=python"
)

if defined PYCMD goto :have_python

echo [1/4] Python not found. Trying to install via winget...
where winget >nul 2>&1
if errorlevel 1 (
  echo.
  echo   winget is not available on this PC.
  echo   Please install Python 3 manually:
  echo     https://www.python.org/downloads/
  echo   IMPORTANT: check "Add python.exe to PATH" during install,
  echo   then run this 설치.bat again.
  echo.
  pause
  exit /b 1
)

winget install -e --id Python.Python.3.12 --source winget ^
  --accept-package-agreements --accept-source-agreements
echo.
echo   Python installed. You may need to CLOSE this window and
echo   run 설치.bat again so PATH refreshes.
echo.

REM try to detect again in this same session
py -3 --version >nul 2>&1 && set "PYCMD=py -3"
if not defined PYCMD (
  python --version >nul 2>&1 && set "PYCMD=python"
)
if not defined PYCMD (
  echo   Could not detect Python in this session.
  echo   Close this window and double-click 설치.bat once more.
  pause
  exit /b 1
)

:have_python
for /f "delims=" %%v in ('%PYCMD% --version 2^>^&1') do set "PYVER=%%v"
echo [1/4] Python found: %PYVER%
echo.

REM ---- 2) upgrade pip --------------------------------------
echo [2/4] Upgrading pip...
%PYCMD% -m pip install --upgrade pip
echo.

REM ---- 3) install required libraries -----------------------
echo [3/4] Installing required libraries (requirements.txt)...
%PYCMD% -m pip install -r requirements.txt
if errorlevel 1 (
  echo.
  echo   Some libraries failed to install. Core dedup still works;
  echo   optional features may be disabled. See messages above.
  echo.
)
echo.

REM ---- 3b) optional: chromaprint (fpcalc) for precise audio matching ----
echo [3b/4] Optional: Chromaprint (precise audio fingerprint)...
where winget >nul 2>&1
if errorlevel 1 (
  echo   winget not found - skipping. Audio still works via local fingerprint.
) else (
  winget install -e --id AcoustID.Chromaprint --accept-package-agreements --accept-source-agreements
  echo   (If it failed, that's OK - audio falls back to local fingerprint.)
)
echo.

REM ---- 4) environment check --------------------------------
echo [4/4] Environment check:
%PYCMD% main.py doctor
echo.

echo ============================================================
echo   Setup done.
echo   Start the program:  double-click  파일정리_실행.vbs
echo ============================================================
echo.
pause
endlocal
