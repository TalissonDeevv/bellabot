# Gera o pacote PRIVADO com o que não vai para o repositório: .env (tokens e
# chaves) e a pasta data (bancos da Bella). Mande para quem vai hospedar por um
# canal privado (DM, pendrive, Drive com acesso restrito). Nunca no GitHub.
#
# A Bella deste PC é desligada antes, para os bancos não mudarem no meio da
# cópia e para ela não rodar em dois lugares com o mesmo token.
$ErrorActionPreference = "Stop"
$root = (Resolve-Path (Join-Path $PSScriptRoot "..")).Path
Set-Location $root

if (-not (Test-Path -LiteralPath (Join-Path $root ".env"))) { throw "Não achei o .env em $root." }

Write-Host "Desligando a Bella deste PC..."
powershell.exe -NoLogo -NoProfile -ExecutionPolicy Bypass -File (Join-Path $root "iniciar_bella.ps1") -Action Stop

$output = Join-Path ([Environment]::GetFolderPath("Desktop")) "bella-dados.tar.gz"
if (Test-Path -LiteralPath $output) { Remove-Item -LiteralPath $output -Force }

$items = @(".env", "data")
if (Test-Path -LiteralPath (Join-Path $root "deploy\freellmapi\.env")) { $items += "deploy/freellmapi/.env" }

& tar.exe -czf $output --exclude="*.corrupt-*" --exclude="*.tmp" -C $root @items
if ($LASTEXITCODE -ne 0) { throw "O tar falhou ($LASTEXITCODE)." }

$size = [math]::Round((Get-Item -LiteralPath $output).Length / 1MB, 1)
Write-Host ""
Write-Host "Pronto: $output ($size MB)"
Write-Host "Contém tokens e chaves: envie só por canal privado."
Write-Host "A Bella ficou DESLIGADA neste PC. Ela volta quando subir no servidor."
Write-Host "Se precisar ligar aqui de novo: LIGAR_BELLA.bat"
