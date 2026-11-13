@echo off
title Bella Nyelle - Encerrar
cd /d "%~dp0"
powershell.exe -NoLogo -NoProfile -ExecutionPolicy Bypass -File "%~dp0iniciar_bella.ps1" -Action Stop
echo.
pause
