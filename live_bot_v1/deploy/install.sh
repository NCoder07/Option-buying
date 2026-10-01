#!/usr/bin/env bash
# install.sh — First-time installation on the VM
# Run as root or with sudo.
# Usage: sudo bash install.sh
set -euo pipefail

BOT_USER="nifty_bot"
BOT_HOME="/opt/nifty_bot"
STATE_DIR="/var/lib/nifty_bot"
LOG_DIR="/var/log/nifty_bot"
CONFIG_DIR="/etc/nifty_bot"
SERVICE_FILE="/etc/systemd/system/nifty_bot.service"
REPO_DIR="$(cd "$(dirname "$0")/.." && pwd)"

echo "==> Installing NIFTY bot from: $REPO_DIR"

# 1. Create dedicated non-root user
if ! id "$BOT_USER" &>/dev/null; then
    useradd --system --shell /usr/sbin/nologin --home-dir "$BOT_HOME" --create-home "$BOT_USER"
    echo "  Created user: $BOT_USER"
fi

# 2. Create directories
install -d -o "$BOT_USER" -g "$BOT_USER" -m 750 "$BOT_HOME"
install -d -o "$BOT_USER" -g "$BOT_USER" -m 750 "$STATE_DIR"
install -d -o "$BOT_USER" -g "$BOT_USER" -m 750 "$STATE_DIR/data"
install -d -o "$BOT_USER" -g "$BOT_USER" -m 750 "$STATE_DIR/Dependencies"
install -d -o "$BOT_USER" -g "$BOT_USER" -m 750 "$LOG_DIR"
install -d -o root -g root -m 755 "$CONFIG_DIR"
echo "  Directories created"

# 3. Copy code
rsync -a --delete \
    --exclude='__pycache__' --exclude='*.pyc' --exclude='.venv' \
    --exclude='state/' --exclude='data/' --exclude='logs/' \
    "$REPO_DIR/" "$BOT_HOME/"
chown -R "$BOT_USER":"$BOT_USER" "$BOT_HOME"
echo "  Code copied to $BOT_HOME"

# 4. Install Python venv
cd "$BOT_HOME"
python3 -m venv .venv
.venv/bin/pip install --upgrade pip
.venv/bin/pip install -r requirements.txt
chown -R "$BOT_USER":"$BOT_USER" .venv
echo "  Python venv created and packages installed"

# 5. Install config (if not already present)
if [ ! -f "$CONFIG_DIR/config.yaml" ]; then
    cp "$REPO_DIR/config/config.yaml" "$CONFIG_DIR/config.yaml"
    echo "  Config installed at $CONFIG_DIR/config.yaml"
else
    echo "  Config already exists at $CONFIG_DIR/config.yaml — NOT overwritten"
fi

# 6. Install env file (example if not present)
if [ ! -f "$CONFIG_DIR/bot.env" ]; then
    cp "$REPO_DIR/.env.example" "$CONFIG_DIR/bot.env"
    chown root:root "$CONFIG_DIR/bot.env"
    chmod 600 "$CONFIG_DIR/bot.env"
    echo "  IMPORTANT: Edit $CONFIG_DIR/bot.env with real credentials!"
else
    echo "  $CONFIG_DIR/bot.env already exists — NOT overwritten"
fi

# 7. Install systemd service
cp "$REPO_DIR/deploy/nifty_bot.service" "$SERVICE_FILE"
systemctl daemon-reload
echo "  Systemd service installed: $SERVICE_FILE"

# 8. Enable service (but don't start yet — credentials needed first)
systemctl enable nifty_bot
echo "  Service enabled (will start on next boot)"
echo ""
echo "==> Installation complete."
echo ""
echo "NEXT STEPS:"
echo "  1. Edit $CONFIG_DIR/bot.env with real credentials"
echo "  2. Whitelist VM static IP in Dhan portal"
echo "  3. Run: sudo python3 $BOT_HOME/audit_env.py --env $CONFIG_DIR/bot.env"
echo "  4. Start service: sudo systemctl start nifty_bot"
echo "  5. Watch logs: journalctl -u nifty_bot -f"
echo ""
echo "See RUNBOOK.md for full setup instructions."
