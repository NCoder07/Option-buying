# NIFTY 50 Weekly Option-Buying Bot — v1

An automated, fully-unattended trading bot for the **NIFTY 50 weekly options** strategy on the NSE. Runs on a Linux VM with [Dhan](https://dhan.co) as the broker, supports **paper mode** by default, and promotes to live only after an explicit double-gate check.

---

## Strategy at a glance

| Parameter | Value |
|---|---|
| Underlying | NIFTY 50 index (NFO) |
| Instruments | Weekly CE + PE options |
| Selection time | 09:20 IST — pick ATM-ish options in the ₹50–₹75 LTP band |
| Entry trigger | LTP ≥ 1.5× selection-time premium (momentum breakout) |
| Stop loss | 0.50× average fill price (software SL) |
| Exit | Next trading day at 09:25 IST (time exit) |
| Sizing | 1 lot default; configurable |

Full specification in [`live_bot_v1/NIFTY_Option_Buying_Strategy_Specification_v1.docx`](live_bot_v1/NIFTY_Option_Buying_Strategy_Specification_v1.docx).

---

## Repository layout

```
live_bot_v1/
├── main.py                  # Entry point (paper / live)
├── botctl.py                # CLI control: status, flatten, resume
├── audit_env.py             # Phase-0 pre-flight audit
├── config/
│   └── config.yaml          # All strategy knobs (no secrets)
├── src/bot/
│   ├── strategy.py          # Core selection + entry + SL loop
│   ├── lifecycle.py         # Day lifecycle orchestrator
│   ├── live_broker.py       # Dhan order adapter
│   ├── paper_broker.py      # Simulated fills (paper mode)
│   ├── replay_broker.py     # Historical replay for dev
│   ├── risk_guard.py        # Circuit breakers
│   ├── clock_guard.py       # NTP drift check
│   ├── ip_guard.py          # Static-IP enforcement
│   ├── state_db.py          # SQLite position/state store
│   ├── journal.py           # trades.csv / daily_log.csv writer
│   ├── alerts.py            # Telegram notifications
│   └── calendar.py          # NSE trading calendar
├── deploy/
│   ├── install.sh           # First-time VM provisioning
│   ├── deploy.sh            # Rolling upgrade (stop→pull→test→start)
│   ├── nifty_bot.service    # systemd unit file
│   └── setup_windows.ps1    # Windows dev environment helper
├── tests/                   # pytest suite
├── .env.example             # Credential template (copy → .env, never commit)
├── requirements.txt
├── requirements-dev.txt
└── RUNBOOK.md               # Operations guide
```

---

## Quick start (paper mode on a Linux VM)

### 1 · Clone and install

```bash
git clone https://github.com/<your-username>/Option-buying.git /tmp/nifty_bot_src
cd /tmp/nifty_bot_src/live_bot_v1
sudo bash deploy/install.sh
```

### 2 · Fill in credentials

```bash
sudo cp .env.example /etc/nifty_bot/bot.env
sudo nano /etc/nifty_bot/bot.env   # fill DHAN_*, TELEGRAM_*, HEARTBEAT_URL, EXPECTED_STATIC_IP
sudo chmod 600 /etc/nifty_bot/bot.env
```

### 3 · Run the pre-flight audit

```bash
source /opt/nifty_bot/.venv/bin/activate
python /opt/nifty_bot/audit_env.py --env /etc/nifty_bot/bot.env
```

### 4 · Start in paper mode

```bash
sudo systemctl start nifty_bot
journalctl -u nifty_bot -f
```

The bot starts in paper mode (`mode: "paper"` in config). It will log in at 08:45 IST, select options at 09:20, simulate entries and SL monitoring, and send Telegram alerts for every event.

---

## Promoting to live

Two independent gates must both be satisfied:

1. `CONFIRM_LIVE=YES` must be set in `/etc/nifty_bot/bot.env`
2. `--live` flag must be passed on the systemd `ExecStart` line

Neither gate alone is sufficient. See [`live_bot_v1/RUNBOOK.md § 3`](live_bot_v1/RUNBOOK.md#3-paper--live-promotion-checklist) for the full checklist (minimum 5 paper days + overnight cycle + recovery tests).

---

## Configuration

All strategy parameters live in [`live_bot_v1/config/config.yaml`](live_bot_v1/config/config.yaml). No secrets in config — credentials are environment variables only.

Key knobs:

| Key | Default | Description |
|---|---|---|
| `ltp_band_low / high` | 50 / 75 | Premium filter at 09:20 |
| `trigger_multiplier` | 1.50 | Entry trigger = P0 × multiplier |
| `sl_multiplier` | 0.50 | Stop loss = fill × multiplier |
| `lots` | 1 | Position size |
| `gap_cross_policy` | `enter` | `enter` or `skip` gap-through events |
| `entry_cutoff_time` | 15:25 | No new entries after this time |

---

## Running tests

```bash
cd live_bot_v1
pip install -r requirements-dev.txt
pytest tests/ -v
```

---

## Safety features

- **IP guard** — blocks orders if outbound IP differs from `EXPECTED_STATIC_IP`
- **Clock guard** — halts new entries if NTP drift > 2 s
- **Circuit breakers** — max orders/day, max consecutive rejections, max daily loss
- **Double live-gate** — env var + CLI flag both required for real money
- **Software SL** — no exchange SL orders (avoids overnight DAY-order expiry risk)
- **Reconciliation** — state restored from SQLite on restart; orphaned positions detected

---

## Disclaimer

> This software is provided for **educational and research purposes only**. Options trading involves substantial risk of loss. Past paper-trade results do not guarantee live-trade performance. You are solely responsible for any financial decisions made using this code. The author(s) accept no liability for trading losses.

---

## License

MIT
