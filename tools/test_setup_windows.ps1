$ErrorActionPreference = 'Stop'
. "$PSScriptRoot\setup_windows.ps1"

function Assert($Condition, $Message) {
    if (-not $Condition) { throw $Message }
}

Assert ((Get-OllamaUrl '') -eq 'http://127.0.0.1:11434') 'Default Ollama address'
Assert ((Get-OllamaUrl '0.0.0.0:12434') -eq 'http://127.0.0.1:12434') 'Custom bind port'
Assert ((Get-OllamaUrl 'http://127.0.0.1:12434/') -eq 'http://127.0.0.1:12434') 'URL form'
Assert (Test-VersionAtLeast '2.1.285 (Claude Code)' $MinClaudeVersion) 'Newer Claude Code passes the minimum'
Assert (-not (Test-VersionAtLeast '2.0.1 (Claude Code)' $MinClaudeVersion)) 'Older Claude Code is refused'
Assert (-not (Test-VersionAtLeast 'unknown' $MinClaudeVersion)) 'An unreadable version is refused'
$folders = @(Get-LocalDataDirs 'C:\Temp\BalloonLens')
Assert ($folders.Count -eq 4 -and @($folders | Where-Object { $_ -like '*claude-config' }).Count -eq 1) 'Subscription folders are listed'

$python = Join-Path (Split-Path $PSScriptRoot) 'helper\.venv\Scripts\python.exe'
$failed = $false
try { Invoke-Checked $python @('-c', 'import sys; sys.exit(7)') } catch { $failed = $true }
Assert $failed 'Failed commands must stop installation'
Invoke-Checked $python @('-c', 'pass')
Assert (Test-Dependencies $python) 'Existing environment must be reusable'
Assert (-not (Test-Dependencies $python 'package_that_does_not_exist_123')) 'Missing dependency must be detected'
$report = Get-StatusReport | ConvertFrom-Json
Assert ($report.root -and $null -ne $report.deps -and $null -ne $report.nativeHost -and $null -ne $report.helper -and $null -ne $report.claude.installed) 'Status report has the fields the setup window reads'
Assert ((Get-PortOwner 49997) -eq 'free') 'A port nobody uses reads as free'
Write-Output 'Installer checks: 12 passed'
