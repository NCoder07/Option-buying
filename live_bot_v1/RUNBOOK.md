# RUNBOOK — NIFTY 50 Weekly Option Buying Bot v1

This document covers: first-time setup, daily operations, alert responses, emergency procedures, and upgrade instructions.

---

## Table of Contents

1. [First-Time VM Setup](#1-first-time-vm-setup)
2. [Whitelisting the VM Static IP in Dhan](#2-whitelisting-the-vm-static-ip-in-dhan)
3. [Paper → Live Promotion Checklist](#3-paper--live-promotion-checklist)
4. [Daily Health Checks](#4-daily-health-checks)
5. [Responding to Alerts](#5-responding-to-alerts)
6. [Emergency: Flatten All Positions](#6-emergency-flatten-all-positions)
7. [Restore from Backup](#7-restore-from-backup)
8. [Safe Upgrade Procedure](#8-safe-upgrade-procedure)
9. [Known Limitations & Deviations from Spec](#9-known-limitations--deviations-from-spec)
10. [What to Do Before Going Live with 1 Lot](#10-what-to-do-before-going-live-with-1-lot)

---

## 1. First-Time VM Setup

### 1.1 Prerequisites

- Ubuntu 22.04 or 24.04 LTS (arm64 or x86_64)
- **Static public IP** assigned to the VM (required for Dhan order API)
- Python 3.10+ installed (`python3 --version`)
- `git`, `rsync` installed

### 1.2 Clone and Install

```bash
# As your admin user (with sudo):
git clone <your-repo-url> /tmp/nifty_bot_src
cd /tmp/nifty_bot_src/live_bot_v1
sudo bash deploy/install.sh
```

This will:
- Create `nifty_bot` system user (non-root, no login shell)
- Create `/opt/nifty_bot`, `/var/lib/nifty_bot`, `/var/log/nifty_bot`, `/etc/nifty_bot`
- Install Python venv with all dependencies
- Copy config files
- Install and enable the systemd service (but not start it yet)

### 1.3 Fill in Credentials

```bash
sudo nano /etc/nifty_bot/bot.env
```

Required fields:
```
DHAN_CLIENT_CODE=your_client_id
DHAN_PIN=your_6_digit_pin
DHAN_TOTP_SECRET=your_base32_totp_secret   # from Dhan portal → 2FA → show secret
TELEGRAM_BOT_TOKEN=your_bot_token
TELEGRAM_CHAT_ID=your_chat_id
HEARTBEAT_URL=https://hc-ping.com/your-uuid
EXPECTED_STATIC_IP=x.x.x.x                # VM's static public IP
```

```bash
sudo chmod 600 /etc/nifty_bot/bot.env
sudo chown root:root /etc/nifty_bot/bot.env
```

### 1.4 Run Phase 0 Audit

```bash
source /opt/nifty_bot/.venv/bin/activate
python /opt/nifty_bot/audit_env.py --env /etc/nifty_bot/bot.env
# Check: IP matches, login works, NIFTY LTP returned, balance accessible
```

### 1.5 Start in Paper Mode

```bash
sudo systemctl start nifty_bot
sudo systemctl status nifty_bot
journalctl -u nifty_bot -f    # tail live logs
```

The bot starts in paper mode (config default `mode: "paper"`). It will:
- Log in at 08:45 IST
- Do a full paper day: selection at 09:20, entry simulation, SL monitoring, 09:25 exit next day
- Send Telegram alerts for every event
- Write journal to `/var/lib/nifty_bot/data/journal/`

### 1.6 Harden SSH

```bash
# Disable password auth:
sudo sed -i 's/^PasswordAuthentication yes/PasswordAuthentication no/' /etc/ssh/sshd_config
sudo sed -i 's/^PermitRootLogin yes/PermitRootLogin no/' /etc/ssh/sshd_config
sudo systemctl reload ssh

# Firewall: allow only SSH (ideally from your IP only):
sudo ufw allow from YOUR_IP to any port 22
sudo ufw enable
```

### 1.7 Prevent Reboots During Market Hours

```bash
# Unattended-upgrades: allow security updates but block auto-reboot
# between 08:30 and 15:40 IST (or simply disable auto-reboot):
sudo apt install unattended-upgrades
sudo nano /etc/apt/apt.conf.d/50unattended-upgrades
# Set: Unattended-Upgrade::Automatic-Reboot "false";
```

### 1.8 Setup Chrony (NTP)

```bash
sudo apt install chrony
sudo systemctl enable chrony --now
chronyc tracking   # verify offset < 0.1s
```

---

## 2. Whitelisting the VM Static IP in Dhan

1. Log into **Dhan web portal** → `My Profile` → `API` → `Manage Connections`
2. Find your API key entry
3. Click **"Edit"** → under **"Allowed IPs"**, add your VM's static public IP
4. Save and confirm with OTP
5. Run `audit_env.py` to verify: the IP mismatch check should pass

> ⚠️ Without whitelisting, all order API calls return HTTP 403. Paper mode (data only) may still work.

---

## 3. Paper → Live Promotion Checklist

Complete **all** items before setting `CONFIRM_LIVE=YES`:

- [ ] Paper mode has run at least **5 complete trading days** with no unhandled errors
- [ ] At least one **overnight + next-morning cycle** completed correctly (position survived VM midnight)
- [ ] Deliberate **service kill** (`sudo systemctl kill nifty_bot`) with open paper position recovered correctly
- [ ] **VM reboot** with simulated open position recovered correctly
- [ ] Journals (`trades.csv`, `daily_log.csv`) look correct and match visual chart observation
- [ ] Telegram alerts firing for all events (entry, exit, SL, EOD)
- [ ] Dead-man heartbeat pinging (check your healthchecks.io dashboard)
- [ ] Backup job running (test restore from backup once)
- [ ] IP guard confirmed passing (`audit_env.py`)
- [ ] Clock drift confirmed < 1s (`chronyc tracking`)
- [ ] Account has sufficient funds for at least 2× premium × lot_size (e.g., ₹5,000–₹15,000 for 1 NIFTY lot each side)
- [ ] You have reviewed `reports/api_audit.md` and all caveats addressed
- [ ] Config `mode` set to `"paper"` in `/etc/nifty_bot/config.yaml` (live uses CLI flag only)

**To switch to live:**
```bash
sudo nano /etc/nifty_bot/bot.env
# Add: CONFIRM_LIVE=YES

sudo nano /etc/systemd/system/nifty_bot.service
# Change ExecStart to add: --live flag
# ExecStart=... python /opt/nifty_bot/main.py --live ...

sudo systemctl daemon-reload
sudo systemctl restart nifty_bot
# Watch for the LIVE MODE ACTIVATED banner in logs
```

---

## 4. Daily Health Checks

### Morning (before 08:45 IST):
```bash
# Check service is running:
sudo systemctl status nifty_bot

# Check last night's overnight position (if any):
python /opt/nifty_bot/botctl.py status

# Check disk space:
df -h /var/lib/nifty_bot

# Check NTP:
chronyc tracking | grep "System time"
```

### After 09:20 (selection confirmation):
- Telegram alert: **SELECTED CE** and **SELECTED PE** (or NO_TRADE) should arrive
- Verify strikes and triggers are reasonable

### After 09:25:
- If overnight position: **EXIT** alert should arrive
- `botctl.py status` should show positions CLOSED

### End of Day (~15:40):
- Telegram: **EOD summary** alert
- Check `journal/trades.csv` for today's row

---

## 5. Responding to Alerts

### 🆘 "Bot HALTED: max_consecutive_rejections"
Orders are being rejected by Dhan. Common causes:
1. Token expired mid-session → `sudo systemctl restart nifty_bot`
2. Insufficient funds → add funds to Dhan account
3. IP no longer whitelisted → re-whitelist in Dhan portal
4. Market circuit breaker → wait for market to reopen

```bash
# After fixing root cause:
python /opt/nifty_bot/botctl.py resume
```

### 🆘 "IP GUARD: outbound IP mismatch"
VM IP changed (dynamic IP assigned). Bot is in monitor-only mode.
1. Verify new IP: `curl https://api.ipify.org`
2. Whitelist new IP in Dhan portal
3. Update `EXPECTED_STATIC_IP` in `/etc/nifty_bot/bot.env`
4. Restart: `sudo systemctl restart nifty_bot`

### 🆘 "EXIT FAILED — MANUAL INTERVENTION REQUIRED"
The bot could not close a position after all retries.
1. Log into Dhan mobile/web immediately
2. Navigate to Positions → find the open NIFTY option
3. Place a SELL order manually (LIMIT at bid or MARKET)
4. Update DB to mark position closed:
```bash
sqlite3 /var/lib/nifty_bot/bot_state.sqlite \
  "UPDATE positions SET status='CLOSED', exit_reason='manual_emergency' WHERE status='OPEN'"
sudo systemctl restart nifty_bot
```

### ⚠️ "CLOCK DRIFT exceeds tolerance"
NTP out of sync.
```bash
sudo chronyc makestep   # force immediate sync
chronyc tracking        # verify offset < 1s
python /opt/nifty_bot/botctl.py resume   # re-enable entries
```

### ⚠️ "DISK LOW"
```bash
df -h /var/lib/nifty_bot
# Archive old tick data:
tar -czf /backup/nifty_bot_data_$(date +%Y%m).tar.gz /var/lib/nifty_bot/data/
rm -rf /var/lib/nifty_bot/data/2024-*/    # remove old months
```

### ⚠️ "EXPIRY BREACH ALERT"
Exit date > expiry date for a held contract. This should never happen.
1. Check positions: `python /opt/nifty_bot/botctl.py status`
2. Verify the contract has not expired worthless
3. If still trading: flatten immediately (see Section 6)
4. File a bug report — this indicates a logic error

### ℹ️ "GAP CROSS (gap_cross_policy=enter)"
A gap-through on trigger was detected and entry was taken per config. This is expected behavior. Review the journal row; if you want to skip gap-crosses, change `gap_cross_policy: "skip"` in config and redeploy.

---

## 6. Emergency: Flatten All Positions

**Before flattening:** confirm you understand this will sell ALL open option positions at market.

```bash
# Via botctl (safe — requires manual "CONFIRM" input):
python /opt/nifty_bot/botctl.py flatten
# Type: CONFIRM

# If botctl socket not available (bot crashed):
# 1. Log into Dhan web/mobile and sell manually
# 2. Mark in DB:
sqlite3 /var/lib/nifty_bot/bot_state.sqlite \
  "UPDATE positions SET status='CLOSED', exit_reason='manual_flatten' WHERE status='OPEN'"
```

---

## 7. Restore from Backup

```bash
# Stop the bot:
sudo systemctl stop nifty_bot

# Restore state directory from rclone backup:
rclone copy s3:my-bot-backup/nifty_bot/latest/ /var/lib/nifty_bot/ --progress

# Fix permissions:
sudo chown -R nifty_bot:nifty_bot /var/lib/nifty_bot

# Restart:
sudo systemctl start nifty_bot

# Verify reconciliation passes (check logs):
journalctl -u nifty_bot --since "5 minutes ago"
```

---

## 8. Safe Upgrade Procedure

> **Never deploy while a position is open near 09:15–09:25 or during the active entry window (09:20–15:25 IST).**

```bash
# Check for open positions first:
python /opt/nifty_bot/botctl.py status | grep '"position"'

# If no positions, deploy:
sudo bash /opt/nifty_bot/deploy/deploy.sh

# If positions exist, wait until after 09:25 (time exit) and then deploy.
# If urgent, use --force flag with extreme care:
sudo bash /opt/nifty_bot/deploy/deploy.sh --force
```

The deploy script: stops service → git pull → pip install → runs tests → restarts service.

---

## 9. Known Limitations & Deviations from Spec

| Item | Detail |
|---|---|
| **Entry cutoff** | Spec has no explicit cutoff. Bot defaults to `entry_cutoff_time: "15:25:00"`. Entries after 15:00 are flagged `late_entry` in journal. |
| **WebSocket** | Phase 1 uses REST polling (~1 Hz). MarketFeed WS is available in dhanhq and can be wired in a future PR without changing strategy logic. |
| **Exchange SL** | `use_exchange_sl: false` (default). Exchange SL orders would need re-arming each morning (DAY orders expire); this adds complexity and dual-fire risk. Software SL is preferred. |
| **Tradehull CWD hack** | `Tradehull` hardcodes `Dependencies/` relative to CWD. Bot calls `os.chdir(state_dir)` at startup. This is a library limitation. |
| **Option chain expiry by index** | `get_option_chain(expiry_idx)` takes an index (0=nearest), not a date string. Bot translates via `expiry_list.index(expiry_date)`. |
| **`botctl` socket** | Implemented as Unix domain socket in `lifecycle.py`. The socket server code is scaffolded — wire `DayLifecycle.status_snapshot()`, `flatten_all()`, etc. into the socket handler if needed. |
| **Backup** | `backup_enabled: false` by default. Enable by setting to `true` and configuring `backup_rclone_remote` in config.yaml. |
| **ReplayBroker** | Reads candles from `DATA_DIR`. Works with the Options Simulator parquet format. Run with `BOT_DATA_DIR=/path/to/your/data python main.py` (paper mode, no Dhan creds needed for replay). |

---

## 10. What to Do Before Going Live with 1 Lot

1. **Complete paper forward test** — minimum 5 days, including one overnight cycle
2. **Run `audit_env.py`** on VM — confirm GO verdict
3. **Whitelist VM IP** in Dhan portal
4. **Fund the account** — ensure ≥ 2× expected premium × 75 (e.g., ₹10,500 for 1 lot at ₹70 average)
5. **Test Telegram alerts** — confirm all event types arrive on your phone
6. **Test heartbeat** — confirm healthchecks.io shows green
7. **Test service restart** — `sudo systemctl kill nifty_bot` with a paper open position; confirm recovery
8. **Test VM reboot** — `sudo reboot`; confirm bot restarts and loads overnight state
9. **Review journal** — `trades.csv` should show realistic slippage (fills close to ask on entry, close to bid on exit)
10. **Confirm mode switch** — add `--live` to systemd ExecStart AND `CONFIRM_LIVE=YES` to bot.env; verify live banner appears in logs
11. **Start with 1 lot** — `lots: 1` in config; never increase without a full review of forward-test results

> **Remember:** This is a forward test, not a live scalp. The goal is data fidelity — matching fills, slippage, and gap behavior to your backtest assumptions. Treat every deviation as signal, not noise.
