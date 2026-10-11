# Registers the BalloonLens native messaging host for the current user (no admin rights needed),
# so the Chrome extension can start the local helper by itself.
$ErrorActionPreference = "Stop"
$root = Split-Path -Parent $PSScriptRoot
$helper = Join-Path $root "helper"
$bat = Join-Path $helper "native\balloonlens_host.bat"
$python = Join-Path $helper ".venv\Scripts\python.exe"
$extId = "pnccemlpjodglelglkjjdnnmdknejgop"
$name = "com.balloonlens.helper"

if (-not (Test-Path $python)) {
  Write-Host "[x] helper\.venv not found. Create the Python environment first (see README)." -ForegroundColor Red
  exit 1
}

$dir = Join-Path $env:LOCALAPPDATA "BalloonLens\native"
New-Item -ItemType Directory -Force $dir | Out-Null
$manifestPath = Join-Path $dir "$name.json"
$manifest = [ordered]@{
  name            = $name
  description     = "BalloonLens local helper launcher"
  path            = $bat
  type            = "stdio"
  allowed_origins = @("chrome-extension://$extId/")
}
# Chrome's JSON reader does not accept a BOM, so write plain UTF-8.
[IO.File]::WriteAllText($manifestPath, ($manifest | ConvertTo-Json))

foreach ($browser in "Google\Chrome", "Chromium", "Microsoft\Edge") {
  $key = "HKCU:\Software\$browser\NativeMessagingHosts\$name"
  New-Item -Path $key -Force | Out-Null
  Set-Item -Path $key -Value $manifestPath
}
Write-Host "[ok] Native messaging host registered: $manifestPath" -ForegroundColor Green

if (-not (Get-Command ollama -ErrorAction SilentlyContinue)) {
  Write-Host "[!] Ollama not found. Install it from https://ollama.com/download" -ForegroundColor Yellow
}
Write-Host "Done. Go back to the BalloonLens settings page in Chrome."
