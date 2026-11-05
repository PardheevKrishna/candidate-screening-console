<#
.SYNOPSIS
    Creates the Python virtual environment for the Candidate Screening & Integrity Console
    and installs its pinned dependencies.

.DESCRIPTION
    Steps:
      1. Finds Python 3.11 (64-bit): -PythonExe when given, otherwise the "py -3.11" launcher,
         otherwise python3.11 / python / python3 on PATH.
      2. Creates the virtual environment at -VenvPath, or reuses it when it already exists.
      3. Installs requirements.txt from the default index, from an internal mirror (-IndexUrl,
         -TrustedHost) or from a folder of pre-downloaded wheels (-WheelDir, no network needed).
      4. Runs "pip check" and the test suite (skip the tests with -NoTests).

    pip also honours its usual environment variables, for example HTTPS_PROXY (corporate proxy),
    PIP_INDEX_URL, PIP_TRUSTED_HOST and PIP_CERT (CA bundle when TLS traffic is inspected).
    Run with -WhatIf to print the steps without changing anything.
    Full installation guide: docs/DEPLOYMENT.md

.PARAMETER VenvPath
    Folder of the virtual environment. Keep it outside cloud-synced folders, network shares and
    the project folder, and create it on the machine that runs the app (a virtual environment
    cannot be copied to another machine). Default: <user profile>\.venvs\hiring-screening

.PARAMETER IndexUrl
    Package index to install from, for example an internal PyPI mirror
    (https://pypi.internal.example/simple).

.PARAMETER TrustedHost
    Host name of the package index to trust without TLS certificate verification. Use it only for
    an internal mirror served over plain HTTP or with a certificate pip cannot verify.

.PARAMETER WheelDir
    Install offline from this folder of wheels (pip --no-index --find-links).
    Cannot be combined with -IndexUrl.

.PARAMETER PythonExe
    Full path of the Python 3.11 (64-bit) interpreter used to create the virtual environment.

.PARAMETER NoTests
    Skip the test suite after installing.

.EXAMPLE
    powershell -NoProfile -ExecutionPolicy Bypass -File scripts\setup.ps1

.EXAMPLE
    .\scripts\setup.ps1 -VenvPath C:\apps\venvs\hiring-screening -IndexUrl https://pypi.internal.example/simple

.EXAMPLE
    .\scripts\setup.ps1 -VenvPath C:\apps\venvs\hiring-screening -WheelDir D:\install\wheelhouse

.EXAMPLE
    .\scripts\setup.ps1 -WhatIf
#>
[CmdletBinding(SupportsShouldProcess = $true)]
param(
    [string]$VenvPath = (Join-Path ([Environment]::GetFolderPath('UserProfile')) '.venvs\hiring-screening'),
    [string]$IndexUrl = '',
    [string]$TrustedHost = '',
    [string]$WheelDir = '',
    [string]$PythonExe = '',
    [switch]$NoTests
)

Set-StrictMode -Version 2.0
$ErrorActionPreference = 'Stop'

$RequiredMinor = 11   # requirements.txt is pinned and tested on Python 3.11

function Write-Step([string]$Message) {
    Write-Host "==> $Message"
}

function Stop-WithError([string]$Message) {
    Write-Host "ERROR: $Message" -ForegroundColor Red
    exit 1
}

function Hide-Credential([string]$Text) {
    # Never print user:password embedded in an index URL.
    return ($Text -replace '://[^/@\s]+@', '://***@')
}

function Get-PythonInfo([string]$Exe, [string[]]$PrefixArgs) {
    # Version and pointer size of an interpreter, or $null when it cannot be run.
    $ErrorActionPreference = 'Continue'
    $probe = "import sys, struct; print('%d.%d.%d %d' % (sys.version_info[0], sys.version_info[1], sys.version_info[2], struct.calcsize('P') * 8))"
    try {
        $out = & $Exe @PrefixArgs -c $probe 2>$null
    } catch {
        return $null
    }
    if ($LASTEXITCODE -ne 0 -or -not $out) { return $null }
    $parts = ([string](@($out)[-1])).Trim().Split(' ')
    if ($parts.Count -ne 2) { return $null }
    try {
        return [pscustomobject]@{ Version = [version]$parts[0]; Bits = [int]$parts[1] }
    } catch {
        return $null
    }
}

function Find-Python {
    $candidates = New-Object System.Collections.Generic.List[object]
    if ($PythonExe) {
        $candidates.Add(@($PythonExe))
    } else {
        if (Get-Command py -CommandType Application -ErrorAction SilentlyContinue) {
            $candidates.Add(@('py', "-3.$RequiredMinor"))
        }
        foreach ($name in @("python3.$RequiredMinor", 'python', 'python3')) {
            foreach ($cmd in @(Get-Command $name -CommandType Application -ErrorAction SilentlyContinue)) {
                # Skip the Microsoft Store placeholder: it offers to install Python instead of running it.
                if ($cmd.Source -notmatch '\\WindowsApps\\') { $candidates.Add(@($cmd.Source)) }
            }
        }
    }
    $seen = @()
    foreach ($candidate in $candidates) {
        $exe = [string]$candidate[0]
        $prefix = @($candidate | Select-Object -Skip 1)
        $label = (@($exe) + $prefix) -join ' '
        $info = Get-PythonInfo $exe $prefix
        if ($null -eq $info) { continue }
        $seen += "$label = Python $($info.Version) ($($info.Bits)-bit)"
        if ($info.Bits -ne 64 -or $info.Version.Major -ne 3) { continue }
        if ($info.Version.Minor -eq $RequiredMinor) {
            return [pscustomobject]@{ Exe = $exe; Prefix = $prefix; Version = $info.Version; Label = $label }
        }
        if ($PythonExe -and $info.Version.Minor -gt $RequiredMinor) {
            Write-Warning "Python $($info.Version) is not the tested version (3.$RequiredMinor); continuing because -PythonExe was given."
            return [pscustomobject]@{ Exe = $exe; Prefix = $prefix; Version = $info.Version; Label = $label }
        }
    }
    $detail = if ($seen.Count) { ' Found: ' + ($seen -join '; ') + '.' } else { ' No Python interpreter was found.' }
    Stop-WithError ("Python 3.$RequiredMinor (64-bit) is required.$detail Install it for all users " +
                    '(python.org installer or the company software portal) or pass -PythonExe <path>.')
}

function Test-Pip([string]$Python) {
    $ErrorActionPreference = 'Continue'
    & $Python -m pip --version *> $null
    return ($LASTEXITCODE -eq 0)
}

# ---------------------------------------------------------------------------
# Checks
# ---------------------------------------------------------------------------
$root = Split-Path -Parent $PSScriptRoot
$requirements = Join-Path $root 'requirements.txt'
if (-not (Test-Path -LiteralPath $requirements -PathType Leaf)) {
    Stop-WithError "requirements.txt not found in $root."
}
if ($WheelDir -and $IndexUrl) {
    Stop-WithError 'Use either -WheelDir (offline install) or -IndexUrl (package mirror), not both.'
}
$wheelFull = ''
if ($WheelDir) {
    $wheelFull = $ExecutionContext.SessionState.Path.GetUnresolvedProviderPathFromPSPath($WheelDir)
    if (-not (Test-Path -LiteralPath $wheelFull -PathType Container)) { Stop-WithError "Wheel folder not found: $wheelFull" }
}

$venvFull = $ExecutionContext.SessionState.Path.GetUnresolvedProviderPathFromPSPath($VenvPath).TrimEnd('\')
$venvPython = Join-Path $venvFull 'Scripts\python.exe'

foreach ($synced in @($env:OneDrive, $env:OneDriveCommercial, $env:OneDriveConsumer)) {
    if ($synced -and ($venvFull + '\').StartsWith($synced.TrimEnd('\') + '\', [System.StringComparison]::OrdinalIgnoreCase)) {
        Write-Warning ("$venvFull is inside a cloud-synced folder. Syncing locks and corrupts virtual environments; " +
                       'use a local folder such as C:\apps\venvs\hiring-screening.')
        break
    }
}
if (($venvFull + '\').StartsWith($root.TrimEnd('\') + '\', [System.StringComparison]::OrdinalIgnoreCase)) {
    Write-Warning 'The virtual environment is inside the project folder. Keep it outside, so it is never copied or replaced together with the code.'
}

# ---------------------------------------------------------------------------
# 1-2. Python and the virtual environment
# ---------------------------------------------------------------------------
if (Test-Path -LiteralPath $venvPython -PathType Leaf) {
    Write-Step "Using the existing virtual environment at $venvFull"
    $info = Get-PythonInfo $venvPython @()
    if ($null -eq $info) {
        Stop-WithError ("The virtual environment at $venvFull does not start (was its base Python removed or moved?). " +
                        'Delete that folder and run this script again.')
    }
    if ($info.Version.Minor -ne $RequiredMinor) {
        Write-Warning "It uses Python $($info.Version); requirements.txt is tested on Python 3.$RequiredMinor."
    }
} else {
    $python = Find-Python
    $baseExe = [string]$python.Exe
    $prefix = @($python.Prefix)
    Write-Step "Creating the virtual environment at $venvFull with $($python.Label) (Python $($python.Version))"
    if ($PSCmdlet.ShouldProcess($venvFull, 'Create virtual environment')) {
        $parent = Split-Path -Parent $venvFull
        if ($parent -and -not (Test-Path -LiteralPath $parent)) {
            New-Item -ItemType Directory -Path $parent -Force | Out-Null
        }
        & $baseExe @prefix -m venv $venvFull
        if ($LASTEXITCODE -ne 0) { Stop-WithError "Creating the virtual environment failed (exit code $LASTEXITCODE)." }
        if (-not (Test-Path -LiteralPath $venvPython -PathType Leaf)) {
            Stop-WithError "No interpreter found at $venvPython after creating the virtual environment."
        }
    }
}
$venvReady = Test-Path -LiteralPath $venvPython -PathType Leaf

if ($venvReady -and -not (Test-Pip $venvPython)) {
    Write-Step 'pip is missing in the virtual environment; adding it with ensurepip (works offline)'
    if ($PSCmdlet.ShouldProcess($venvFull, 'python -m ensurepip')) {
        & $venvPython -m ensurepip --upgrade --default-pip
        if ($LASTEXITCODE -ne 0) { Stop-WithError "Could not install pip into $venvFull." }
    }
}

# ---------------------------------------------------------------------------
# 3. Dependencies
# ---------------------------------------------------------------------------
$pipArgs = @('-m', 'pip', 'install', '--disable-pip-version-check', '--requirement', $requirements)
if ($wheelFull) {
    $pipArgs += @('--no-index', '--find-links', $wheelFull)
} elseif ($IndexUrl) {
    $pipArgs += @('--index-url', $IndexUrl)
}
if ($TrustedHost) { $pipArgs += @('--trusted-host', $TrustedHost) }

Write-Step ('Installing dependencies: python ' + (Hide-Credential ($pipArgs -join ' ')))
if ($PSCmdlet.ShouldProcess($venvFull, 'pip install --requirement requirements.txt')) {
    & $venvPython @pipArgs
    if ($LASTEXITCODE -ne 0) {
        Stop-WithError ("pip install failed (exit code $LASTEXITCODE). See 'Troubleshooting' in docs/DEPLOYMENT.md " +
                        '(proxy, certificates, package mirror, offline wheels).')
    }
    & $venvPython -m pip check
    if ($LASTEXITCODE -ne 0) { Stop-WithError 'pip check reported inconsistent dependencies (see above).' }
}

# ---------------------------------------------------------------------------
# 4. Tests
# ---------------------------------------------------------------------------
if ($NoTests) {
    Write-Step 'Skipping the test suite (-NoTests).'
} elseif ($PSCmdlet.ShouldProcess($root, 'Run the test suite (python -m pytest)')) {
    Write-Step 'Running the test suite (about a minute)'
    Push-Location -LiteralPath $root
    try {
        & $venvPython -m pytest -p no:cacheprovider
        $testExit = $LASTEXITCODE
    } finally {
        Pop-Location
    }
    if ($testExit -ne 0) {
        Stop-WithError "The test suite failed (exit code $testExit). Fix the problems above before starting the app."
    }
}

if ($WhatIfPreference) {
    Write-Step 'Dry run (-WhatIf): nothing was changed.'
    exit 0
}
Write-Step 'Done.'
Write-Host ''
Write-Host 'Start the app with:'
Write-Host ('  powershell -NoProfile -ExecutionPolicy Bypass -File "{0}" -VenvPath "{1}"' -f (Join-Path $root 'scripts\run_app.ps1'), $venvFull)
Write-Host 'Add -Address 0.0.0.0 to accept connections from other machines (read "Network and security" in docs/DEPLOYMENT.md first).'
exit 0
