@echo off
chcp 65001 >nul
cd /d "%~dp0"
title Build EXE

set "PYEXE="
where py >nul 2>nul && set "PYEXE=py"
if not defined PYEXE where python >nul 2>nul && set "PYEXE=python"
if not defined PYEXE (
  echo [ERROR] Python not found.
  pause & exit /b 1
)

echo Installing PyInstaller and optional libraries (if needed)...
"%PYEXE%" -m pip install --disable-pip-version-check pyinstaller send2trash Pillow pypdf pillow-heif olefile py7zr imageio-ffmpeg rapidocr-onnxruntime PyMuPDF rawpy numpy

REM Locate fpcalc (chromaprint) to bundle for precise audio matching (optional)
set "FPCALC_ARG="
for /f "delims=" %%p in ('where fpcalc 2^>nul') do set "FPCALC=%%p"
if not defined FPCALC if exist "%LOCALAPPDATA%\Microsoft\WinGet\Links\fpcalc.exe" set "FPCALC=%LOCALAPPDATA%\Microsoft\WinGet\Links\fpcalc.exe"
if defined FPCALC (
  echo Bundling fpcalc: %FPCALC%
  set "FPCALC_ARG=--add-binary "%FPCALC%;.""
) else (
  echo fpcalc not found - exe will use local audio fingerprint only.
)

echo Building exe...
"%PYEXE%" -m PyInstaller --noconfirm --onefile --windowed --name "파일정리" --collect-submodules organizer --hidden-import send2trash --collect-all PIL --collect-all pypdf --collect-all pillow_heif --collect-all olefile --collect-all py7zr --collect-all imageio_ffmpeg --collect-all rapidocr_onnxruntime --collect-all fitz --collect-all rawpy --collect-all numpy %FPCALC_ARG% app.py

echo.
echo Done. EXE is at: dist\파일정리.exe
pause
