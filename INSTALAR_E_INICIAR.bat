@echo off
setlocal
cd /d "%~dp0"

if not exist ".venv\Scripts\python.exe" (
  powershell -NoProfile -ExecutionPolicy Bypass -File ".\setup.ps1"
  if errorlevel 1 (
    echo.
    echo Falha durante a instalacao.
    pause
    exit /b 1
  )
)

findstr /B /C:"DISCORD_TOKEN=coloque_o_token_do_bot_aqui" ".env" >nul 2>&1
if not errorlevel 1 (
  echo.
  echo A instalacao foi concluida. Preencha DISCORD_TOKEN no arquivo .env.
  start "" notepad.exe ".env"
  pause
  exit /b 0
)

powershell -NoProfile -ExecutionPolicy Bypass -File ".\run_bot.ps1"
if errorlevel 1 (
  echo.
  echo O bot foi encerrado com erro. Confira a mensagem acima.
  pause
)
