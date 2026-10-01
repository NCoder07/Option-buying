#!/usr/bin/env bash
# deploy.sh — Safe hot-deploy (git pull + reinstall + restart)
#
# SAFETY: Never restarts the service if a position is open near 09:15–09:25
# or during the active entry window (09:20–15:25 IST).
# Usage: sudo bash deploy.sh [--force]
set -euo pipefail

FORCE=${1:-""}
BOT_HOME="/opt/nifty_bot"
STATE_DIR="/var/lib/nifty_bot"
BOT_USER="nifty_bot"

# IST time check
IST_HOUR=$(TZ="Asia/Kolkata" date +%H)
IST_MIN=$(TZ="Asia/Kolkata" date +%M)
IST_TIME="${IST_HOUR}${IST_MIN}"

echo "==> deploy.sh: IST time is ${IST_HOUR}:${IST_MIN}"

if [ -z "$FORCE" ]; then
    # Block deploys during sensitive windows:
    # 09:10–09:30 (overnight exit + selection)
    # 09:30–15:30 (entry monitoring window)
    if [ "$IST_TIME" -ge "0910" ] && [ "$IST_TIME" -le "1530" ]; then
        echo ""
        echo "⚠️  DEPLOY BLOCKED: IST ${IST_HOUR}:${IST_MIN} is within active trading window (09:10–15:30)"
        echo "   Deploy during off-hours or use --force (dangerous if position open)"
        echo ""
        exit 1
    fi
fi

# Check for open positions
OPEN_POSITIONS=$(sqlite3 "$STATE_DIR/bot_state.sqlite" \
    "SELECT COUNT(*) FROM positions WHERE status='OPEN'" 2>/dev/null || echo "0")

if [ "$OPEN_POSITIONS" -gt "0" ] && [ -z "$FORCE" ]; then
    echo ""
    echo "⚠️  DEPLOY BLOCKED: $OPEN_POSITIONS open position(s) in DB."
    echo "   Flatten all positions first or use --force."
    echo ""
    exit 1
fi

echo "==> Stopping service…"
systemctl stop nifty_bot || true

echo "==> Git pull…"
cd "$BOT_HOME"
git pull --ff-only

echo "==> Reinstall dependencies…"
.venv/bin/pip install -r requirements.txt --quiet

echo "==> Running tests…"
.venv/bin/python -m pytest tests/ -q --tb=short || {
    echo "❌ Tests failed — aborting deploy"
    exit 1
}

echo "==> Restarting service…"
systemctl start nifty_bot
sleep 3
systemctl status nifty_bot --no-pager -l | head -20

echo ""
echo "==> Deploy complete. Watch logs: journalctl -u nifty_bot -f"
