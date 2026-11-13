$OutputEncoding = [Console]::OutputEncoding = [System.Text.UTF8Encoding]::new($false)
$env:PYTHONUTF8 = "1"
$env:PYTHONIOENCODING = "utf-8"

$ProjectRoot = (Get-Location).Path
$VenvPath = Join-Path $ProjectRoot ".venv"
$VenvPython = Join-Path $VenvPath "Scripts\python.exe"

function New-BellaVenv {
    if (Get-Command py -ErrorAction SilentlyContinue) {
        & py -3.12 -m venv ".venv"
        if ($LASTEXITCODE -eq 0) {
            return
        }
        Write-Host "[notice] Não consegui criar a venv com Python 3.12 via py. Tentando python..."
    }
    & python -m venv ".venv"
    if ($LASTEXITCODE -ne 0) {
        throw "Falha ao criar o ambiente virtual Python."
    }
}

function Test-BellaVenv {
    if (!(Test-Path -LiteralPath $VenvPython)) {
        return $false
    }
    & $VenvPython -m pip --version *> $null
    return $LASTEXITCODE -eq 0
}

function Reset-BellaVenv {
    if (Test-Path -LiteralPath $VenvPath) {
        $resolvedRoot = (Resolve-Path -LiteralPath $ProjectRoot).Path
        $resolvedVenv = (Resolve-Path -LiteralPath $VenvPath).Path
        if (!$resolvedVenv.StartsWith($resolvedRoot, [System.StringComparison]::OrdinalIgnoreCase)) {
            throw "Caminho da .venv fora da pasta do projeto. Remoção cancelada."
        }
        Write-Host "[notice] Recriando .venv corrompida/incompleta..."
        Remove-Item -LiteralPath $resolvedVenv -Recurse -Force
    }
    New-BellaVenv
    & $VenvPython -m ensurepip --upgrade
    if ($LASTEXITCODE -ne 0) {
        throw "Falha ao preparar o pip dentro da .venv."
    }
    & $VenvPython -m pip install --upgrade pip setuptools wheel
    if ($LASTEXITCODE -ne 0) {
        throw "Falha ao atualizar pip/setuptools/wheel dentro da .venv."
    }
}

$escapedRoot = [Regex]::Escape($ProjectRoot)
$runningBots = Get-CimInstance Win32_Process |
    Where-Object {
        $_.Name -match '^python(\.exe)?$' -and
        $_.CommandLine -match $escapedRoot -and
        $_.CommandLine -match 'bot\.py'
    }
foreach ($process in $runningBots) {
    Write-Host "[notice] Encerrando instância antiga da Bella: PID $($process.ProcessId)"
    Stop-Process -Id $process.ProcessId -Force -ErrorAction SilentlyContinue
}

if (!(Test-BellaVenv)) {
    Reset-BellaVenv
}

if (!(Test-Path ".env")) {
    Copy-Item ".env.example" ".env"
    throw "Arquivo .env criado. Preencha DISCORD_TOKEN e execute novamente."
}

if (!(Test-Path "data")) {
    New-Item -ItemType Directory -Path "data" | Out-Null
}
if (!(Test-Path "data\traffic.ndjson")) {
    New-Item -ItemType File -Path "data\traffic.ndjson" | Out-Null
}

& $VenvPython tools\normalize_utf8.py
if ($LASTEXITCODE -ne 0) {
    throw "Falha ao normalizar os arquivos do projeto para UTF-8."
}

$requirementsStream = [System.IO.File]::OpenRead((Join-Path $ProjectRoot "requirements.txt"))
$requirementsHasher = [System.Security.Cryptography.SHA256]::Create()
try {
    $requirementsHash = [System.BitConverter]::ToString(
        $requirementsHasher.ComputeHash($requirementsStream)
    ).Replace("-", "")
}
finally {
    $requirementsStream.Dispose()
    $requirementsHasher.Dispose()
}
$requirementsMarker = Join-Path $VenvPath "requirements.sha256"
$installedHash = if (Test-Path -LiteralPath $requirementsMarker) {
    (Get-Content -LiteralPath $requirementsMarker -Raw).Trim()
}
else {
    ""
}
if ($installedHash -ne $requirementsHash) {
    Write-Host "[notice] Instalando ou atualizando dependências..."
    & $VenvPython -m pip install -r requirements.txt
    if ($LASTEXITCODE -ne 0) {
        throw "Falha ao instalar as dependências do projeto."
    }
    Set-Content -LiteralPath $requirementsMarker -Value $requirementsHash -Encoding ASCII
}
else {
    Write-Host "[notice] Dependências já estão atualizadas."
}
if (!(Test-Path ".venv\playwright-ready.txt")) {
    & $VenvPython -m playwright install chromium
    if ($LASTEXITCODE -ne 0) {
        throw "Falha ao instalar o Chromium usado no monitoramento das paginas."
    }
    Set-Content -Path ".venv\playwright-ready.txt" -Value "ready"
}
& $VenvPython bot.py
