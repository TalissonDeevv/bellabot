# Configura a FreeLLMAPI do servidor pelo PowerShell, sem abrir o painel:
#   1. cria a conta admin (ou faz login se ela já existir);
#   2. cadastra as chaves da Groq e do Gemini que já estão no .env da Bella;
#   3. grava a chave unificada no .env da Bella, liga a FreeLLMAPI e reinicia.
# E-mail e senha são digitados aqui e vão direto para o servidor pelo túnel SSH.
#
# Uso:  powershell -ExecutionPolicy Bypass -File deploy\freellmapi\configurar_freellmapi.ps1 -Ip 1.2.3.4
param(
    [Parameter(Mandatory = $true)][string]$Ip,
    [string]$Usuario = "bella",
    [string]$ChaveSsh = (Join-Path $env:USERPROFILE ".ssh\bella_contabo"),
    [int]$PortaLocal = 3001
)
$ErrorActionPreference = "Stop"
$alvo = "$Usuario@$Ip"
$sshBase = @("-i", $ChaveSsh, "-o", "BatchMode=yes")

function Invoke-Remoto([string]$comando, [string]$entrada = $null) {
    if ($null -ne $entrada) { $saida = $entrada | & ssh @sshBase $alvo $comando }
    else { $saida = & ssh @sshBase $alvo $comando }
    if ($LASTEXITCODE -ne 0) { throw "Falha no servidor ($LASTEXITCODE): $comando" }
    return $saida
}

function Invoke-Api([string]$metodo, [string]$caminho, $corpo = $null, [string]$token = $null) {
    $headers = @{}
    if ($token) { $headers["Authorization"] = "Bearer $token" }
    $params = @{ Method = $metodo; Uri = "$base$caminho"; Headers = $headers; ContentType = "application/json" }
    if ($null -ne $corpo) { $params["Body"] = [Text.Encoding]::UTF8.GetBytes(($corpo | ConvertTo-Json -Compress)) }
    try { return Invoke-RestMethod @params }
    catch {
        $detalhe = $_.ErrorDetails.Message
        $status = $null
        if ($_.Exception.Response) { $status = [int]$_.Exception.Response.StatusCode }
        throw [Exception]::new("HTTP $status em $caminho : $detalhe")
    }
}

# ---- túnel até a FreeLLMAPI (só existe em 127.0.0.1 no servidor)
$portaRemota = (Invoke-Remoto "grep -oP '^FREELLMAPI_PORT=\K[0-9]+' ~/bellabot/deploy/freellmapi/.env || echo 3001" | Select-Object -Last 1).Trim()
$base = "http://127.0.0.1:$PortaLocal"
$tunel = $null
try { Invoke-RestMethod "$base/api/ping" -TimeoutSec 5 | Out-Null }
catch {
    Write-Host "Abrindo túnel SSH na porta $PortaLocal..."
    $tunel = Start-Process ssh -PassThru -WindowStyle Hidden -ArgumentList @(
        "-i", $ChaveSsh, "-o", "BatchMode=yes", "-o", "ExitOnForwardFailure=yes",
        "-N", "-L", "${PortaLocal}:127.0.0.1:$portaRemota", $alvo)
    Start-Sleep -Seconds 4
    Invoke-RestMethod "$base/api/ping" -TimeoutSec 10 | Out-Null
}

try {
    # ---- 1. conta admin
    $status = Invoke-Api GET "/api/auth/status"
    $email = Read-Host "E-mail do admin da FreeLLMAPI"
    $senhaSegura = Read-Host "Senha (mínimo 8 caracteres)" -AsSecureString
    $bstr = [Runtime.InteropServices.Marshal]::SecureStringToBSTR($senhaSegura)
    try { $senha = [Runtime.InteropServices.Marshal]::PtrToStringBSTR($bstr) }
    finally { [Runtime.InteropServices.Marshal]::ZeroFreeBSTR($bstr) }
    if ($senha.Length -lt 8) { throw "A senha precisa ter pelo menos 8 caracteres." }

    if ($status.needsSetup) {
        Write-Host "Criando a conta..."
        $codigo = (Invoke-Remoto "sudo docker logs bella-freellmapi 2>&1 | grep -oP 'First-run setup code: \K\S+' | tail -1" | Select-Object -Last 1)
        $corpo = @{ email = $email; password = $senha }
        if ($codigo) { $corpo["setupCode"] = $codigo.Trim() }
        $token = (Invoke-Api POST "/api/auth/setup" $corpo).token
    } else {
        Write-Host "A conta já existe; fazendo login..."
        $token = (Invoke-Api POST "/api/auth/login" @{ email = $email; password = $senha }).token
    }
    $senha = $null
    if (-not $token) { throw "O servidor não devolveu a sessão." }
    Write-Host "Conta ok."

    # ---- 2. chaves dos provedores, lidas do .env da Bella no servidor
    $linhas = Invoke-Remoto "grep -E '^(GROQ_API_KEY|GEMINI_API_KEY)=' ~/bellabot/.env || true"
    $chaves = @{}
    foreach ($linha in @($linhas)) {
        if ($linha -match '^([A-Z_]+)=(.+)$') { $chaves[$Matches[1]] = $Matches[2].Trim().Trim('"') }
    }
    $existentes = @()
    try { $existentes = @(Invoke-Api GET "/api/keys" -token $token) | ForEach-Object { $_.platform } } catch { }
    foreach ($par in @(@("groq", "GROQ_API_KEY", "Groq"), @("google", "GEMINI_API_KEY", "Gemini"))) {
        $plataforma, $variavel, $nome = $par
        if (-not $chaves[$variavel]) { Write-Host "  $nome : sem chave no .env, pulando."; continue }
        if ($existentes -contains $plataforma) { Write-Host "  $nome : já cadastrada."; continue }
        try {
            Invoke-Api POST "/api/keys" @{ platform = $plataforma; key = $chaves[$variavel]; label = "bella" } $token | Out-Null
            Write-Host "  $nome : cadastrada."
        } catch { Write-Host "  $nome : falhou ($($_.Exception.Message))" }
    }
    $chaves = $null

    # ---- 3. chave unificada -> .env da Bella, liga e reinicia
    $unificada = (Invoke-Api GET "/api/settings/api-key" -token $token).apiKey
    if (-not $unificada) { throw "Não consegui ler a chave unificada." }
    if ($unificada -notmatch '^[A-Za-z0-9_\-]+$') { throw "Chave unificada em formato inesperado." }
    # O script vai pelo stdin do ssh (nada de chave na linha de comando, nada de
    # aspas quebradas pelo PowerShell 5.1, só quebras de linha do Linux).
    $scriptRemoto = @"
k='$unificada'
cd ~/bellabot
set_env() { if grep -q "^`$1=" .env; then sed -i "s#^`$1=.*#`$1=`$2#" .env; else printf '%s=%s\n' "`$1" "`$2" >> .env; fi; }
set_env FREELLMAPI_API_KEY "`$k"
set_env FREELLMAPI_ENABLED true
sudo systemctl restart bella-bot
"@ -replace "`r", ""
    Invoke-Remoto "bash -s" $scriptRemoto | Out-Null
    $scriptRemoto = $null
    $unificada = $null
    Write-Host "Bella reiniciada. Conferindo..."
    Start-Sleep -Seconds 12
    $log = Invoke-Remoto "sudo journalctl -u bella-bot --since '-1min' --no-pager | grep -E 'IA via FreeLLMAPI|Bot online' | tail -2"
    $log | ForEach-Object { Write-Host "  $_" }
    Write-Host ""
    Write-Host "Pronto. A Bella agora usa a FreeLLMAPI (com a Groq direta de reserva)."
    Write-Host "Para mais provedores, abra http://localhost:$PortaLocal com o túnel e use a página Keys."
}
finally {
    if ($tunel) { Stop-Process -Id $tunel.Id -ErrorAction SilentlyContinue }
}
