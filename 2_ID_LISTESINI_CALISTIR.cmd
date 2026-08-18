@echo off
chcp 65001 >nul
title Kep Atlas Sirket Gorsel Botu - ID Listesi
cd /d "%~dp0"
powershell.exe -NoProfile -ExecutionPolicy Bypass -File "%~dp0BASLAT.ps1"
set "KOD=%ERRORLEVEL%"
echo.
if not "%KOD%"=="0" echo Program hata koduyla durdu: %KOD%
if not "%KOD%"=="0" echo Yarim kalan isi surdurmek icin 3_KALDIGI_YERDEN_DEVAM.cmd dosyasini acin.
if "%KOD%"=="0" echo Gorsel islemi tamamlandi.
pause
exit /b %KOD%
