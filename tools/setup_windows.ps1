param([switch]$CheckOnly, [switch]$WithLocal, [switch]$SkipModel, [switch]$StatusJson)

# Minimum Claude Code version: `auth status` reports configDirectory from this version on (see claude_cli.py).
$MinClaudeVersion = [version]'2.1.268'

function Invoke-Checked {
    param([string]$Executable, [string[]]$Arguments)
    & $Executable @Arguments
    if ($LASTEXITCODE -ne 0) { throw "Command failed ($LASTEXITCODE): $Executable $($Arguments -join ' ')" }
}

function Get-OllamaUrl([string]$Address) {
    if ([string]::IsNullOrWhiteSpace($Address)) { return 'http://127.0.0.1:11434' }
    $Address = $Address.Trim().TrimEnd('/')
    if ($Address -notmatch '^https?://') { $Address = "http://$Address" }
    $uri = [uri]$Address
    $builder = New-Object System.UriBuilder($uri)
    if ($uri.Host -in @('0.0.0.0', '[::]', '::')) { $builder.Host = '127.0.0.1' }
    if ($uri.IsDefaultPort -and $Address -notmatch ':\d+$') { $builder.Port = 11434 }
    return $builder.Uri.AbsoluteUri.TrimEnd('/')
}

function Test-VersionAtLeast([string]$Text, [version]$Minimum) {
    if ($Text -notmatch '(\d+\.\d+\.\d+)') { return $false }
    return ([version]$Matches[1]) -ge $Minimum
}

function Get-DataDir {
    if ($env:BALLOONLENS_DATA) { return $env:BALLOONLENS_DATA }
    return (Join-Path $env:LOCALAPPDATA 'BalloonLens')
}

# Folders the subscription backends use. Created here so the first call never has to guess their state.
function Get-LocalDataDirs([string]$DataDir) {
    return @(
        (Join-Path $DataDir 'claude-config'),
        (Join-Path $DataDir 'sandbox'),
        (Join-Path $DataDir 'runtime')
    )
}

function Test-Dependencies([string]$Python, [string]$Module = '') {
    $ErrorActionPreference = 'Continue'
    if (-not (Test-Path -LiteralPath $Python)) { return $false }
    $probe = @'
import importlib, sys
if sys.argv[1] != '-':
    importlib.import_module(sys.argv[1])
else:
    for name in ['torch','torchvision','transformers','easyocr','manga_ocr','fugashi','unidic_lite','jaconv','fastapi','uvicorn','numpy','PIL','cv2','pytest','httpx']:
        importlib.import_module(name)
'@
    # A sentinel avoids PowerShell 5 dropping an empty native argument.
    $argument = if ($Module) { $Module } else { '-' }
    & $Python -c $probe $argument *> $null
    return $LASTEXITCODE -eq 0
}

function Find-Python311 {
    $ErrorActionPreference = 'Continue'
    $candidates = @((Join-Path $env:LOCALAPPDATA 'Programs\Python\Python311\python.exe'))
    $command = Get-Command python.exe -ErrorAction SilentlyContinue
    if ($command) { $candidates += $command.Source }
    $launcher = Get-Command py.exe -ErrorAction SilentlyContinue
    if ($launcher) {
        $located = & $launcher.Source -3.11 -c 'import sys; print(sys.executable)' 2>$null
        if ($LASTEXITCODE -eq 0) { $candidates += $located }
    }
    foreach ($candidate in $candidates) {
        if (Test-Path -LiteralPath $candidate) {
            & $candidate -c 'import sys; sys.exit(0 if sys.version_info[:2] == (3,11) else 1)' *> $null
            if ($LASTEXITCODE -eq 0) { return $candidate }
        }
    }
}

function Install-WingetPackage([string]$Id) {
    $winget = Get-Command winget.exe -ErrorAction SilentlyContinue
    if (-not $winget) { throw "winget is missing. Install Microsoft App Installer, then rerun Setup.cmd. Required package: $Id" }
    Invoke-Checked $winget.Source @('install', '--id', $Id, '--exact', '--accept-package-agreements', '--accept-source-agreements', '--silent')
}

function Find-Ollama {
    $command = Get-Command ollama.exe -ErrorAction SilentlyContinue
    if ($command) { return $command.Source }
    $path = Join-Path $env:LOCALAPPDATA 'Programs\Ollama\ollama.exe'
    if (Test-Path -LiteralPath $path) { return $path }
}

function Find-Claude {
    $command = Get-Command claude.exe -ErrorAction SilentlyContinue
    if ($command) { return $command.Source }
    $local = Join-Path $env:USERPROFILE '.local\bin\claude.exe'
    if (Test-Path -LiteralPath $local) { return $local }
}

# Reads the CLI's own status. Never calls a model, so it uses no subscription usage.
function Get-ClaudeReport([string]$Path) {
    $report = [pscustomobject]@{ installed = $false; version = ''; versionOk = $false; loggedIn = $false; note = '' }
    if (-not $Path) {
        $report.note = 'Claude Code가 없어요. 설치: https://claude.com/claude-code'
        return $report
    }
    $report.installed = $true
    $ErrorActionPreference = 'Continue'
    $report.version = (& $Path --version | Out-String).Trim()
    $report.versionOk = Test-VersionAtLeast $report.version $MinClaudeVersion
    $previous = $env:CLAUDE_CONFIG_DIR
    $env:CLAUDE_CONFIG_DIR = Join-Path (Get-DataDir) 'claude-config'
    try {
        $info = (& $Path auth status | Out-String) | ConvertFrom-Json
        $report.loggedIn = [bool]$info.loggedIn -and ($info.authMethod -in @('claude.ai', 'oauth_token'))
        if ($info.authMethod -in @('api_key', 'api_key_helper')) { $report.note = 'API 키로 로그인돼 있어요. 구독 로그인으로 바꿔 주세요.' }
    } catch {
        $report.note = '로그인 상태를 읽지 못했어요.'
    } finally {
        $env:CLAUDE_CONFIG_DIR = $previous
    }
    return $report
}

function Get-HelperStatus([string]$Url) {
    $pair = $null
    try {
        $pair = Invoke-RestMethod "$Url/v1/pair" -Method Post -Headers @{Origin='chrome-extension://pnccemlpjodglelglkjjdnnmdknejgop'} -TimeoutSec 3
        return Invoke-RestMethod "$Url/v1/status" -Headers @{'x-balloonlens-token'=$pair.token} -TimeoutSec 5
    } catch { return $null } finally { $pair = $null }
}

function Write-BackendReport($Claude) {
    if (-not $Claude.installed) { Write-Host "[!] $($Claude.note)" -ForegroundColor Yellow }
    elseif (-not $Claude.versionOk) { Write-Host "[!] Claude Code 2.1.268 이상이 필요해요. 지금: $($Claude.version)" -ForegroundColor Yellow }
    else { Write-Host "[ok] Claude Code $($Claude.version) · 로그인 $(if ($Claude.loggedIn) { '됨' } else { '필요' })" }
}

# Progress lines for the setup window (tools/setup_gui.py reads "[step i/n] ..."). They show what is happening now.
function Write-Step([int]$Index, [int]$Total, [string]$Text) {
    Write-Host "[step $Index/$Total] $Text"
}

function Start-BalloonLensSetup {
    param([switch]$CheckOnly, [switch]$WithLocal, [switch]$SkipModel)
    $ErrorActionPreference = 'Stop'
    $root = Split-Path $PSScriptRoot
    $helper = Join-Path $root 'helper'
    $python = Join-Path $helper '.venv\Scripts\python.exe'
    if ($CheckOnly) {
        if (-not (Test-Dependencies $python)) { throw 'Python dependencies are missing or outdated. Run Setup.cmd.' }
        Write-Host '[ok] Python dependencies available.'
        Write-BackendReport (Get-ClaudeReport (Find-Claude))
        Write-Host "[i] Ollama (로컬 모델, 선택 사항): $(if (Find-Ollama) { '설치됨' } else { '없음' })"
        Write-Host '[ok] Check-only made no installation changes.'
        return
    }
    Write-Step 1 6 'Python 환경 준비'
    if (-not (Test-Path -LiteralPath $python)) {
        $basePython = Find-Python311
        if (-not $basePython) { Install-WingetPackage 'Python.Python.3.11'; $basePython = Find-Python311 }
        if (-not $basePython) { throw 'Python 3.11 was not found after installation. Reopen Setup.cmd or install Python 3.11 from python.org.' }
        Invoke-Checked $basePython @('-m', 'venv', (Join-Path $helper '.venv'))
    }
    Write-Step 2 6 '번역 패키지 확인과 설치 (처음에는 수 GB라서 여러 분 걸려요)'
    $nvidia = Get-Command nvidia-smi.exe -ErrorAction SilentlyContinue
    $gpu = $false
    if ($nvidia) { & $nvidia.Source -L *> $null; $gpu = $LASTEXITCODE -eq 0 }
    $depsReady = Test-Dependencies $python
    $cudaReady = $true
    if ($gpu) { & $python -c 'import importlib.util,sys; sys.exit(0 if importlib.util.find_spec(''torch'') and __import__(''torch'').cuda.is_available() else 1)' *> $null; $cudaReady = $LASTEXITCODE -eq 0 }
    if (-not $depsReady -or -not $cudaReady) {
        $index = if ($gpu) { 'https://download.pytorch.org/whl/cu124' } else { 'https://download.pytorch.org/whl/cpu' }
        $torchArgs = @('-m', 'pip', 'install', 'torch==2.6.0', 'torchvision==0.21.0', '--index-url', $index)
        if (-not $cudaReady) { $torchArgs += '--force-reinstall' }
        Invoke-Checked $python $torchArgs
        Invoke-Checked $python @('-m', 'pip', 'install', '-r', (Join-Path $helper 'requirements.txt'))
        if (-not (Test-Dependencies $python)) { throw 'Dependency imports failed. Check the pip errors above and rerun Setup.cmd.' }
        if ($gpu) { Invoke-Checked $python @('-c', 'import torch; assert torch.cuda.is_available(), ''Update NVIDIA driver for CUDA 12.4 and rerun Setup.cmd''') }
    }
    foreach ($dir in (Get-LocalDataDirs (Get-DataDir))) { New-Item -ItemType Directory -Force -Path $dir | Out-Null }

    # Local models are optional since v0.5.0: only -WithLocal installs Ollama and the default local model.
    if ($WithLocal) {
        $ollama = Find-Ollama
        if (-not $ollama) { Install-WingetPackage 'Ollama.Ollama'; $ollama = Find-Ollama }
        if (-not $ollama) { throw 'Ollama was not found. Install from https://ollama.com/download and rerun Setup.cmd.' }
        $freshHost = [Environment]::GetEnvironmentVariable('OLLAMA_HOST', 'User')
        if ($null -ne $freshHost) { $env:OLLAMA_HOST = $freshHost }
        $ollamaUrl = Get-OllamaUrl $env:OLLAMA_HOST
        $tags = $null
        try { $tags = Invoke-RestMethod "$ollamaUrl/api/tags" -TimeoutSec 3 } catch { }
        if ($null -eq $tags) {
            Start-Process -FilePath $ollama -ArgumentList @('serve') -WindowStyle Hidden
            for ($i=0; $i -lt 30 -and $null -eq $tags; $i++) {
                Start-Sleep -Seconds 1
                try { $tags = Invoke-RestMethod "$ollamaUrl/api/tags" -TimeoutSec 2 } catch { }
            }
            if ($null -eq $tags) { throw "Ollama did not respond at $ollamaUrl. Check OLLAMA_HOST and the Ollama app." }
        }
        if (-not $SkipModel -and 'qwen3.5:9b' -notin @($tags.models.name)) {
            Write-Host 'Downloading qwen3.5:9b locally. This may take several minutes.'
            Invoke-Checked $ollama @('pull', 'qwen3.5:9b')
        }
    }

    Write-Step 3 6 '크롬 자동 실행 등록'
    Invoke-Checked 'powershell.exe' @('-NoProfile', '-ExecutionPolicy', 'Bypass', '-File', (Join-Path $PSScriptRoot 'install_windows.ps1'))
    Write-Step 4 6 '도우미 켜기와 확인'
    $port = if ($env:BALLOONLENS_PORT) { $env:BALLOONLENS_PORT } else { '8787' }
    $url = "http://127.0.0.1:$port"
    $status = Get-HelperStatus $url
    if (-not $status.ok) {
        $pythonw = Join-Path $helper '.venv\Scripts\pythonw.exe'
        Start-Process -FilePath $pythonw -ArgumentList @('-m', 'balloonlens') -WorkingDirectory $helper -WindowStyle Hidden
        for ($i=0; $i -lt 45 -and -not $status.ok; $i++) { Start-Sleep -Seconds 1; $status = Get-HelperStatus $url }
        if (-not $status.ok) {
            $owner = Get-PortOwner ([int]$port)
            if ($owner -like 'other:*') { throw "포트 $port 을 다른 프로그램($($owner.Substring(6)))이 쓰고 있어요. 그 프로그램을 끄고 Setup.cmd를 다시 실행하세요." }
            throw "도우미가 $url 에서 켜지지 않았어요. %LOCALAPPDATA%\BalloonLens\helper.log 를 확인하세요."
        }
    }
    Write-Step 5 6 'Claude 상태 확인'
    Write-BackendReport (Get-ClaudeReport (Find-Claude))
    Write-Step 6 6 '끝'
    Write-Host "[ok] Setup complete. Chrome > Extensions > Developer mode > Load unpacked: $(Join-Path $root 'extension')"
    Write-Host '구독 번역: 확장의 설정 화면에서 로그인 명령을 복사해 PowerShell에 붙여 넣으세요. 이 스크립트는 로그인하지 않아요.'
}

# What holds the helper port: 'free', 'balloonlens' (our helper answers pairing) or 'other:<process name>'.
function Get-PortOwner([int]$Port) {
    $listener = Get-NetTCPConnection -State Listen -LocalPort $Port -ErrorAction SilentlyContinue | Select-Object -First 1
    if (-not $listener) { return 'free' }
    try {
        $pair = Invoke-RestMethod "http://127.0.0.1:$Port/v1/pair" -Method Post -Headers @{Origin='chrome-extension://pnccemlpjodglelglkjjdnnmdknejgop'} -TimeoutSec 3
        if ($pair.token) { return 'balloonlens' }
    } catch { }
    $name = (Get-Process -Id $listener.OwningProcess -ErrorAction SilentlyContinue).ProcessName
    return "other:$name"
}

# Machine-readable status for the setup window (tools/setup_gui.py). Prints one JSON object and nothing else.
function Get-StatusReport {
    $root = Split-Path $PSScriptRoot
    $python = Join-Path $root 'helper\.venv\Scripts\python.exe'
    $port = if ($env:BALLOONLENS_PORT) { $env:BALLOONLENS_PORT } else { '8787' }
    [pscustomobject]@{
        root = $root
        dataDir = Get-DataDir
        extensionDir = Join-Path $root 'extension'
        deps = [bool](Test-Dependencies $python)
        nativeHost = [bool](Test-Path -LiteralPath 'HKCU:\Software\Google\Chrome\NativeMessagingHosts\com.balloonlens.helper')
        helper = ($null -ne (Get-HelperStatus "http://127.0.0.1:$port"))
        portOwner = Get-PortOwner ([int]$port)
        ollama = [bool](Find-Ollama)
        claudePath = Find-Claude
        claude = Get-ClaudeReport (Find-Claude)
    } | ConvertTo-Json -Depth 4
}

if ($MyInvocation.InvocationName -ne '.') {
    if ($StatusJson) { Get-StatusReport; exit 0 }
    try { Start-BalloonLensSetup -CheckOnly:$CheckOnly -WithLocal:$WithLocal -SkipModel:$SkipModel; exit 0 }
    catch { Write-Host "[x] $($_.Exception.Message)" -ForegroundColor Red; exit 1 }
}
