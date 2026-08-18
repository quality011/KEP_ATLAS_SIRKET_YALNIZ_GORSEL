@echo off
chcp 65001 >nul
title Kep Atlas Sirket Gorsel Botu - Kurulum
cd /d "%~dp0"
powershell.exe -NoProfile -ExecutionPolicy Bypass -File "%~dp0KURULUM.ps1"
set "KOD=%ERRORLEVEL%"
echo.
if not "%KOD%"=="0" echo Kurulum tamamlanamadi. Yukaridaki hata mesajini kontrol edin.
if "%KOD%"=="0" echo Kurulum tamamlandi. 2_ID_LISTESINI_CALISTIR.cmd dosyasini acabilirsiniz.
pause
exit /b %KOD%
