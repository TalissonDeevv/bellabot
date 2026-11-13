@echo off
title Bella Nyelle - Reiniciar
cd /d "%~dp0"
powershell.exe -NoLogo -NoProfile -ExecutionPolicy Bypass -File "%~dp0iniciar_bella.ps1" -Action Restart
echo.
pause
