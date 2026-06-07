@echo off
cd /d "%~dp0"

set "PYW="
where pythonw >nul 2>nul && set "PYW=pythonw"
if not defined PYW for /f "delims=" %%i in ('py -c "import sys,os;print(os.path.join(os.path.dirname(sys.executable),'pythonw.exe'))" 2^>nul') do set "PYW=%%i"
if not defined PYW (
  echo [ERROR] pythonw not found. Please check your Python installation.
  pause
  exit /b 1
)

start "" "%PYW%" "app.py"
