$ErrorActionPreference = "Stop"
$OutputEncoding = [Console]::OutputEncoding = [System.Text.UTF8Encoding]::new($false)

Add-Type -AssemblyName System.Windows.Forms
Add-Type -AssemblyName System.Drawing

$createdNew = $false
$managerMutex = New-Object System.Threading.Mutex($true, "Local\BellaNyelleManager", [ref]$createdNew)
if (-not $createdNew) {
    exit 0
}

$projectRoot = $PSScriptRoot
$controlScript = Join-Path $projectRoot "iniciar_bella.ps1"
$logsPath = Join-Path $projectRoot "logs"
$escapedRoot = [Regex]::Escape($projectRoot)

function Get-BellaStatus {
    $running = @(
        Get-CimInstance Win32_Process -ErrorAction SilentlyContinue |
            Where-Object {
                $_.CommandLine -and
                $_.CommandLine -match $escapedRoot -and
                (
                    ($_.Name -match '^python(w)?(\.exe)?$' -and $_.CommandLine -match 'bot\.py') -or
                    ($_.Name -match '^(powershell|pwsh)(\.exe)?$' -and $_.CommandLine -match 'run_bot\.ps1')
                )
            }
    )
    if ($running.Count -gt 0) {
        return @{ Running = $true; Pid = $running[0].ProcessId }
    }
    return @{ Running = $false; Pid = $null }
}

function Invoke-BellaAction([string]$action) {
    $startInfo = [System.Diagnostics.ProcessStartInfo]::new()
    $startInfo.FileName = "powershell.exe"
    $startInfo.Arguments = "-NoLogo -NoProfile -ExecutionPolicy Bypass -File `"$controlScript`" -Action $action"
    $startInfo.WorkingDirectory = $projectRoot
    $startInfo.UseShellExecute = $false
    $startInfo.CreateNoWindow = $true
    $startInfo.RedirectStandardOutput = $true
    $startInfo.RedirectStandardError = $true
    $process = [System.Diagnostics.Process]::Start($startInfo)
    $output = $process.StandardOutput.ReadToEnd()
    $errorOutput = $process.StandardError.ReadToEnd()
    $process.WaitForExit()
    if ($process.ExitCode -ne 0) {
        throw (($errorOutput + [Environment]::NewLine + $output).Trim())
    }
    return $output.Trim()
}

$form = New-Object System.Windows.Forms.Form
$form.Text = "Acordar a Bella"
$form.Size = New-Object System.Drawing.Size(540, 390)
$form.StartPosition = "CenterScreen"
$form.FormBorderStyle = "FixedSingle"
$form.MaximizeBox = $false
$form.BackColor = [System.Drawing.Color]::FromArgb(18, 14, 18)
$form.ForeColor = [System.Drawing.Color]::WhiteSmoke

$iconPath = Join-Path $projectRoot "assets\bella-launcher.ico"
if (Test-Path -LiteralPath $iconPath) {
    $form.Icon = New-Object System.Drawing.Icon($iconPath)
}

$title = New-Object System.Windows.Forms.Label
$title.Text = "Central da Bella"
$title.Font = New-Object System.Drawing.Font("Segoe UI Semibold", 22)
$title.AutoSize = $true
$title.Location = New-Object System.Drawing.Point(28, 24)
$form.Controls.Add($title)

$subtitle = New-Object System.Windows.Forms.Label
$subtitle.Text = "Ligue, reinicie ou desligue a Bella sem abrir o VS Code."
$subtitle.Font = New-Object System.Drawing.Font("Segoe UI", 10)
$subtitle.ForeColor = [System.Drawing.Color]::FromArgb(190, 180, 188)
$subtitle.AutoSize = $true
$subtitle.Location = New-Object System.Drawing.Point(31, 70)
$form.Controls.Add($subtitle)

$statusPanel = New-Object System.Windows.Forms.Panel
$statusPanel.Location = New-Object System.Drawing.Point(30, 108)
$statusPanel.Size = New-Object System.Drawing.Size(462, 76)
$statusPanel.BackColor = [System.Drawing.Color]::FromArgb(34, 25, 31)
$form.Controls.Add($statusPanel)

$statusDot = New-Object System.Windows.Forms.Label
$statusDot.Text = "●"
$statusDot.Font = New-Object System.Drawing.Font("Segoe UI", 17)
$statusDot.AutoSize = $true
$statusDot.Location = New-Object System.Drawing.Point(18, 20)
$statusPanel.Controls.Add($statusDot)

$statusLabel = New-Object System.Windows.Forms.Label
$statusLabel.Font = New-Object System.Drawing.Font("Segoe UI Semibold", 12)
$statusLabel.AutoSize = $true
$statusLabel.Location = New-Object System.Drawing.Point(54, 16)
$statusPanel.Controls.Add($statusLabel)

$statusDetail = New-Object System.Windows.Forms.Label
$statusDetail.Font = New-Object System.Drawing.Font("Segoe UI", 9)
$statusDetail.ForeColor = [System.Drawing.Color]::FromArgb(175, 165, 173)
$statusDetail.AutoSize = $true
$statusDetail.Location = New-Object System.Drawing.Point(56, 42)
$statusPanel.Controls.Add($statusDetail)

function New-ActionButton([string]$text, [int]$x, [int]$y, [int]$width, [System.Drawing.Color]$color) {
    $button = New-Object System.Windows.Forms.Button
    $button.Text = $text
    $button.Location = New-Object System.Drawing.Point($x, $y)
    $button.Size = New-Object System.Drawing.Size($width, 42)
    $button.FlatStyle = "Flat"
    $button.FlatAppearance.BorderSize = 0
    $button.BackColor = $color
    $button.ForeColor = [System.Drawing.Color]::White
    $button.Font = New-Object System.Drawing.Font("Segoe UI Semibold", 10)
    $button.Cursor = [System.Windows.Forms.Cursors]::Hand
    $form.Controls.Add($button)
    return $button
}

$startButton = New-ActionButton "Iniciar" 30 208 142 ([System.Drawing.Color]::FromArgb(27, 150, 91))
$restartButton = New-ActionButton "Reiniciar" 190 208 142 ([System.Drawing.Color]::FromArgb(92, 81, 201))
$stopButton = New-ActionButton "Fechar Bella" 350 208 142 ([System.Drawing.Color]::FromArgb(196, 54, 75))
$logsButton = New-ActionButton "Abrir logs" 30 266 222 ([System.Drawing.Color]::FromArgb(58, 50, 57))
$folderButton = New-ActionButton "Abrir pasta do bot" 270 266 222 ([System.Drawing.Color]::FromArgb(58, 50, 57))

$hint = New-Object System.Windows.Forms.Label
$hint.Text = 'Fechar esta janela não desliga a Bella. Use "Fechar Bella" quando quiser encerrá-la.'
$hint.Font = New-Object System.Drawing.Font("Segoe UI", 8.5)
$hint.ForeColor = [System.Drawing.Color]::FromArgb(155, 145, 153)
$hint.AutoSize = $true
$hint.Location = New-Object System.Drawing.Point(31, 326)
$form.Controls.Add($hint)

function Update-StatusView {
    $status = Get-BellaStatus
    if ($status.Running) {
        $statusDot.ForeColor = [System.Drawing.Color]::FromArgb(62, 220, 135)
        $statusLabel.Text = "Bella está ligada"
        $statusDetail.Text = "Funcionando em segundo plano — processo $($status.Pid)"
        $startButton.Enabled = $false
        $restartButton.Enabled = $true
        $stopButton.Enabled = $true
    }
    else {
        $statusDot.ForeColor = [System.Drawing.Color]::FromArgb(230, 93, 111)
        $statusLabel.Text = "Bella está desligada"
        $statusDetail.Text = "Clique em Iniciar para acordá-la."
        $startButton.Enabled = $true
        $restartButton.Enabled = $false
        $stopButton.Enabled = $false
    }
}

function Run-Action([string]$action, [string]$progressText) {
    $form.UseWaitCursor = $true
    $startButton.Enabled = $false
    $restartButton.Enabled = $false
    $stopButton.Enabled = $false
    $statusLabel.Text = $progressText
    $statusDetail.Text = "Aguarde alguns segundos..."
    [System.Windows.Forms.Application]::DoEvents()
    try {
        Invoke-BellaAction $action | Out-Null
    }
    catch {
        [System.Windows.Forms.MessageBox]::Show(
            $_.Exception.Message,
            "Não foi possível controlar a Bella",
            [System.Windows.Forms.MessageBoxButtons]::OK,
            [System.Windows.Forms.MessageBoxIcon]::Error
        ) | Out-Null
    }
    finally {
        $form.UseWaitCursor = $false
        Update-StatusView
    }
}

$startButton.Add_Click({ Run-Action "Start" "Acordando a Bella..." })
$restartButton.Add_Click({ Run-Action "Restart" "Reiniciando a Bella..." })
$stopButton.Add_Click({
    $answer = [System.Windows.Forms.MessageBox]::Show(
        "Deseja realmente fechar a Bella?",
        "Fechar Bella",
        [System.Windows.Forms.MessageBoxButtons]::YesNo,
        [System.Windows.Forms.MessageBoxIcon]::Question
    )
    if ($answer -eq [System.Windows.Forms.DialogResult]::Yes) {
        Run-Action "Stop" "Fechando a Bella..."
    }
})
$logsButton.Add_Click({
    New-Item -ItemType Directory -Path $logsPath -Force | Out-Null
    Start-Process explorer.exe -ArgumentList "`"$logsPath`""
})
$folderButton.Add_Click({ Start-Process explorer.exe -ArgumentList "`"$projectRoot`"" })

$timer = New-Object System.Windows.Forms.Timer
$timer.Interval = 5000
$timer.Add_Tick({ Update-StatusView })
$form.Add_Shown({ Update-StatusView; $timer.Start() })
$form.Add_FormClosed({
    $timer.Stop()
    $timer.Dispose()
    if ($managerMutex) {
        $managerMutex.ReleaseMutex()
        $managerMutex.Dispose()
    }
})

[void]$form.ShowDialog()
