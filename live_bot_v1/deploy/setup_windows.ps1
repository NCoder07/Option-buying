# setup_windows.ps1 — One-shot Windows deployment for nifty_bot
# Run from the VM as Administrator in PowerShell:
#   cd C:\nifty_bot\live_bot_v1\deploy
#   powershell -ExecutionPolicy Bypass -File setup_windows.ps1
#
# What it does:
#   1. Creates required directories
#   2. Installs the bot as a Windows service via NSSM (auto-start, restart-on-failure)
#   3. Sets service environment (reads C:\nifty_bot\secrets\bot.env)
#   4. Configures stdout/stderr logging to C:\nifty_bot\logs\
#   5. Starts the service

$ErrorActionPreference = "Stop"
$BOT_DIR    = "C:\nifty_bot\live_bot_v1"
$STATE_DIR  = "C:\nifty_bot\state"
$DATA_DIR   = "C:\nifty_bot\data"
$LOG_DIR    = "C:\nifty_bot\logs"
$SECRETS    = "C:\nifty_bot\secrets\bot.env"
$NSSM       = "C:\nifty_bot\nssm.exe"
$PYTHON     = (Get-Command python).Source
$SVC_NAME   = "nifty_bot"

Write-Host "=== nifty_bot Windows Service Setup ===" -ForegroundColor Cyan

# --- 1. Directories ---
foreach ($d in @($STATE_DIR, "$STATE_DIR\Dependencies", $DATA_DIR, "$DATA_DIR\journal", $LOG_DIR, "C:\nifty_bot\secrets")) {
    New-Item -ItemType Directory -Force -Path $d | Out-Null
}
Write-Host "[1/5] Directories OK"

# --- 2. Secrets file check ---
if (-not (Test-Path $SECRETS)) {
    Write-Host "ERROR: $SECRETS not found. Create it from .env.example first." -ForegroundColor Red
    exit 1
}
Write-Host "[2/5] Secrets file found"

# --- 3. Remove old service if exists ---
$existing = ""
try { $existing = & $NSSM status $SVC_NAME 2>&1 } catch {}
if ($existing -and $existing -notmatch "Can't open service" -and $existing -notmatch "not installed") {
    Write-Host "Removing existing service..."
    try { & $NSSM stop $SVC_NAME 2>&1 | Out-Null } catch {}
    try { & $NSSM remove $SVC_NAME confirm 2>&1 | Out-Null } catch {}
}

# --- 4. Install service ---
& $NSSM install $SVC_NAME $PYTHON
& $NSSM set $SVC_NAME AppParameters "C:\nifty_bot\live_bot_v1\main.py --env C:\nifty_bot\secrets\bot.env --state-dir C:\nifty_bot\state --data-dir C:\nifty_bot\data --log-dir C:\nifty_bot\logs"
& $NSSM set $SVC_NAME AppDirectory $BOT_DIR
& $NSSM set $SVC_NAME DisplayName "NIFTY Bot (Paper/Live)"
& $NSSM set $SVC_NAME Description "NIFTY 50 Weekly Option Buying Bot v1"
& $NSSM set $SVC_NAME Start SERVICE_AUTO_START
# Stdout + Stderr to log files (NSSM rotates at 10 MB)
& $NSSM set $SVC_NAME AppStdout "$LOG_DIR\service_stdout.log"
& $NSSM set $SVC_NAME AppStderr "$LOG_DIR\service_stderr.log"
& $NSSM set $SVC_NAME AppRotateFiles 1
& $NSSM set $SVC_NAME AppRotateBytes 10485760
# Restart on failure after 10s, then 30s, then 60s
& $NSSM set $SVC_NAME AppThrottle 5000
& $NSSM set $SVC_NAME AppRestartDelay 10000
Write-Host "[3/5] NSSM service installed"

# --- 5. Load env vars from bot.env into service environment ---
# NSSM can pass env via AppEnvironmentExtra
$envLines = Get-Content $SECRETS | Where-Object { $_ -match "^[A-Z_]+=.+" }
$envString = $envLines -join "`n"
& $NSSM set $SVC_NAME AppEnvironmentExtra $envLines
Write-Host "[4/5] Environment variables loaded from bot.env"

# --- 6. Start service ---
& $NSSM start $SVC_NAME
Start-Sleep -Seconds 3
$status = & $NSSM status $SVC_NAME
Write-Host "[5/5] Service status: $status"

if ($status -eq "SERVICE_RUNNING") {
    Write-Host ""
    Write-Host "=== SETUP COMPLETE ===" -ForegroundColor Green
    Write-Host "Service 'nifty_bot' is running."
    Write-Host ""
    Write-Host "Useful commands:"
    Write-Host "  Check status  : C:\nifty_bot\nssm.exe status nifty_bot"
    Write-Host "  Stop          : C:\nifty_bot\nssm.exe stop nifty_bot"
    Write-Host "  Start         : C:\nifty_bot\nssm.exe start nifty_bot"
    Write-Host "  Restart       : C:\nifty_bot\nssm.exe restart nifty_bot"
    Write-Host "  Remove        : C:\nifty_bot\nssm.exe remove nifty_bot confirm"
    Write-Host ""
    Write-Host "Watch logs (live):"
    Write-Host "  Get-Content C:\nifty_bot\logs\bot.log -Wait -Tail 50"
    Write-Host "  Get-Content C:\nifty_bot\logs\service_stdout.log -Wait -Tail 50"
} else {
    Write-Host "WARNING: Service may not have started. Check:" -ForegroundColor Yellow
    Write-Host "  Get-Content C:\nifty_bot\logs\service_stderr.log -Tail 30"
}
