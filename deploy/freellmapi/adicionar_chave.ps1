# Cadastra a chave de mais um provedor na FreeLLMAPI do servidor, pelo PowerShell.
# A chave é digitada escondida (como senha) e vai direto para o servidor pelo túnel SSH;
# não fica no histórico do PowerShell nem em arquivo.
#
# Uso:
#   powershell -ExecutionPolicy Bypass -File deploy\freellmapi\adicionar_chave.ps1 -Ip 169.58.243.36 -Plataforma openrouter
#   powershell -ExecutionPolicy Bypass -File deploy\freellmapi\adicionar_chave.ps1 -Ip 169.58.243.36 -Listar
# Esqueceu a senha do painel? Acrescente -RedefinirSenha (usa o código que a FreeLLMAPI escreve no log).
param(
    [Parameter(Mandatory = $true)][string]$Ip,
    [ValidateSet("openrouter", "mistral", "nvidia", "cohere", "huggingface", "cloudflare",
                 "cerebras", "groq", "google", "llm7", "siliconflow", "requesty", "zhipu", "ovh", "kilo")]
    [string]$Plataforma,
    [switch]$Listar,
    [switch]$RedefinirSenha,
    [string]$Usuario = "bella",
    [string]$ChaveSsh = (Join-Path $env:USERPROFILE ".ssh\bella_contabo"),
    [int]$PortaLocal = 3001
)
$ErrorActionPreference = "Stop"
if (-not $Listar -and -not $Plataforma) { throw "Informe -Plataforma (ex.: -Plataforma openrouter) ou -Listar." }
$alvo = "$Usuario@$Ip"

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

function Read-Secreto([string]$pergunta) {
    $seguro = Read-Host $pergunta -AsSecureString
    $bstr = [Runtime.InteropServices.Marshal]::SecureStringToBSTR($seguro)
    try { return [Runtime.InteropServices.Marshal]::PtrToStringBSTR($bstr) }
    finally { [Runtime.InteropServices.Marshal]::ZeroFreeBSTR($bstr) }
}

# ---- túnel até a FreeLLMAPI (ela só escuta em 127.0.0.1 no servidor)
$base = "http://127.0.0.1:$PortaLocal"
$tunel = $null
try { Invoke-RestMethod "$base/api/ping" -TimeoutSec 5 | Out-Null }
catch {
    Write-Host "Abrindo túnel SSH na porta $PortaLocal..."
    $tunel = Start-Process ssh -PassThru -WindowStyle Hidden -ArgumentList @(
        "-i", $ChaveSsh, "-o", "BatchMode=yes", "-o", "ExitOnForwardFailure=yes",
        "-N", "-L", "${PortaLocal}:127.0.0.1:3001", $alvo)
    Start-Sleep -Seconds 4
    Invoke-RestMethod "$base/api/ping" -TimeoutSec 10 | Out-Null
}

try {
    if ($RedefinirSenha) {
        # Fluxo oficial da FreeLLMAPI: pede um código, que ela escreve no log do servidor.
        Invoke-Api POST "/api/auth/forgot-password" @{} | Out-Null
        Start-Sleep -Seconds 2
        $codigo = (& ssh -i $ChaveSsh -o BatchMode=yes $alvo "sudo docker logs --since 2m bella-freellmapi 2>&1 | grep -oP 'Password-reset code: \K\S+' | tail -1" | Select-Object -Last 1)
        if (-not $codigo) { throw "Não achei o código de redefinição no log do servidor." }
        $nova = Read-Secreto "Nova senha da FreeLLMAPI (mínimo 8 caracteres)"
        $confirma = Read-Secreto "Repita a nova senha"
        if ($nova -ne $confirma) { throw "As senhas não batem." }
        if ($nova.Length -lt 8) { throw "A senha precisa ter pelo menos 8 caracteres." }
        $resposta = Invoke-Api POST "/api/auth/reset-password" @{ resetCode = $codigo.Trim(); newPassword = $nova }
        $nova = $null; $confirma = $null; $codigo = $null
        $token = $resposta.token
        Write-Host "Senha redefinida. Conta: $($resposta.email)"
    } else {
        $email = Read-Host "E-mail do admin da FreeLLMAPI"
        $senha = Read-Secreto "Senha do admin da FreeLLMAPI"
        $token = (Invoke-Api POST "/api/auth/login" @{ email = $email; password = $senha }).token
        $senha = $null
    }
    if (-not $token) { throw "Login recusado." }

    # O PowerShell 5.1 devolve a lista JSON como um único objeto; desenrola aqui.
    $resposta = Invoke-Api GET "/api/keys" -token $token
    $chaves = @(foreach ($lista in @($resposta)) { foreach ($item in $lista) { $item } })
    Write-Host ""
    Write-Host "Chaves cadastradas hoje:"
    $chaves | Where-Object { $_.platform } | Group-Object platform | ForEach-Object { Write-Host ("  {0}: {1} chave(s)" -f $_.Name, $_.Count) }
    if ($Listar) { return }

    Write-Host ""
    if (@("ovh", "kilo") -contains $Plataforma) {
        # Provedores que funcionam sem conta: a FreeLLMAPI guarda um marcador "sem chave".
        if (@($chaves | Where-Object { $_.platform -eq $Plataforma }).Count -gt 0) { Write-Host "$Plataforma já estava ativa."; return }
        Invoke-Api POST "/api/keys" @{ platform = $Plataforma; label = "bella" } $token | Out-Null
        Write-Host "$Plataforma ativada sem chave."
        return
    }
    $chave = (Read-Secreto "Cole a chave da $Plataforma (fica escondida) e aperte Enter").Trim()
    if ($chave.Length -lt 10) { throw "Chave vazia ou curta demais." }
    Invoke-Api POST "/api/keys" @{ platform = $Plataforma; key = $chave; label = "bella" } $token | Out-Null
    $chave = $null
    Write-Host "Chave da $Plataforma cadastrada. A FreeLLMAPI já passa a usar, sem reiniciar a Bella."
}
finally {
    if ($tunel) { Stop-Process -Id $tunel.Id -ErrorAction SilentlyContinue }
}
