<#
.SYNOPSIS
    Starts the Candidate Screening & Integrity Console (Streamlit) from its virtual environment.

.DESCRIPTION
    Runs "python -m streamlit run app.py" from the project folder, in the foreground, until the
    app is stopped (Ctrl+C, or the service manager). Besides the port and address below it always
    passes:
      --server.headless true            no browser window and no first-run prompt
      --server.fileWatcherType none     no reloading when files change (restart after an upgrade)
      --browser.gatherUsageStats false  no usage statistics
    Every other setting comes from .streamlit/config.toml or from STREAMLIT_* environment
    variables (for example STREAMLIT_SERVER_BASE_URL_PATH). See docs/DEPLOYMENT.md.

.PARAMETER VenvPath
    Folder of the virtual environment created by scripts\setup.ps1.
    Default: <user profile>\.venvs\hiring-screening

.PARAMETER Port
    TCP port to listen on. Default: 8501.

.PARAMETER Address
    Network interface to listen on. 127.0.0.1 (default) accepts connections from this machine
    only, which is what a reverse proxy on the same machine needs. 0.0.0.0 accepts connections
    from other machines: restrict the port with the firewall, because the app has no login.

.PARAMETER PublicHost
    Host name that users type in the browser, for example screening.example.internal. Sets
    browser.serverAddress (used for the printed URL and the origin checks) and stops Streamlit
    from looking up the machine's public IP address at startup.

.EXAMPLE
    .\scripts\run_app.ps1
    Local use: open http://127.0.0.1:8501 on this machine.

.EXAMPLE
    .\scripts\run_app.ps1 -VenvPath C:\apps\venvs\hiring-screening -Address 0.0.0.0 -PublicHost screening-vm.example.internal
    Intranet use: other machines open http://screening-vm.example.internal:8501
#>
[CmdletBinding()]
param(
    [string]$VenvPath = (Join-Path ([Environment]::GetFolderPath('UserProfile')) '.venvs\hiring-screening'),
    [ValidateRange(1, 65535)]
    [int]$Port = 8501,
    [ValidateNotNullOrEmpty()]
    [string]$Address = '127.0.0.1',
    [string]$PublicHost = ''
)

Set-StrictMode -Version 2.0
$ErrorActionPreference = 'Stop'

function Stop-WithError([string]$Message) {
    Write-Host "ERROR: $Message" -ForegroundColor Red
    exit 1
}

$root = Split-Path -Parent $PSScriptRoot
$venvFull = $ExecutionContext.SessionState.Path.GetUnresolvedProviderPathFromPSPath($VenvPath).TrimEnd('\')
$python = Join-Path $venvFull 'Scripts\python.exe'

if (-not (Test-Path -LiteralPath (Join-Path $root 'app.py') -PathType Leaf)) {
    Stop-WithError "app.py not found in $root."
}
if (-not (Test-Path -LiteralPath $python -PathType Leaf)) {
    Stop-WithError "No virtual environment at $venvFull. Run scripts\setup.ps1 first, or pass -VenvPath."
}

# Fail early, naming the owner, when the port is taken (Streamlit itself only says "in use").
$listener = $null
try {
    $listener = Get-NetTCPConnection -State Listen -LocalPort $Port -ErrorAction Stop | Select-Object -First 1
} catch {
    $listener = $null   # nothing is listening, or the cmdlet is not available on this system
}
if ($null -ne $listener) {
    $owner = Get-Process -Id $listener.OwningProcess -ErrorAction SilentlyContinue
    $ownerName = if ($owner) { $owner.ProcessName } else { 'unknown' }
    Stop-WithError "Port $Port is already in use by process $($listener.OwningProcess) ($ownerName). Stop it or choose another -Port."
}

$streamlitArgs = @(
    '-m', 'streamlit', 'run', 'app.py',
    '--server.port', "$Port",
    '--server.address', $Address,
    '--server.headless', 'true',
    '--server.fileWatcherType', 'none',
    '--browser.gatherUsageStats', 'false'
)
if ($PublicHost) { $streamlitArgs += @('--browser.serverAddress', $PublicHost) }

$env:PYTHONUNBUFFERED = '1'   # log lines reach service log files immediately
Write-Host "Starting the app from $root (listening on ${Address}:$Port). Press Ctrl+C to stop."

# The app must run from the project folder: .streamlit/ (config and secrets) and the .cache/
# folder are resolved from the working directory.
Push-Location -LiteralPath $root
try {
    $ErrorActionPreference = 'Continue'   # Streamlit logs to stderr; that is not a script error
    & $python @streamlitArgs
    $exitCode = $LASTEXITCODE
} finally {
    Pop-Location
}
exit $exitCode
