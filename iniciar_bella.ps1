param(
    [ValidateSet("Start", "Stop", "Restart", "Status")]
    [string]$Action = "Start"
)

$ErrorActionPreference = "Stop"
$OutputEncoding = [Console]::OutputEncoding = [System.Text.UTF8Encoding]::new($false)

$projectRoot = $PSScriptRoot
$escapedRoot = [Regex]::Escape($projectRoot)

function Get-BellaProcesses {
    return @(
        Get-CimInstance Win32_Process -ErrorAction SilentlyContinue |
            Where-Object {
                $_.ProcessId -ne $PID -and
                $_.CommandLine -and
                $_.CommandLine -match $escapedRoot -and
                (
                    ($_.Name -match '^python(w)?(\.exe)?$' -and $_.CommandLine -match 'bot\.py') -or
                    ($_.Name -match '^(powershell|pwsh)(\.exe)?$' -and $_.CommandLine -match 'run_bot\.ps1')
                )
            }
    )
}

function Get-BellaBotProcesses {
    return @(
        Get-BellaProcesses |
            Where-Object { $_.Name -match '^python(w)?(\.exe)?$' }
    )
}

function Stop-Bella {
    $running = Get-BellaProcesses
    if ($running.Count -eq 0) {
        Write-Host "A Bella já está desligada." -ForegroundColor Yellow
        return
    }

    $pythonProcesses = @($running | Where-Object { $_.Name -match '^python' })
    $runnerProcesses = @($running | Where-Object { $_.Name -match '^(powershell|pwsh)' })
    foreach ($process in @($pythonProcesses + $runnerProcesses)) {
        try {
            Stop-Process -Id $process.ProcessId -ErrorAction Stop
        }
        catch {
            Stop-Process -Id $process.ProcessId -Force -ErrorAction SilentlyContinue
        }
    }

    $deadline = (Get-Date).AddSeconds(8)
    while ((Get-BellaProcesses).Count -gt 0 -and (Get-Date) -lt $deadline) {
        Start-Sleep -Milliseconds 250
    }
    foreach ($process in (Get-BellaProcesses)) {
        Stop-Process -Id $process.ProcessId -Force -ErrorAction SilentlyContinue
    }

    Write-Host "Bella desligada com segurança." -ForegroundColor Green
}

function Start-Bella {
    $running = Get-BellaProcesses
    if ($running.Count -gt 0) {
        Write-Host "A Bella já está ligada. PID: $($running[0].ProcessId)" -ForegroundColor Green
        return
    }

    Set-Location -LiteralPath $projectRoot
    $logsPath = Join-Path $projectRoot "logs"
    New-Item -ItemType Directory -Path $logsPath -Force | Out-Null
    $stdoutPath = Join-Path $logsPath "bella-output.log"
    $stderrPath = Join-Path $logsPath "bella-error.log"

    foreach ($path in @($stdoutPath, $stderrPath)) {
        if ((Test-Path -LiteralPath $path) -and (Get-Item -LiteralPath $path).Length -gt 0) {
            $previousPath = [System.IO.Path]::ChangeExtension($path, ".previous.log")
            Copy-Item -LiteralPath $path -Destination $previousPath -Force
            Clear-Content -LiteralPath $path
        }
    }

    $runnerPath = Join-Path $projectRoot "run_bot.ps1"
    $process = Start-Process `
        -FilePath "powershell.exe" `
        -ArgumentList @(
            "-NoLogo",
            "-NoProfile",
            "-ExecutionPolicy",
            "Bypass",
            "-File",
            "`"$runnerPath`""
        ) `
        -WorkingDirectory $projectRoot `
        -WindowStyle Hidden `
        -RedirectStandardOutput $stdoutPath `
        -RedirectStandardError $stderrPath `
        -PassThru

    $deadline = (Get-Date).AddSeconds(60)
    while ((Get-BellaBotProcesses).Count -eq 0 -and (Get-Date) -lt $deadline) {
        if ($process.HasExited) {
            break
        }
        Start-Sleep -Milliseconds 500
        $process.Refresh()
    }
    if ((Get-BellaBotProcesses).Count -eq 0) {
        $details = ""
        if (Test-Path -LiteralPath $stderrPath) {
            $details = (Get-Content -LiteralPath $stderrPath -Tail 12 -ErrorAction SilentlyContinue) -join [Environment]::NewLine
        }
        throw "A Bella não conseguiu iniciar.$([Environment]::NewLine)$details"
    }

    Start-Sleep -Seconds 3

    $botProcess = (Get-BellaBotProcesses)[0]
    Write-Host "Bella iniciada em segundo plano. Processo do bot: $($botProcess.ProcessId)" -ForegroundColor Green
    Write-Host "Você pode fechar o inicializador sem desligar a Bella."
}

switch ($Action) {
    "Start" { Start-Bella }
    "Stop" { Stop-Bella }
    "Restart" {
        Stop-Bella
        Start-Sleep -Seconds 1
        Start-Bella
    }
    "Status" {
        $running = Get-BellaProcesses
        if ($running.Count -gt 0) {
            Write-Host "LIGADA|$($running[0].ProcessId)"
        }
        else {
            Write-Host "DESLIGADA|"
        }
    }
}
