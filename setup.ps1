$OutputEncoding = [Console]::OutputEncoding = [System.Text.UTF8Encoding]::new($false)
$env:PYTHONUTF8 = "1"
$env:PYTHONIOENCODING = "utf-8"

if (!(Test-Path ".venv")) {
    py -m venv .venv
}

if (!(Test-Path ".env")) {
    Copy-Item ".env.example" ".env"
    Write-Host "Arquivo .env criado. Preencha DISCORD_TOKEN antes de iniciar."
}

if (!(Test-Path "data")) {
    New-Item -ItemType Directory -Path "data" | Out-Null
}
if (!(Test-Path "data\traffic.ndjson")) {
    New-Item -ItemType File -Path "data\traffic.ndjson" | Out-Null
}

.\.venv\Scripts\python.exe tools\normalize_utf8.py
if ($LASTEXITCODE -ne 0) {
    throw "Falha ao normalizar os arquivos do projeto para UTF-8."
}

.\.venv\Scripts\python.exe -m pip install --upgrade pip
.\.venv\Scripts\python.exe -m pip install -r requirements.txt
.\.venv\Scripts\python.exe -m playwright install chromium

if ($LASTEXITCODE -ne 0) {
    throw "Falha ao preparar o Chromium do monitoramento."
}

Set-Content -Path ".venv\playwright-ready.txt" -Value "ready"
Write-Host "Ambiente preparado. Configure o arquivo .env e execute .\run_bot.ps1."
