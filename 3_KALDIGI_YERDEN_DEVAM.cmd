@echo off
chcp 65001 >nul
title Kep Atlas Sirket Gorsel Botu - Devam
cd /d "%~dp0"
powershell.exe -NoProfile -ExecutionPolicy Bypass -File "%~dp0DEVAM.ps1"
set "KOD=%ERRORLEVEL%"
echo.
if not "%KOD%"=="0" echo Devam islemi hata koduyla durdu: %KOD%
if "%KOD%"=="0" echo Devam islemi tamamlandi.
pause
exit /b %KOD%
