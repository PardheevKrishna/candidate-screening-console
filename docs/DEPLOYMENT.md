# Deploying on a company VM

This guide installs the Candidate Screening & Integrity Console on a virtual machine inside the
company network, so recruiters can use it from their browsers on the intranet. It covers Windows
Server and Linux, installs with or without internet access, running the app as a service, and the
day-to-day tasks: health checks, retention, backups and upgrades.

Values in angle brackets, such as `<DOMAIN\svc-screening>` or `<screening.example.internal>`, are
placeholders for your environment. The examples use the layout below. Any local folders work,
but keep them out of cloud-synced folders and network shares.

| | Windows Server | Linux |
|---|---|---|
| Application code | `C:\apps\hiring-screening` | `/opt/hiring-screening` |
| Virtual environment | `C:\apps\venvs\hiring-screening` | `/opt/venvs/hiring-screening` |
| Saved runs (`SCREENING_RUNS_DIR`) | `D:\screening-data\runs` | `/var/lib/hiring-screening/runs` |
| Service account | `<DOMAIN\svc-screening>` | `screening` |
| App port | `8501` | `8501` |

**Contents**

1. [How the app runs](#1-how-the-app-runs)
2. [Prerequisites](#2-prerequisites)
3. [Copy the project to the VM](#3-copy-the-project-to-the-vm)
4. [Create the virtual environment and install dependencies](#4-create-the-virtual-environment-and-install-dependencies)
5. [Configuration](#5-configuration)
6. [Run interactively](#6-run-interactively)
7. [Run as a service](#7-run-as-a-service)
8. [Network and security](#8-network-and-security)
9. [LLM data handling](#9-llm-data-handling)
10. [Health check](#10-health-check)
11. [Data retention and cleanup](#11-data-retention-and-cleanup)
12. [Backups](#12-backups)
13. [Upgrading](#13-upgrading)
14. [Smoke test after installation](#14-smoke-test-after-installation)
15. [Troubleshooting](#15-troubleshooting)

---

## 1. How the app runs

```
 Recruiter's browser (intranet / VPN)
        |  HTTPS, company SSO                          <- recommended when several people use it
        v
 Company reverse proxy (TLS + SSO)
        |  HTTP + WebSocket to 127.0.0.1:8501
        v
 Streamlit app: one Python process started by scripts/run_app.ps1 or scripts/run_app.sh
        |  writes: saved runs (SCREENING_RUNS_DIR), LLM response cache (.cache/llm/)
        v  only when a user clicks an LLM action
 LLM endpoint (hosted provider, or the internal wrapper; optional)
```

* One Python process serves the page and a WebSocket connection (`/_stcore/stream`) on a single
  TCP port.
* Every browser tab is its own session. Its uploads, parsed resumes and results are held in the
  server's memory. Uploaded files themselves are never written to disk.
* The app writes to disk only in these cases: a user saves a run in Run History (this stores the
  results and the parsed resume text, see section 11), an LLM response is cached, someone generates
  the synthetic sample data, or the service writes its log.
* There is no login screen. Who can open the URL is decided by the network, the firewall and the
  reverse proxy (section 8).

## 2. Prerequisites

| Item | Requirement |
|---|---|
| Operating system | Windows Server 2019 or later, or 64-bit Linux with glibc 2.28 or later (RHEL/Rocky/Alma 8+, Debian 11+, Ubuntu 22.04+) |
| Python | **3.11, 64-bit** (`requirements.txt` is pinned and tested on 3.11.9) |
| CPU | 4 vCPU recommended; resume parsing runs on several threads |
| Memory | 8 GB minimum; 16 GB when several recruiters process drives at the same time |
| Disk | About **1.5 GB** for the code, the virtual environment (about 0.6 GB) and the pip download cache, **plus data**: about 6 MB per saved 3,000-candidate run, the LLM cache (a few KB per response) and, if generated, the synthetic sample (about 90 MB, or 10 MB with `--quick`) |
| Network | Inbound: the app port (or 443 on the reverse proxy). Outbound: the package index during installation only, and the LLM endpoint only if LLM features are used |
| Accounts | An administrator to install; a dedicated, non-administrator service account to run the app |

Measured on the 3,000-candidate synthetic sample (2,892 resumes): preparing the data takes about
30 s and the process peaks at about 0.9 GB of memory. Afterwards each open session keeps roughly
0.3 GB, plus the size of its uploads (the sample's resume ZIP is 40 MB; the upload limit is 1 GB),
until its browser tab has been closed for about two minutes.

**Installing Python 3.11**

* **Windows Server:** use the python.org 3.11.9 64-bit installer, or the company software portal.
  Install it **for all users** so the service account can run it. The installer adds the `py`
  launcher. Unattended install:

  ```powershell
  .\python-3.11.9-amd64.exe /quiet InstallAllUsers=1 PrependPath=1 Include_launcher=1 Include_test=0
  py -0p          # lists installed versions; 3.11 must be among them
  ```

* **Linux:** RHEL/Rocky/Alma 8.8+ and 9.2+: `sudo dnf install python3.11`. Ubuntu 22.04:
  `sudo apt install python3.11 python3.11-venv`. Debian 12: `sudo apt install python3 python3-venv`
  (its `python3` is 3.11). On other distributions, use the company's approved Python 3.11 build.

## 3. Copy the project to the VM

**Option A: Git (preferred).** Clone or pull from the internal repository into
`C:\apps\hiring-screening` or `/opt/hiring-screening`. Ignored files (data, secrets, caches,
virtual environments) are never in the repository, so nothing unwanted comes along. That includes the
synthetic sample: everything under `data/` except `data/README.md` is ignored, so generate the sample
on the VM when you need it (section 14).

**Option B: an archive.** Build it from a clean copy that contains only what the app needs:

* `app.py`, `generate_mock_data.py`, `requirements.txt`, `pytest.ini`, `README.md`, `.gitignore`
* the folders `docs/`, `screening/`, `scripts/`, `tests/`, `ui/`
* `.streamlit/config.toml` and `.streamlit/secrets.toml.example`

**Never copy these:**

| Path | Why |
|---|---|
| `data/` | Candidate data (real TA feeds and resumes are personal data); the synthetic sample is generated on the VM with `python generate_mock_data.py` (section 14) |
| `runs/` | Saved runs contain candidate personal data and belong to the machine that created them |
| `.cache/` | LLM response cache; machine-specific and rebuilt automatically |
| `.streamlit/secrets.toml` | API keys; create a new one on the VM (section 5.2) |
| `.venv/`, `venv/` or any other virtual environment | Virtual environments contain absolute paths and cannot be moved to another machine; create a new one on the VM |
| `__pycache__/`, `.pytest_cache/`, editor or tool settings folders | Machine-specific files that are generated again |

From a Git checkout, `git archive` writes exactly the committed files:

```bash
git archive --format=tar.gz --output ../hiring-screening.tar.gz HEAD
```

Without Git, stage an allow-listed copy and pack it. On Windows (run in the project folder; `tar`
is built into Windows 10, Windows Server 2019 and later):

```powershell
$stage = Join-Path $env:TEMP 'hiring-screening-release'
Remove-Item -Recurse -Force $stage -ErrorAction SilentlyContinue
New-Item -ItemType Directory -Force (Join-Path $stage '.streamlit') | Out-Null
foreach ($item in 'app.py', 'generate_mock_data.py', 'requirements.txt', 'pytest.ini', 'README.md', '.gitignore',
                  'docs', 'screening', 'scripts', 'tests', 'ui') {
    Copy-Item -Recurse -Path $item -Destination $stage
}
Copy-Item '.streamlit\config.toml', '.streamlit\secrets.toml.example' (Join-Path $stage '.streamlit')
Get-ChildItem $stage -Recurse -Directory -Force | Where-Object { $_.Name -in '__pycache__', '.pytest_cache' } |
    Remove-Item -Recurse -Force
tar -czf ..\hiring-screening.tar.gz -C $stage .
```

On Linux:

```bash
stage=$(mktemp -d)
cp -r app.py generate_mock_data.py requirements.txt pytest.ini README.md .gitignore \
      docs screening scripts tests ui "$stage"/
mkdir -p "$stage/.streamlit" && cp .streamlit/config.toml .streamlit/secrets.toml.example "$stage/.streamlit/"
find "$stage" -type d \( -name __pycache__ -o -name .pytest_cache \) -prune -exec rm -rf {} +
tar -czf ../hiring-screening.tar.gz -C "$stage" .
```

Unpack on the VM:

```powershell
New-Item -ItemType Directory -Force C:\apps\hiring-screening | Out-Null
tar -xzf .\hiring-screening.tar.gz -C C:\apps\hiring-screening
Get-ChildItem -Recurse C:\apps\hiring-screening | Unblock-File   # clears the "downloaded from the internet" mark
```

```bash
sudo mkdir -p /opt/hiring-screening && sudo tar -xzf hiring-screening.tar.gz -C /opt/hiring-screening
```

If the scripts were edited on Windows and copied to Linux by hand, check their line endings
(section 15).

## 4. Create the virtual environment and install dependencies

Create the virtual environment on the VM itself, in a local folder **outside** the project folder
and outside cloud-synced folders (OneDrive and similar) or network shares. Sync clients and
antivirus scanners lock files while pip writes them, which breaks installs.

### 4.1 With the setup scripts (recommended)

The scripts find Python 3.11 (64-bit), create the virtual environment (or reuse it), install
`requirements.txt`, run `pip check` and the test suite (several hundred tests, about a minute).

```powershell
# Windows (as an administrator)
cd C:\apps\hiring-screening
powershell -NoProfile -ExecutionPolicy Bypass -File scripts\setup.ps1 -VenvPath C:\apps\venvs\hiring-screening
```

```bash
# Linux (as root, so the code and the environment stay read-only for the service account)
cd /opt/hiring-screening
sudo bash scripts/setup.sh --venv /opt/venvs/hiring-screening
```

| `setup.ps1` | `setup.sh` | Purpose |
|---|---|---|
| `-VenvPath <dir>` | `--venv <dir>` | Virtual environment folder (default: `<user profile>\.venvs\hiring-screening` or `~/.venvs/hiring-screening`) |
| `-IndexUrl <url>` | `--index-url <url>` | Install from an internal mirror (4.3) |
| `-TrustedHost <host>` | `--trusted-host <host>` | Skip TLS verification for that mirror host (only when unavoidable) |
| `-WheelDir <dir>` | `--wheel-dir <dir>` | Install offline from downloaded wheels (4.4) |
| `-PythonExe <path>` | `--python <exe>` | Use this interpreter instead of searching for Python 3.11 |
| `-NoTests` | `--no-tests` | Skip the test suite |
| `-WhatIf` | `--dry-run` | Show the steps without changing anything |

`-ExecutionPolicy Bypass` applies to that one PowerShell process only; it does not change the
machine's policy. If Group Policy requires signed scripts, sign `scripts\*.ps1` with the company
code-signing certificate instead.

### 4.2 Manually

```powershell
py -3.11 -m venv C:\apps\venvs\hiring-screening
C:\apps\venvs\hiring-screening\Scripts\python.exe -m pip install -r C:\apps\hiring-screening\requirements.txt
```

```bash
python3.11 -m venv /opt/venvs/hiring-screening
/opt/venvs/hiring-screening/bin/python -m pip install -r /opt/hiring-screening/requirements.txt
```

### 4.3 Through a corporate proxy or an internal mirror

* **Proxy:** set it for the installing shell, for example `$env:HTTPS_PROXY = 'http://<proxy-host>:<port>'`
  (PowerShell) or `export HTTPS_PROXY=http://<proxy-host>:<port>` (bash), then run the setup script.
* **Internal mirror** (Artifactory, Nexus, devpi and similar): pass its "simple" index URL, for example
  `-IndexUrl https://<pypi-mirror.example.internal>/simple` or `--index-url ...`. Add `-TrustedHost`
  / `--trusted-host` only for a mirror served over plain HTTP or with a certificate pip cannot
  verify. Credentials embedded in the URL are masked in the script output.
* **Make the mirror permanent** with a pip configuration file (`C:\ProgramData\pip\pip.ini` for the
  whole Windows machine, `/etc/pip.conf` on Linux, or `pip.ini` / `pip.conf` inside the virtual
  environment folder for that environment only):

  ```ini
  [global]
  index-url = https://<pypi-mirror.example.internal>/simple
  # cert = <path to the company root CA bundle (PEM)>
  # trusted-host = <pypi-mirror.example.internal>
  ```

* **TLS inspection (CERTIFICATE_VERIFY_FAILED):** pip 24.2 and later trust the operating system's
  certificate store automatically. The pip that ships with Python 3.11.9 is 24.0 and does not, so
  give it the company root CA as a PEM file: `$env:PIP_CERT = '<path>\company-root-ca.pem'` or
  `export PIP_CERT=/etc/pki/tls/certs/company-root-ca.pem` (or `cert =` in `pip.ini` / `pip.conf`).

### 4.4 Offline (no internet, no mirror)

1. On a machine **with** internet access, the same operating system family and Python 3.11 64-bit,
   download every package as a wheel:

   ```bash
   python -m pip download -r requirements.txt -d wheelhouse --only-binary=:all:
   ```

   To download for a different target operating system, add its tags, for example for a Linux VM:

   ```bash
   python -m pip download -r requirements.txt -d wheelhouse --only-binary=:all: \
       --python-version 3.11 --implementation cp --abi cp311 \
       --platform manylinux_2_28_x86_64 --platform manylinux2014_x86_64
   ```

   or `--platform win_amd64` for a Windows VM.

2. Copy the `wheelhouse` folder to the VM, then install from it:

   ```powershell
   powershell -NoProfile -ExecutionPolicy Bypass -File scripts\setup.ps1 -VenvPath C:\apps\venvs\hiring-screening -WheelDir D:\install\wheelhouse
   ```

   ```bash
   sudo bash scripts/setup.sh --venv /opt/venvs/hiring-screening --wheel-dir /tmp/wheelhouse
   ```

   This runs `pip install --no-index --find-links <wheelhouse> -r requirements.txt`. pip itself
   comes from Python's bundled copy (`ensurepip`), so it needs no download.

## 5. Configuration

### 5.1 Environment variables

| Variable | Purpose | Default |
|---|---|---|
| `NVIDIA_API_KEY` | API key for the hosted LLM provider of the default configuration. Leave it unset to run without LLM features. Takes precedence over `.streamlit/secrets.toml`. | unset |
| `TACHYON_API_KEY` (or the name set in `api_key_env`) | Replaces `NVIDIA_API_KEY` after switching to the internal LLM wrapper ([SWITCH_TO_TACHYON.md](SWITCH_TO_TACHYON.md)). No key is needed if the wrapper authenticates by itself. | unset |
| `SCREENING_RUNS_DIR` | Folder for saved runs (Run History). Put it on a data disk with backups and restricted access. | `<project>/runs` |
| `APP_BRAND_EYEBROW` | The small line of text above the title in the header banner, for example a team or environment name. The app reads it once, so restart it after a change. | `WELLS FARGO · TALENT ACQUISITION` (built into `ui/theme.py`) |
| `HTTPS_PROXY`, `HTTP_PROXY`, `NO_PROXY` | Outbound proxy for LLM calls (pip uses them as well). List internal hosts, such as an internal LLM gateway, in `NO_PROXY`. | unset |
| `SSL_CERT_FILE` / `SSL_CERT_DIR` | CA bundle for LLM calls. Without it, the Windows certificate store or the Linux system CA bundle is used. | unset |
| `STREAMLIT_<SECTION>_<OPTION>` | Any Streamlit setting, for example `STREAMLIT_BROWSER_SERVER_ADDRESS` or `STREAMLIT_SERVER_ADDRESS` (5.3) | unset |

Set them in the shell before an interactive start (`$env:NAME = 'value'` in PowerShell,
`export NAME=value` in bash), or in the service definition (section 7). Keep API keys out of
command lines and scripts.

### 5.2 Secrets file

The API key can live in `.streamlit/secrets.toml` in the project folder instead of an environment
variable (an environment variable wins when both are set). The file is never committed.

```powershell
cd C:\apps\hiring-screening
Copy-Item .streamlit\secrets.toml.example .streamlit\secrets.toml
notepad .streamlit\secrets.toml        # NVIDIA_API_KEY = "<key>"
icacls .streamlit\secrets.toml /inheritance:r /grant:r "Administrators:F" "SYSTEM:F" "<DOMAIN\svc-screening>:R"
```

```bash
# Linux: create the service account first (section 7.1)
cd /opt/hiring-screening
sudo install -m 640 -o root -g screening .streamlit/secrets.toml.example .streamlit/secrets.toml
sudoedit .streamlit/secrets.toml       # NVIDIA_API_KEY = "<key>"
```

The UI only ever shows "LLM: configured", never the key. Rotate any key that may have been exposed.

### 5.3 Streamlit settings

`.streamlit/config.toml` in the project holds the settings that are the same everywhere, including
the safe default listen address `127.0.0.1`: even `streamlit run app.py` started without the run
scripts accepts connections from the same machine only. The run scripts add the per-machine settings.
To change anything per machine, use command-line options of the run scripts or `STREAMLIT_*`
environment variables, so upgrades never overwrite your changes. Precedence: command-line flag, then
`STREAMLIT_*` environment variable, then the project's `.streamlit/config.toml`, then the service
account's `~/.streamlit/config.toml`.

| Setting | Value | Where it is set | Notes |
|---|---|---|---|
| `server.port` | `8501` | run scripts (`-Port` / `--port`) | The scripts never move to another port silently; a busy port is an error |
| `server.address` | `127.0.0.1` | config.toml (`[server] address`, the default); run scripts (`-Address` / `--address`); `STREAMLIT_SERVER_ADDRESS` | `127.0.0.1` = this machine only (behind a reverse proxy); `0.0.0.0` = all interfaces (section 8). The run scripts always pass an address (default `127.0.0.1`), which overrides `STREAMLIT_SERVER_ADDRESS`, so with the scripts use `-Address` / `--address`; the variable applies when Streamlit is started directly |
| `server.headless` | `true` | run scripts | No browser window and no first-run prompt; required for services |
| `server.fileWatcherType` | `none` | run scripts | No reloads when files change; restart after an upgrade |
| `browser.gatherUsageStats` | `false` | config.toml and run scripts | No usage statistics are sent |
| `server.maxUploadSize` | `1024` (MB) | config.toml | A reverse proxy needs at least the same request-body limit |
| `server.enableXsrfProtection` | `true` | config.toml | Keep it on. Uploads need the XSRF cookie, so serve the app on its own origin and not in a cross-origin iframe |
| `server.enableCORS` | `true` | config.toml | Keep it on |
| `browser.serverAddress` | host name users type | run scripts (`-PublicHost` / `--public-host`) or `STREAMLIT_BROWSER_SERVER_ADDRESS` | Used in the printed URL and in the origin checks. Set it whenever users reach the app by host name |
| `browser.serverPort` | `443` behind a TLS proxy | `STREAMLIT_BROWSER_SERVER_PORT` | Port users type, when it differs from `server.port` |
| `server.baseUrlPath` | for example `screening` | `STREAMLIT_SERVER_BASE_URL_PATH` | Only when the proxy publishes the app under a path such as `https://<host>/screening/` |
| `server.allowedHosts` | for example `<screening.example.internal> localhost` | `STREAMLIT_SERVER_ALLOWED_HOSTS` (space-separated) | Optional protection against DNS rebinding: only these `Host` headers are accepted on the browser connection |
| `client.showErrorDetails` | `type` | `STREAMLIT_CLIENT_SHOW_ERROR_DETAILS` | Optional hardening: users see only the error type for unexpected errors; full details stay in the log |
| `server.disconnectedSessionTTL` | `120` (s) | `STREAMLIT_SERVER_DISCONNECTED_SESSION_TTL` | How long a session survives a dropped connection (laptop sleep, VPN reconnect). Longer keeps more memory in use |

### 5.4 LLM provider, or no LLM

* **No LLM (safest for real candidate data until a provider is approved):** leave the key unset.
  The sidebar shows "LLM: not configured"; LLM buttons explain how to enable them; every other
  feature works. A recruiter can also switch LLM features off for their own session
  (Config & Rubric Tuning, "LLM features enabled").
* **Hosted provider (default configuration):** set `NVIDIA_API_KEY`. Calls go to the provider's
  public endpoint, which means candidate-related text leaves the company network (section 9).
  Allow outbound HTTPS to that endpoint through the proxy only if the data policy permits it.
* **Internal LLM wrapper:** follow [SWITCH_TO_TACHYON.md](SWITCH_TO_TACHYON.md), then set the key
  variable it names (if any). Add the gateway host to `NO_PROXY` if it must bypass the proxy.

After any change, check it in Config & Rubric Tuning with "Test LLM connection".

## 6. Run interactively

Use this for the first start and for troubleshooting. The app runs in the foreground; stop it with
Ctrl+C.

```powershell
# Windows: this machine only (http://127.0.0.1:8501)
powershell -NoProfile -ExecutionPolicy Bypass -File C:\apps\hiring-screening\scripts\run_app.ps1 -VenvPath C:\apps\venvs\hiring-screening

# Windows: reachable from the intranet (read section 8 first)
powershell -NoProfile -ExecutionPolicy Bypass -File C:\apps\hiring-screening\scripts\run_app.ps1 -VenvPath C:\apps\venvs\hiring-screening -Address 0.0.0.0 -PublicHost <vm-name.example.internal>
```

```bash
# Linux: this machine only
bash /opt/hiring-screening/scripts/run_app.sh --venv /opt/venvs/hiring-screening

# Linux: reachable from the intranet (read section 8 first)
bash /opt/hiring-screening/scripts/run_app.sh --venv /opt/venvs/hiring-screening --address 0.0.0.0 --public-host <vm-name.example.internal>
```

| `run_app.ps1` | `run_app.sh` | Purpose | Default |
|---|---|---|---|
| `-VenvPath <dir>` | `--venv <dir>` | Virtual environment created in section 4 | `<user profile>\.venvs\hiring-screening` / `~/.venvs/hiring-screening` |
| `-Port <n>` | `--port <n>` | TCP port | `8501` |
| `-Address <ip>` | `--address <ip>` | Interface to listen on | `127.0.0.1` |
| `-PublicHost <name>` | `--public-host <name>` | Host name users type (sets `browser.serverAddress`) | not set |

The scripts always start the app from the project folder, because `.streamlit/` and `.cache/`
are resolved from the working directory. The app is ready when it prints its URL, usually within
a few seconds. Then open `http://<vm-name>:8501` (or `http://127.0.0.1:8501` on the VM itself when
it listens on `127.0.0.1`) and run the smoke test (section 14).

Started without the scripts (`python -m streamlit run app.py` from the project folder), the app also
listens on `127.0.0.1` only, because `.streamlit/config.toml` sets that address. To expose it on the
intranet that way, set `STREAMLIT_SERVER_ADDRESS=0.0.0.0` (or pass `--server.address 0.0.0.0`). Prefer
the scripts for anything beyond a quick check: they also set headless mode, switch off file watching
and check the port.

## 7. Run as a service

### 7.1 Service account and permissions

Run the app under a dedicated account without administrator rights. It needs:

| Path | Access |
|---|---|
| Python 3.11 installation, application code, virtual environment | read and execute |
| `.cache/` in the project folder (LLM cache) and the saved-runs folder | modify |
| `.streamlit/secrets.toml` | read |
| log folder (Windows with NSSM) | modify |

```powershell
# Windows (as an administrator)
$svc = '<DOMAIN\svc-screening>'
New-Item -ItemType Directory -Force C:\apps\hiring-screening\.cache, C:\apps\hiring-screening\logs, D:\screening-data\runs | Out-Null
icacls C:\apps\hiring-screening /grant "${svc}:(OI)(CI)RX"
icacls C:\apps\venvs\hiring-screening /grant "${svc}:(OI)(CI)RX"
icacls C:\apps\hiring-screening\.cache /grant "${svc}:(OI)(CI)M"
icacls C:\apps\hiring-screening\logs /grant "${svc}:(OI)(CI)M"
icacls D:\screening-data\runs /inheritance:r /grant:r "Administrators:(OI)(CI)F" "SYSTEM:(OI)(CI)F" "${svc}:(OI)(CI)M"
```

```bash
# Linux
sudo useradd --system --create-home --home-dir /var/lib/hiring-screening --shell /usr/sbin/nologin screening
sudo install -d -o screening -g screening -m 750 /var/lib/hiring-screening/runs /opt/hiring-screening/.cache
```

### 7.2 Windows: NSSM (recommended)

NSSM runs any program as a Windows service, restarts it when it stops and rotates its log. It is a
third-party tool; install it from the company software repository and put `nssm.exe` on the PATH.

```powershell
$app  = 'C:\apps\hiring-screening'
$venv = 'C:\apps\venvs\hiring-screening'
nssm install HiringScreening "$env:SystemRoot\System32\WindowsPowerShell\v1.0\powershell.exe"
nssm set HiringScreening AppParameters "-NoProfile -ExecutionPolicy Bypass -File $app\scripts\run_app.ps1 -VenvPath $venv -Port 8501 -Address 127.0.0.1"
nssm set HiringScreening AppDirectory $app
nssm set HiringScreening DisplayName "Candidate Screening & Integrity Console"
nssm set HiringScreening Start SERVICE_DELAYED_AUTO_START
nssm set HiringScreening AppEnvironmentExtra "SCREENING_RUNS_DIR=D:\screening-data\runs" "STREAMLIT_BROWSER_SERVER_ADDRESS=<screening.example.internal>"
nssm set HiringScreening AppStdout "$app\logs\service.log"
nssm set HiringScreening AppStderr "$app\logs\service.log"
nssm set HiringScreening AppRotateFiles 1
nssm set HiringScreening AppRotateOnline 1
nssm set HiringScreening AppRotateBytes 10485760
nssm set HiringScreening AppExit Default Restart
nssm set HiringScreening AppRestartDelay 5000
$cred = Get-Credential -Message 'Service account for the screening app'   # keeps the password out of the command history
nssm set HiringScreening ObjectName $cred.UserName $cred.GetNetworkCredential().Password
nssm start HiringScreening
```

* Use `-Address 0.0.0.0` instead of `127.0.0.1` when there is no reverse proxy on the same VM.
* The paths above contain no spaces. If yours do, quote them inside `AppParameters`.
* Keep the API key in `.streamlit\secrets.toml` (5.2) rather than in `AppEnvironmentExtra`, which
  any administrator can read in the registry.
* Daily operation: `Get-Service HiringScreening`, `Restart-Service HiringScreening`, log in
  `C:\apps\hiring-screening\logs\service.log`. Remove with `nssm remove HiringScreening confirm`.

### 7.3 Windows: Task Scheduler (built in, when NSSM is not available)

```powershell
$app  = 'C:\apps\hiring-screening'
$venv = 'C:\apps\venvs\hiring-screening'
$action = New-ScheduledTaskAction -Execute "$env:SystemRoot\System32\WindowsPowerShell\v1.0\powershell.exe" `
    -Argument "-NoProfile -ExecutionPolicy Bypass -File $app\scripts\run_app.ps1 -VenvPath $venv -Port 8501 -Address 127.0.0.1" `
    -WorkingDirectory $app
$trigger = New-ScheduledTaskTrigger -AtStartup
$settings = New-ScheduledTaskSettingsSet -ExecutionTimeLimit ([TimeSpan]::Zero) -MultipleInstances IgnoreNew `
    -RestartCount 3 -RestartInterval (New-TimeSpan -Minutes 1) -StartWhenAvailable
$cred = Get-Credential -Message 'Service account for the screening app'
Register-ScheduledTask -TaskName 'HiringScreening' -Action $action -Trigger $trigger -Settings $settings `
    -User $cred.UserName -Password $cred.GetNetworkCredential().Password -RunLevel Limited
Start-ScheduledTask -TaskName 'HiringScreening'
```

* `-ExecutionTimeLimit ([TimeSpan]::Zero)` is required: by default Task Scheduler stops a task
  after 72 hours.
* The account needs the "Log on as a batch job" right (often managed by Group Policy). For a group
  managed service account, use
  `-Principal (New-ScheduledTaskPrincipal -UserId '<DOMAIN\gmsa-screening$>' -LogonType Password)`
  instead of `-User`/`-Password`.
* Task Scheduler has no per-task environment variables. Put the API key in
  `.streamlit\secrets.toml`, and set other variables machine-wide, for example
  `[Environment]::SetEnvironmentVariable('SCREENING_RUNS_DIR', 'D:\screening-data\runs', 'Machine')`.
  Restart the task afterwards (restart the VM if the task still sees the old value).
* Task Scheduler does not keep the app's console output and does not watch the app the way a
  service manager does. Monitor the health endpoint (section 10), or prefer NSSM.

### 7.4 Linux: systemd

`/etc/hiring-screening.env` (owner `root:screening`, mode `640`):

```ini
SCREENING_RUNS_DIR=/var/lib/hiring-screening/runs
STREAMLIT_BROWSER_SERVER_ADDRESS=<screening.example.internal>
# HTTPS_PROXY=http://<proxy-host>:<port>
# NO_PROXY=localhost,127.0.0.1,.example.internal
# NVIDIA_API_KEY is better kept in /opt/hiring-screening/.streamlit/secrets.toml (section 5.2)
```

`/etc/systemd/system/hiring-screening.service`:

```ini
[Unit]
Description=Candidate Screening & Integrity Console
Wants=network-online.target
After=network-online.target

[Service]
Type=simple
User=screening
Group=screening
WorkingDirectory=/opt/hiring-screening
EnvironmentFile=-/etc/hiring-screening.env
ExecStart=/bin/bash /opt/hiring-screening/scripts/run_app.sh --venv /opt/venvs/hiring-screening --port 8501 --address 127.0.0.1
Restart=on-failure
RestartSec=5
TimeoutStopSec=20
UMask=0027
NoNewPrivileges=true
PrivateTmp=true
ProtectSystem=strict
# ProtectHome assumes the virtual environment is not under /home.
ProtectHome=true
ReadWritePaths=/opt/hiring-screening/.cache /var/lib/hiring-screening

[Install]
WantedBy=multi-user.target
```

```bash
sudo systemctl daemon-reload
sudo systemctl enable --now hiring-screening
systemctl status hiring-screening
journalctl -u hiring-screening -f          # follow the log
```

Use `--address 0.0.0.0` in `ExecStart` when there is no reverse proxy on the same VM.
`ReadWritePaths` must list every folder the app writes to (the LLM cache and the saved runs).

## 8. Network and security

**The app has no login of its own.** Anyone who can open its URL can upload data, run
screenings, and open, rename or delete every saved run (saved runs are shared by all users of an
installation). Access control therefore has to come from the network.

| Setup | When | How |
|---|---|---|
| **A. Direct intranet access** | Pilot or a single small team | Start with `-Address 0.0.0.0` (`--address 0.0.0.0`) and `-PublicHost <vm-name>`. Allow the port only from the recruiters' subnets or the VPN range. Traffic is plain HTTP inside the network; use this only if the company's data policy allows candidate data over unencrypted internal connections. |
| **B. Behind the company reverse proxy (recommended)** | Several recruiters share the app | The proxy terminates TLS and enforces company SSO (ideally limited to a recruiting group). The app listens on `127.0.0.1` only, and the firewall opens only the proxy's port 443. |

Do not use Streamlit's built-in TLS options (`server.sslCertFile`); Streamlit itself recommends
terminating TLS at a proxy for production.

**Firewall**

```powershell
# Windows, setup A: allow the app port from the intranet range only
New-NetFirewallRule -DisplayName 'Candidate screening app (TCP 8501)' -Direction Inbound -Action Allow `
    -Protocol TCP -LocalPort 8501 -RemoteAddress <10.0.0.0/8> -Profile Domain
```

```bash
# Linux, setup A (firewalld or ufw)
sudo firewall-cmd --permanent --add-rich-rule='rule family="ipv4" source address="<10.0.0.0/8>" port port="8501" protocol="tcp" accept'
sudo firewall-cmd --reload
sudo ufw allow from <10.0.0.0/8> to any port 8501 proto tcp
```

For setup B, open 443 for the proxy instead and leave 8501 closed.

**Reverse proxy requirements**

* Pass WebSocket upgrades: the browser keeps a WebSocket open to `/_stcore/stream`.
* Long idle timeouts (an hour or more) for that connection.
* A request-body limit of at least 1 GB, matching `server.maxUploadSize`.
* Forward the original `Host` header, or set `browser.serverAddress` to the public host name.
  Otherwise the app rejects the browser connection as cross-origin and the page stays on
  "Connecting".
* Run one app instance per VM. If a load balancer spreads users over several instances, it needs
  sticky sessions, and saved runs must live on storage that all instances share.

nginx example (adapt the SSO part to the company's standard, for example `auth_request` to the
identity gateway):

```nginx
# in the http { } block
map $http_upgrade $connection_upgrade { default upgrade; '' close; }

server {
    listen 443 ssl;
    server_name <screening.example.internal>;
    ssl_certificate     /etc/nginx/tls/screening.crt;
    ssl_certificate_key /etc/nginx/tls/screening.key;

    client_max_body_size 1024m;          # >= server.maxUploadSize
    proxy_request_buffering off;         # stream large uploads to the app

    location / {
        proxy_pass http://127.0.0.1:8501;
        proxy_http_version 1.1;
        proxy_set_header Host $host;
        proxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for;
        proxy_set_header X-Forwarded-Proto $scheme;
        proxy_set_header Upgrade $http_upgrade;
        proxy_set_header Connection $connection_upgrade;
        proxy_read_timeout 1h;
        proxy_send_timeout 1h;
    }
}
```

To publish the app under a path instead (`https://<host>/screening/`), set
`STREAMLIT_SERVER_BASE_URL_PATH=screening` and use `location /screening/ { proxy_pass http://127.0.0.1:8501/screening/; ... }`.

IIS on Windows Server (Application Request Routing + URL Rewrite):

* Install the WebSocket Protocol feature (`Install-WindowsFeature Web-WebSockets`), URL Rewrite and
  Application Request Routing, and enable the proxy in ARR's "Server Proxy Settings".
* Add a reverse-proxy rule from `(.*)` to `http://127.0.0.1:8501/{R:1}`, and bind the site to
  443 with the company certificate.
* Keep the original host header:
  `& "$env:windir\system32\inetsrv\appcmd.exe" set config -section:system.webServer/proxy /preserveHostHeader:"True" /commit:apphost`
* Raise the upload limit (the IIS default is about 30 MB):
  `& "$env:windir\system32\inetsrv\appcmd.exe" set config "<site name>" -section:system.webServer/security/requestFiltering /requestLimits.maxAllowedContentLength:1073741824`
* For SSO, enable Windows Authentication on the site and restrict it to the recruiting group with
  an authorization rule.

**Where the data lives**

* Uploads, parsed resumes and results stay in the server's memory for the session; files are
  written only as described in section 11.
* Exports (CSV, Excel, evidence packs) are downloaded to the user's own computer; company policy
  for personal data applies there.
* Logs contain no candidate data by design (names, e-mails and API keys are never logged).

**Outbound connections**

| Connection | When | How to control it |
|---|---|---|
| LLM endpoint | Only when a key is configured and a user clicks an LLM action | Section 9; leave the key unset to disable |
| `fonts.googleapis.com` (the "Inter" web font) | Each user's **browser** loads it with the page; no candidate data is involved | If it is blocked, the browser falls back to a system font; nothing else changes |
| Public-IP lookup by Streamlit (`checkip.amazonaws.com`) | Only if `browser.serverAddress` is not set: at startup when listening on `0.0.0.0`, and when a browser connection's origin does not match the `Host` header | Pass `-PublicHost` / `--public-host` (or set `STREAMLIT_BROWSER_SERVER_ADDRESS`) whenever the app is reached by host name, and forward the `Host` header on the proxy |
| Usage statistics | Never (`gatherUsageStats = false`) | |

**Hardening checklist**

* Dedicated service account without administrator rights; code and virtual environment
  read-only for it (section 7.1).
* `.streamlit/secrets.toml` readable only by administrators and the service account; never
  committed and never copied between machines.
* Saved-runs folder readable only by administrators and the service account.
* Optional: `server.allowedHosts` set to the public host name and `client.showErrorDetails = "type"` (5.3).
* Keep Windows/Linux and Python 3.11 patched. Review security advisories for the pinned packages
  when upgrading (section 13).

## 9. LLM data handling

* The app calls the LLM only when a user clicks an LLM action: re-score near the cutoff,
  rationales, candidate explanations with interview and integrity questions, risk-group
  summaries, the executive summary, Ask the Data, or "Test LLM connection". Re-scoring is limited
  to the ranks around the shortlist cutoff.
* Before any text leaves the VM, candidate names, e-mail addresses, phone numbers and URLs are
  replaced with placeholders, and resume text is truncated (6,000 characters by default). Prompts
  tell the model to ignore protected attributes and to word integrity findings as patterns to
  verify, not accusations.
* Ask the Data sends only column names, types, descriptions and example values of
  low-cardinality, non-personal columns, never rows of data. The question is masked for personal
  values before it is sent, and the answer is computed locally.
* Responses are cached in `.cache/llm/` in the project folder: one JSON file per response,
  holding the parsed response only (not the prompt) and named by a hash of the model, prompt
  version and prompt. The cache is shared by all users of the installation and can be deleted at
  any time (section 11).
* With the hosted provider (default configuration), the redacted text is processed outside the
  company network under that provider's terms. Process real candidate data only with a provider
  the company's data policy approves: the internal wrapper ([SWITCH_TO_TACHYON.md](SWITCH_TO_TACHYON.md))
  or no LLM at all (5.4).

## 10. Health check

`GET http://<host>:<port>/_stcore/health` returns HTTP 200 with the body `ok` when the app accepts
connections, and 503 while it is starting or stopping. With `server.baseUrlPath` set, the path is
`/<base path>/_stcore/health`.

```powershell
(Invoke-WebRequest -UseBasicParsing http://127.0.0.1:8501/_stcore/health).Content    # ok
```

```bash
curl -fsS http://127.0.0.1:8501/_stcore/health; echo                                  # ok
```

Point the company monitoring at this URL: on the VM itself, or through the proxy if the monitoring
system can pass its SSO. It shows that the server is up; it does not test the LLM endpoint or the
saved-runs folder. The smoke test (section 14) covers those.

## 11. Data retention and cleanup

| Where | What | Personal data | Cleanup |
|---|---|---|---|
| Server memory (each browser session) | Uploaded files, parsed resumes, results | Yes | Freed about two minutes after the tab is closed (`server.disconnectedSessionTTL`) and on every service restart. Never written to disk. |
| Saved runs (`SCREENING_RUNS_DIR`, default `<project>/runs`) | One folder per run named `YYYYMMDD-HHMMSS-xxxxxx` (save time): result tables (Parquet), configuration, data-quality report, parsed resumes including resume text (`parsed.json.gz`), LLM notes | **Yes** | Delete in the Run History tab ("Delete"), or remove run folders past the retention period (below) |
| `.cache/llm/` in the project folder | LLM responses (JSON), one file per response | Model-written text about candidates; names, e-mails and phone numbers were redacted from the prompts | Safe to delete at any time; responses are fetched again when needed |
| `data/sample/` | Synthetic sample data | No (fictional) | Delete after the smoke test if it is not needed |
| Service log | Start/stop messages, warnings | No (by design) | NSSM rotation (7.2) or journald retention |
| Users' computers | Downloaded exports and evidence packs | Yes | Company policy for personal data |

Apply the company's retention period to saved runs. Examples for 180 days for runs and 30 days for
the LLM cache:

```powershell
$cutoff = (Get-Date).AddDays(-180).ToString('yyyyMMdd')
Get-ChildItem D:\screening-data\runs -Directory |
    Where-Object { $_.Name -match '^\d{8}-\d{6}-[0-9a-f]{6}$' -and $_.Name.Substring(0, 8) -lt $cutoff } |
    Remove-Item -Recurse -Force
Get-ChildItem C:\apps\hiring-screening\.cache\llm -File -Filter *.json |
    Where-Object { $_.LastWriteTime -lt (Get-Date).AddDays(-30) } | Remove-Item
```

```bash
cutoff=$(date -d '180 days ago' +%Y%m%d)
for dir in /var/lib/hiring-screening/runs/*/; do
    name=$(basename "$dir")
    [[ "$name" =~ ^[0-9]{8}-[0-9]{6}-[0-9a-f]{6}$ && "${name:0:8}" < "$cutoff" ]] && rm -rf -- "$dir"
done
find /opt/hiring-screening/.cache/llm -type f -name '*.json' -mtime +30 -delete
```

Schedule them (Task Scheduler or a systemd timer / cron) under the service account. Folders named
`.tmp-...` in the runs folder are left behind by an interrupted save; delete them while the app is
stopped.

## 12. Backups

| Back up | Notes |
|---|---|
| Saved-runs folder | Only if run history must survive a VM loss. The backup holds personal data: encrypt it, restrict access, and apply the same retention period |
| `.streamlit/secrets.toml` | Better: keep the key in the company secrets vault and recreate the file on a restore |
| Site configuration | Service definition (NSSM or Task Scheduler settings, systemd unit, `/etc/hiring-screening.env`), reverse-proxy and firewall configuration, and any VM-specific `.streamlit/config.toml` changes |

Not needed: the virtual environment, `.cache/`, `data/sample/` and `__pycache__/` are all rebuilt.
The input files (TA feed, resumes) come from the assessment platform and the applicant tracking
system, which remain the systems of record.

Restore: install the same release (sections 3 and 4), restore the saved runs and the secrets, start
the service, and run the smoke test.

## 13. Upgrading

1. Read the release notes. Check whether `requirements.txt` or the Python version changed.
2. Stop the app: `Stop-Service HiringScreening` (or `Stop-ScheduledTask -TaskName HiringScreening`),
   or `sudo systemctl stop hiring-screening`. On Windows this is required: a running app locks
   files inside the virtual environment. Check that nothing still listens on the port
   (`Get-NetTCPConnection -LocalPort 8501 -State Listen` should return nothing).
3. Back up the saved runs (section 12) and keep the current code folder for a rollback, for
   example as `C:\apps\hiring-screening.prev` or `/opt/hiring-screening.prev`.
4. Update the code: `git pull` in the project folder, or unpack the new release into an empty
   folder and copy over `.streamlit/secrets.toml` and any VM-specific settings. Re-apply the
   permissions from section 7.1 if the folder is new.
5. Install the dependencies with the same command as in section 4. It installs what changed and
   runs the tests. If the Python version or many pins changed, create a new virtual environment
   next to the old one (for example `C:\apps\venvs\hiring-screening-2`) and point the service at it.
   That keeps an instant rollback path.
6. Start the app, check the health endpoint (section 10), run the smoke test (section 14) and open
   one older saved run in Run History to confirm it still loads.
7. Rollback: stop the app, restore the previous code folder (and the previous virtual environment
   path in the service definition), then start it again.

The LLM cache does not need clearing: its file names include the model and prompt version, so old
entries are simply not reused.

## 14. Smoke test after installation

Run these from the project folder with the virtual environment's Python
(`C:\apps\venvs\hiring-screening\Scripts\python.exe` or `/opt/venvs/hiring-screening/bin/python`):

1. **Tests:** `python -m pytest -p no:cacheprovider`. All tests pass (several hundred, about a minute).
   The setup scripts already ran them unless `-NoTests` / `--no-tests` was used.
2. **Synthetic data:** the sample is not part of the repository or a release archive, so generate it
   on the VM. `python generate_mock_data.py --quick` writes 300 fictional candidates and about 290
   resumes to `data/sample/` in a few seconds, plus `resumes.zip` (used in step 4) unless you add
   `--no-zip`. Without `--quick` it writes the full 3,000-candidate dataset (a few minutes, about 90 MB).
3. **UI:** start the app (section 6 or 7), open it in a browser from a recruiter's machine and click
   **Load synthetic sample & run**. Expect the KPI cards, a ranked shortlist, risk clusters in
   Integrity & Risk Intelligence and a candidate profile in Individual Deep-Dive. Download the CSV
   or Excel export once.
4. **Uploads:** copy `ta_feed.csv` and `resumes.zip` from `data/sample/` to the machine you test
   from, upload them in the sidebar and run screening. This checks the upload path, including the
   proxy's body-size limit (the full sample's ZIP is 40 MB, above the nginx and IIS defaults).
5. **Run History:** save the run, open it again, then delete it. This checks write access to the
   saved-runs folder.
6. **LLM (if configured):** Config & Rubric Tuning, "Test LLM connection".
7. **Health:** the health endpoint returns `ok` (section 10).
8. **Clean up:** delete `data/sample/` if the synthetic data is not needed on this VM. The
   "Load synthetic sample & run" button then shows as unavailable.

## 15. Troubleshooting

| Symptom | Cause and fix |
|---|---|
| `Port 8501 is already in use ...` or `Port 8501 is not available` | Another program (often a second copy of the app) listens on that port. Find it with `Get-NetTCPConnection -LocalPort 8501 -State Listen` and `Get-Process -Id <pid>` (Windows) or `sudo ss -ltnp 'sport = :8501'` (Linux). Stop it or choose another `-Port` / `--port` (and update the proxy and firewall). |
| The app works in a browser on the VM itself, but other machines cannot connect | It listens on `127.0.0.1`, the default from `.streamlit/config.toml` and the run scripts. Start it with `-Address 0.0.0.0` / `--address 0.0.0.0` (or `STREAMLIT_SERVER_ADDRESS=0.0.0.0` without the scripts) and allow the port in the firewall, or publish it through the reverse proxy (section 8). |
| `Python 3.11 (64-bit) is required` | Install Python 3.11 64-bit for all users (section 2), check with `py -0p` (Windows) or `python3.11 --version`, or pass `-PythonExe` / `--python`. 32-bit Python cannot install the pinned packages. |
| `ensurepip is not available` (Debian/Ubuntu) | `sudo apt install python3.11-venv` (or `python3-venv` on Debian 12), then run setup again. |
| pip: `ProxyError`, `Connection timed out`, `Could not fetch URL` | No route to the index. Set `HTTPS_PROXY`, or use the internal mirror (`-IndexUrl`) or an offline wheelhouse (4.4). |
| pip: `CERTIFICATE_VERIFY_FAILED` | TLS inspection with a company root CA that pip 24.0 does not know: set `PIP_CERT` (4.3). `-TrustedHost` / `--trusted-host` for the mirror is the fallback. |
| pip: `No matching distribution found for <package>==<version>` | The mirror does not carry that version, or the interpreter is not Python 3.11 64-bit. Ask the mirror administrators to sync it, or use a wheelhouse. Offline: the wheels were downloaded for another platform; download them again with the target's `--platform` / `--python-version` (4.4). |
| `running scripts is disabled on this system` | Start the scripts with `powershell -NoProfile -ExecutionPolicy Bypass -File ...`, and run `Unblock-File` on files copied from another machine (section 3). |
| `$'\r': command not found` or `/bin/bash^M: bad interpreter` (Linux) | The `.sh` files have Windows line endings: `sed -i 's/\r$//' scripts/*.sh`. |
| pip on Windows: `Access is denied`, `WinError 5` or `WinError 32` | Files in the virtual environment are locked: stop the service (section 13, step 2) and any other Python process that uses the environment, then run setup again. Keep virtual environments out of synced folders; if antivirus scanning interferes, ask for the company's standard exclusion for Python environments. Deleting saved runs or the cache can fail the same way while the app has the files open. |
| Page loads but stays on "Connecting", or shows a connection error | The WebSocket to `/_stcore/stream` is blocked or rejected. Allow WebSocket upgrades and long timeouts on the proxy, forward the `Host` header or set `browser.serverAddress` (section 8), and check that `server.baseUrlPath` matches the published path. A forward proxy configured in the users' browsers may need an exception for the internal host. On RHEL with nginx, a 502 with SELinux denials in the audit log needs `sudo setsebool -P httpd_can_network_connect 1`. |
| Connection drops every minute or so behind a proxy or load balancer | Its idle timeout is shorter than the WebSocket keep-alive. Raise the timeout, or set `STREAMLIT_SERVER_WEBSOCKET_PING_INTERVAL` to a value below it (in seconds). |
| Upload refused: "File must be ... or smaller" | Streamlit's `server.maxUploadSize` (1024 MB per file). Split the resumes into several ZIPs, or raise the limit with `STREAMLIT_SERVER_MAX_UPLOAD_SIZE` (and on the proxy). |
| Upload fails with HTTP 413 (nginx) or 404.13 (IIS) | The proxy's body limit: `client_max_body_size` (nginx) or `maxAllowedContentLength` (IIS), section 8. |
| Upload fails with HTTP 403 | The XSRF cookie did not reach the app: the page is embedded in a cross-origin frame, or the proxy strips cookies. Serve the app on its own origin; keep XSRF protection on. |
| Some resumes are skipped with "larger than 10 MB" or ZIP limit warnings | The app's own limits (10 MB per file; 10,000 files and 1,024 MB uncompressed per ZIP; ZIP-bomb guard) are listed in the data-quality section. Split oversized ZIPs. |
| Service starts and stops again | Read the log (NSSM log file, Event Viewer, or `journalctl -u hiring-screening -e`). Usual causes: wrong virtual environment path, Python installed for one user only, no write access to `.cache/` or the saved-runs folder, port in use. Run the same command interactively as the service account to see the error. |
| "No LLM API key configured" although a key is set | The key is not visible to the account and folder the app runs as: environment variables set in another session, or `secrets.toml` outside `<project>/.streamlit/`. Check the service definition (section 7). |
| LLM: HTTP 401/403, 404/410, timeouts or certificate errors | 401/403: wrong key or no access to the model. 404/410: the model was retired; the app tries the fallback models automatically. Timeouts or connection errors: set `HTTPS_PROXY` / `NO_PROXY`. Certificate errors: install the company root CA in the OS certificate store, or set `SSL_CERT_FILE`. |
| Session resets after a laptop sleeps or the VPN reconnects | Sessions survive about two minutes without a connection (`server.disconnectedSessionTTL`). Save important runs in Run History. Raise the TTL if needed; it keeps more memory in use. |
| The VM runs out of memory or gets slow with several users | Every open session keeps its own data (roughly 0.3 GB for 3,000 candidates, plus its uploads). Add memory, ask users to close finished tabs, or restart the service outside working hours. |
