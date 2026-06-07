@echo off
chcp 65001 >nul
cd /d "%~dp0"
title Self Test

set "PYEXE="
where py >nul 2>nul && set "PYEXE=py"
if not defined PYEXE where python >nul 2>nul && set "PYEXE=python"
if not defined PYEXE (
  echo [ERROR] Python not found.
  pause & exit /b 1
)

set PYTHONUTF8=1
"%PYEXE%" tests\test_organizer.py
echo.
pause
