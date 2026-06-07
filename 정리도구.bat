@echo off
chcp 65001 >nul
cd /d "%~dp0"
title File Organizer

set "PYEXE="
where py >nul 2>nul && set "PYEXE=py"
if not defined PYEXE where python >nul 2>nul && set "PYEXE=python"
if not defined PYEXE (
  echo [ERROR] Python not found. Install Python or add it to PATH.
  pause
  exit /b 1
)

"%PYEXE%" menu.py
pause
