# Removes the BalloonLens native messaging host registration for the current user.
$name = "com.balloonlens.helper"
foreach ($browser in "Google\Chrome", "Chromium", "Microsoft\Edge") {
  Remove-Item -Path "HKCU:\Software\$browser\NativeMessagingHosts\$name" -Force -ErrorAction SilentlyContinue
}
Remove-Item -Path (Join-Path $env:LOCALAPPDATA "BalloonLens\native\$name.json") -Force -ErrorAction SilentlyContinue
Write-Host "[ok] Native messaging host removed. Cache and settings in %LOCALAPPDATA%\BalloonLens were kept."
