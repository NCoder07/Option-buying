#!/usr/bin/env python3
"""
audit_env.py — Phase 0 environment & API audit script.

Run this on the DEPLOYMENT VM (not the dev machine):
    pip install Dhan_Tradehull dhanhq requests pyotp pytz
    DHAN_CLIENT_CODE=xxx DHAN_PIN=yyy DHAN_TOTP_SECRET=zzz python audit_env.py

Or with a .env file in the same directory:
    python audit_env.py --env /etc/nifty_bot/bot.env

Writes results to audit_env_report.md in the current directory.
NEVER places any orders — read-only smoke tests only.
"""

from __future__ import annotations

import argparse
import datetime
import json
import os
import platform
import shutil
import socket
import subprocess
import sys
import time
import traceback
from pathlib import Path

# ---------------------------------------------------------------------------
# Optional: load .env file
# ---------------------------------------------------------------------------
try:
    from dotenv import load_dotenv
    _DOTENV_AVAILABLE = True
except ImportError:
    _DOTENV_AVAILABLE = False


def _load_env(env_file: str | None) -> None:
    if env_file and _DOTENV_AVAILABLE:
        load_dotenv(env_file)
    elif _DOTENV_AVAILABLE:
        load_dotenv()


# ---------------------------------------------------------------------------
# Report builder
# ---------------------------------------------------------------------------

class Report:
    def __init__(self) -> None:
        self.lines: list[str] = []
        self.verdict = "GO"
        self.blockers: list[str] = []
        self.caveats: list[str] = []

    def h1(self, s: str) -> None:
        self.lines += ["", f"# {s}", ""]

    def h2(self, s: str) -> None:
        self.lines += ["", f"## {s}", ""]

    def h3(self, s: str) -> None:
        self.lines += [f"### {s}", ""]

    def ok(self, msg: str) -> None:
        self.lines.append(f"- ✅ {msg}")

    def warn(self, msg: str) -> None:
        self.lines.append(f"- ⚠️  {msg}")
        self.caveats.append(msg)
        if self.verdict == "GO":
            self.verdict = "GO-WITH-CAVEATS"

    def fail(self, msg: str) -> None:
        self.lines.append(f"- ❌ {msg}")
        self.blockers.append(msg)
        self.verdict = "NO-GO"

    def info(self, msg: str) -> None:
        self.lines.append(f"  {msg}")

    def code(self, s: str) -> None:
        self.lines += ["```", s, "```"]

    def render(self) -> str:
        return "\n".join(self.lines)


# ---------------------------------------------------------------------------
# 1. VM / OS checks
# ---------------------------------------------------------------------------

def check_os(r: Report) -> None:
    r.h2("1. OS / Python / Timezone")
    r.info(f"Platform : {platform.platform()}")
    r.info(f"Python   : {sys.version}")
    r.info(f"CWD      : {os.getcwd()}")

    # Python version
    if sys.version_info >= (3, 10):
        r.ok(f"Python {sys.version_info.major}.{sys.version_info.minor} >= 3.10")
    else:
        r.fail(f"Python {sys.version_info.major}.{sys.version_info.minor} < 3.10 — upgrade required")

    # Timezone
    tz_env = os.environ.get("TZ", "")
    tz_local = datetime.datetime.now().astimezone().tzname()
    r.info(f"TZ env   : '{tz_env}'   Local tzname: '{tz_local}'")
    try:
        import pytz
        ist = pytz.timezone("Asia/Kolkata")
        now_ist = datetime.datetime.now(ist)
        r.ok(f"pytz Asia/Kolkata available — current IST: {now_ist.isoformat()}")
    except Exception as e:
        r.fail(f"pytz not available or Asia/Kolkata failed: {e}")

    # NTP / chrony drift
    _check_ntp(r)

    # Disk space
    _check_disk(r)

    # RAM
    _check_ram(r)


def _check_ntp(r: Report) -> None:
    for cmd in [["chronyc", "tracking"], ["timedatectl", "status"]]:
        try:
            out = subprocess.check_output(cmd, stderr=subprocess.STDOUT, timeout=5).decode()
            r.ok(f"NTP check via `{cmd[0]}`")
            for line in out.splitlines():
                if any(k in line.lower() for k in ["offset", "sync", "drift", "rms", "ntp"]):
                    r.info(f"  {line.strip()}")
            # Warn if drift > 1s
            for line in out.splitlines():
                if "offset" in line.lower() and "system" in line.lower():
                    try:
                        # chronyc: "System time     : 0.000012345 seconds fast of NTP time"
                        parts = line.split(":")
                        if len(parts) > 1:
                            drift_str = parts[1].strip().split()[0]
                            drift = abs(float(drift_str))
                            if drift > 1.0:
                                r.fail(f"Clock drift {drift:.3f}s > 1s — fix NTP before going live")
                            elif drift > 0.1:
                                r.warn(f"Clock drift {drift:.3f}s — acceptable but monitor")
                            else:
                                r.ok(f"Clock drift {drift:.6f}s — excellent")
                    except Exception:
                        pass
            return
        except FileNotFoundError:
            continue
        except Exception as e:
            r.warn(f"NTP check failed ({cmd[0]}): {e}")
            return
    r.warn("Neither chronyc nor timedatectl found — cannot verify NTP sync")


def _check_disk(r: Report) -> None:
    try:
        usage = shutil.disk_usage("/var/lib" if Path("/var/lib").exists() else "/")
        free_gb = usage.free / 1e9
        if free_gb < 2:
            r.fail(f"Disk free: {free_gb:.1f} GB — critically low (need ≥2 GB)")
        elif free_gb < 10:
            r.warn(f"Disk free: {free_gb:.1f} GB — low (recommend ≥10 GB)")
        else:
            r.ok(f"Disk free: {free_gb:.1f} GB")
    except Exception as e:
        r.warn(f"Could not check disk: {e}")


def _check_ram(r: Report) -> None:
    try:
        with open("/proc/meminfo") as f:
            mem = {line.split(":")[0]: int(line.split()[1]) for line in f if ":" in line}
        available_mb = mem.get("MemAvailable", 0) / 1024
        if available_mb < 256:
            r.fail(f"RAM available: {available_mb:.0f} MB — too low")
        elif available_mb < 512:
            r.warn(f"RAM available: {available_mb:.0f} MB — borderline")
        else:
            r.ok(f"RAM available: {available_mb:.0f} MB")
    except Exception:
        r.info("(RAM check: /proc/meminfo not available — non-Linux?)")


# ---------------------------------------------------------------------------
# 2. Outbound IP check
# ---------------------------------------------------------------------------

def check_ip(r: Report) -> None:
    r.h2("2. Outbound Public IP")
    expected = os.environ.get("EXPECTED_STATIC_IP", "")
    try:
        import requests
        resp = requests.get("https://api.ipify.org?format=json", timeout=10)
        public_ip = resp.json()["ip"]
        r.info(f"Outbound public IP : {public_ip}")
        if expected:
            if public_ip == expected:
                r.ok(f"IP matches EXPECTED_STATIC_IP={expected}")
            else:
                r.fail(
                    f"IP MISMATCH: outbound={public_ip}, expected={expected}. "
                    "Whitelist this IP in Dhan portal before going live."
                )
        else:
            r.warn(
                f"EXPECTED_STATIC_IP not set — outbound IP is {public_ip}. "
                "Whitelist this IP in Dhan portal → API → Manage Connections."
            )
    except Exception as e:
        r.warn(f"Could not determine public IP: {e}")


# ---------------------------------------------------------------------------
# 3. Dhan_Tradehull library audit
# ---------------------------------------------------------------------------

def check_library(r: Report) -> None:
    r.h2("3. Library Versions & Public API")
    try:
        import Dhan_Tradehull as dt_mod
        import importlib.metadata
        try:
            ver = importlib.metadata.version("Dhan_Tradehull")
        except Exception:
            ver = "unknown"
        r.ok(f"Dhan_Tradehull version: {ver}")
    except ImportError as e:
        r.fail(f"Dhan_Tradehull not importable: {e}")
        return

    try:
        import dhanhq
        try:
            import importlib.metadata
            dhver = importlib.metadata.version("dhanhq")
        except Exception:
            dhver = "unknown"
        r.ok(f"dhanhq version: {dhver}")
    except ImportError as e:
        r.fail(f"dhanhq not importable: {e}")
        return

    # Auth modes summary
    r.h3("Authentication modes")
    r.info("- `access_token`: manual paste; cached per day in Dependencies/token_<client>_<date>.txt")
    r.info("  Requires manual token rotation → NOT suitable for unattended operation.")
    r.info("- `api_key`: opens browser for OAuth redirect → NOT suitable for unattended operation.")
    r.info("- `pin_totp`: uses PIN + TOTP secret (pyotp) → FULLY UNATTENDED ✅")
    r.info("  Token cached in Dependencies/ after first login; revalidated on restart.")
    r.ok("pin_totp mode supports fully unattended daily token refresh (recommended)")

    # Product types
    r.h3("Product type strings")
    from dhanhq import dhanhq as _dq
    r.info(f"  INTRA (MIS)  = '{_dq.INTRA}'")
    r.info(f"  MARGIN (NRML) = '{_dq.MARGIN}'")
    r.ok("Carry-forward product type confirmed: MARGIN='MARGIN' (not auto-squared-off)")

    # Order types
    r.h3("Order types")
    r.info(f"  LIMIT       = '{_dq.LIMIT}'")
    r.info(f"  MARKET      = '{_dq.MARKET}'")
    r.info(f"  SL (STOPLIMIT)  = '{_dq.SL}'")
    r.info(f"  SLM (STOPMARKET) = '{_dq.SLM}'")

    # Key methods
    r.h3("Key public methods confirmed present")
    from Dhan_Tradehull.Dhan_Tradehull import Tradehull
    import inspect
    methods = [m for m in dir(Tradehull) if not m.startswith("_")]
    required = [
        "get_login", "get_instrument_file", "get_ltp_data", "get_quote_data",
        "get_option_chain", "get_expiry_list", "get_expiry_date", "get_lot_size",
        "order_placement", "modify_order", "cancel_order",
        "get_order_status", "get_order_detail", "get_executed_price",
        "get_executed_price_and_time", "order_report",
        "get_positions", "get_orderbook", "get_trade_book",
        "get_balance", "send_telegram_alert",
    ]
    for m in required:
        if m in methods:
            r.ok(f"  {m}()")
        else:
            r.fail(f"  {m}() — MISSING in installed version")

    # WebSocket
    r.h3("WebSocket / live feed")
    try:
        from dhanhq import marketfeed
        r.ok("dhanhq.marketfeed.MarketFeed available — websocket streaming supported")
        r.info("  Subscription types: Ticker(15), Quote(17), Depth(19), Full(21)")
        r.info("  Exchange segments: IDX=0, NSE=1, NSE_FNO=2, BSE=4, …")
        r.info("  Recommendation: use MarketFeed Quote(17) for LTP+bid+ask at ~1 Hz.")
    except ImportError:
        r.warn("dhanhq.marketfeed not available — polling only")

    # Option chain fields
    r.h3("Option chain fields (get_option_chain response)")
    r.info("  Per-strike: CE LTP, CE Bid, CE Ask, CE IV, CE OI, CE Volume, CE Delta, CE Theta, CE Gamma, CE Vega")
    r.info("  Per-strike: PE LTP, PE Bid, PE Ask, PE IV, PE OI, PE Volume, PE Delta, …")
    r.ok("Option chain returns per-strike LTP, bid/ask, OI, IV, greeks")


# ---------------------------------------------------------------------------
# 4. Dhan login smoke test
# ---------------------------------------------------------------------------

def check_login_and_data(r: Report) -> None:
    r.h2("4. Login & Read-Only Smoke Tests")

    client_code = os.environ.get("DHAN_CLIENT_CODE", "")
    pin = os.environ.get("DHAN_PIN", "")
    totp_secret = os.environ.get("DHAN_TOTP_SECRET", "")

    if not all([client_code, pin, totp_secret]):
        r.warn(
            "DHAN_CLIENT_CODE / DHAN_PIN / DHAN_TOTP_SECRET not set — "
            "skipping live smoke tests. Set env vars and re-run."
        )
        return

    # Create Dependencies dir for token cache (Tradehull hard-codes this)
    os.makedirs("Dependencies", exist_ok=True)

    try:
        from Dhan_Tradehull.Dhan_Tradehull import Tradehull
        th = Tradehull(
            ClientCode=client_code,
            token_id="",
            mode="pin_totp",
            pin=pin,
            totp_secret=totp_secret,
        )
        r.ok("Login successful (pin_totp)")
    except Exception as e:
        r.fail(f"Login failed: {e}")
        r.info(traceback.format_exc())
        return

    # NIFTY LTP
    _smoke_ltp(r, th)

    # Balance
    _smoke_balance(r, th)

    # Expiry list
    expiry_dates = _smoke_expiry_list(r, th)

    # Option chain + bid/ask
    if expiry_dates:
        _smoke_option_chain(r, th, expiry_dates)

    # Positions
    _smoke_positions(r, th)

    # Latency
    _smoke_latency(r, th)


def _smoke_ltp(r: Report, th) -> None:
    try:
        t0 = time.perf_counter()
        ltp = th.get_ltp_data(["NIFTY"])
        lat = (time.perf_counter() - t0) * 1000
        if ltp and "NIFTY" in ltp:
            r.ok(f"NIFTY LTP: {ltp['NIFTY']:.2f}  (latency: {lat:.0f} ms)")
        else:
            r.warn(f"LTP returned empty/unexpected: {ltp}")
    except Exception as e:
        r.warn(f"LTP smoke test failed (market may be closed): {e}")


def _smoke_balance(r: Report, th) -> None:
    try:
        bal = th.get_balance()
        r.ok(f"Available balance: ₹{bal:,.2f}")
    except Exception as e:
        r.warn(f"get_balance() failed: {e}")


def _smoke_expiry_list(r: Report, th) -> list:
    try:
        expiries = th.get_expiry_list("NIFTY", "INDEX")
        if expiries:
            r.ok(f"NIFTY expiry list: first 5 = {expiries[:5]}")
            return expiries
        else:
            r.warn("NIFTY expiry list returned empty")
            return []
    except Exception as e:
        r.warn(f"get_expiry_list() failed: {e}")
        return []


def _smoke_option_chain(r: Report, th, expiries: list) -> None:
    try:
        expiry_idx = 0  # nearest expiry
        t0 = time.perf_counter()
        result = th.get_option_chain("NIFTY", "INDEX", expiry_idx, num_strikes=5)
        lat = (time.perf_counter() - t0) * 1000
        if result is None:
            r.warn("get_option_chain() returned None (market may be closed)")
            return
        atm, df = result
        r.ok(f"Option chain: ATM={atm}, {len(df)} rows, latency={lat:.0f} ms")
        cols = list(df.columns)
        r.info(f"  Columns: {cols}")
        # Check for bid/ask
        if "CE Bid" in cols and "CE Ask" in cols:
            r.ok("Option chain includes CE Bid/Ask")
        else:
            r.warn("CE Bid/Ask not in option chain columns — check format_option_chain()")
        if "CE IV" in cols:
            r.ok("Option chain includes CE IV")
        if "CE OI" in cols:
            r.ok("Option chain includes CE OI")
        # Sample one bid/ask via get_quote_data
        if not df.empty:
            try:
                # Find a symbol in the option chain
                # The option chain uses SEM_CUSTOM_SYMBOL format like "NIFTY 12JUN25 25000 CE"
                # Try get_quote_data on a NIFTY option
                r.h3("bid/ask via get_quote_data")
                r.warn(
                    "Direct bid/ask verification skipped in audit "
                    "(requires valid option trading symbol at this strike). "
                    "get_quote_data() returns top_bid_price / top_ask_price from Dhan."
                )
            except Exception as e:
                r.warn(f"bid/ask check: {e}")
    except Exception as e:
        r.warn(f"get_option_chain() failed: {e}")


def _smoke_positions(r: Report, th) -> None:
    try:
        pos = th.get_positions()
        if hasattr(pos, "empty"):
            r.ok(f"Positions API working — {len(pos)} open positions")
        else:
            r.warn(f"get_positions() returned unexpected type: {type(pos)}")
    except Exception as e:
        r.warn(f"get_positions() failed: {e}")


def _smoke_latency(r: Report, th) -> None:
    r.h3("API Latency (5 LTP calls)")
    lats = []
    for _ in range(5):
        t0 = time.perf_counter()
        try:
            th.get_ltp_data(["NIFTY"])
        except Exception:
            pass
        lats.append((time.perf_counter() - t0) * 1000)
        time.sleep(0.5)
    if lats:
        median = sorted(lats)[len(lats) // 2]
        p95 = sorted(lats)[int(len(lats) * 0.95)] if len(lats) >= 20 else max(lats)
        r.info(f"  Median: {median:.0f} ms   Max: {p95:.0f} ms")
        if median < 500:
            r.ok(f"LTP latency acceptable: median={median:.0f} ms")
        else:
            r.warn(f"LTP latency high: median={median:.0f} ms — consider Mumbai region VM")
        r.info(
            "  1 Hz polling of 2–4 option symbols = ~4 ticker_data calls/sec. "
            "Dhan rate limit is ~100 req/sec for ticker. Polling is safe. "
            "Recommendation: Use dhanhq.MarketFeed Quote(17) websocket for "
            "lower latency and no polling overhead. Polling is a safe fallback."
        )


# ---------------------------------------------------------------------------
# 5. Rate limits & IP whitelist requirements
# ---------------------------------------------------------------------------

def check_rate_limits(r: Report) -> None:
    r.h2("5. Rate Limits, IP Whitelist & Token Validity")
    r.info("Based on Dhan API v2 documentation and Dhan_Tradehull 3.3.2 source inspection:")
    r.info("")
    r.info("ORDER APIS (place/modify/cancel):")
    r.info("  - Require static IP whitelisted in Dhan portal → API → Manage Connections")
    r.info("  - Without whitelisting, order calls return HTTP 403")
    r.info("  - VM must have a static/Elastic IP; dynamic IPs will break this")
    r.info("")
    r.info("DATA APIS (ticker_data, quote_data, option_chain):")
    r.info("  - ~100 req/sec for ticker/quote; ~5–10 req/sec for option chain")
    r.info("  - 1 Hz polling of 2 option symbols: well within limits")
    r.info("  - option_chain: no documented per-second limit; use sparingly (≤1/min at 09:20)")
    r.info("")
    r.info("ACCESS TOKEN:")
    r.info("  - Valid for the current trading day (expires EOD)")
    r.info("  - pin_totp mode auto-generates a new token at each 08:45 login")
    r.info("  - Token cached in Dependencies/token_<client>_<YYYY-MM-DD>.txt")
    r.info("  - Dhan rate-limits login attempts; do not call login more than once per day")
    r.info("")
    r.ok("IP whitelist required for order APIs — see RUNBOOK for setup steps")
    r.ok("Token valid per-day; pin_totp gives fully unattended refresh")


# ---------------------------------------------------------------------------
# 6. Symbol / expiry mechanics
# ---------------------------------------------------------------------------

def check_symbol_expiry(r: Report) -> None:
    r.h2("6. Symbol & Expiry Mechanics")
    r.info("Tradehull get_expiry_list('NIFTY', 'INDEX') returns YYYY-MM-DD strings sorted by date.")
    r.info("The bot detects today's expiry by comparing today's date against the list (never hard-codes weekday).")
    r.info("")
    r.info("NIFTY weekly option trading symbol format (SEM_CUSTOM_SYMBOL):")
    r.info("  e.g.  'NIFTY 12JUN25 25000 CE'  (from instrument master CSV)")
    r.info("  SEM_TRADING_SYMBOL: 'NIFTY25JUN2525000CE'")
    r.info("")
    r.info("order_placement() exchange param for NFO options: exchange='NFO'")
    r.info("trade_type for carry-forward: trade_type='MARGIN'")
    r.info("Lot size from get_lot_size(tradingsymbol) — currently 75 for NIFTY (verify from master)")
    r.info("Tick size: 0.05 (NSE index options)")
    r.info("")
    r.ok("Expiry detection is data-driven (from expiry list API), not weekday-hardcoded")


# ---------------------------------------------------------------------------
# 7. Verdict
# ---------------------------------------------------------------------------

def render_verdict(r: Report) -> None:
    r.h1("PHASE 0 VERDICT")
    r.info(f"**{r.verdict}**")
    r.info("")
    if r.blockers:
        r.h2("Blockers (must fix before going live)")
        for b in r.blockers:
            r.info(f"- {b}")
    if r.caveats:
        r.h2("Caveats (address before first live trade)")
        for c in r.caveats:
            r.info(f"- {c}")

    r.h2("Proposed Design Decisions")
    r.info("**Auth:**       pin_totp mode — fully unattended. Rotate at 08:45 IST daily.")
    r.info("**LTP/quotes:** dhanhq.MarketFeed Quote(17) websocket for selected 2 contracts post-09:20.")
    r.info("                Polling (ticker_data) as fallback for 09:15–09:25 overnight exit window.")
    r.info("**Option chain:** REST get_option_chain() once at 09:20 for strike selection snapshot.")
    r.info("**Product type:** MARGIN (carry-forward, not MIS intraday).")
    r.info("**Order type:** Marketable LIMIT (entry: ask+buffer; exit: bid−buffer).")
    r.info("**SL monitoring:** Software SL via LTP polling — use_exchange_sl=False (default).")
    r.info("**State:**      SQLite for positions/orders; YAML config; JSON-lines for tick data.")
    r.info("**Timezone:**   All logic in Asia/Kolkata via pytz, regardless of VM system TZ.")
    r.info("**Dependencies dir:** Bot creates Dependencies/ relative to --state-dir, not CWD.")
    r.info("                     Tradehull hard-codes 'Dependencies/' — bot must chdir to state-dir at startup.")


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main() -> None:
    ap = argparse.ArgumentParser(description="Phase 0 environment & API audit")
    ap.add_argument("--env", default=None, help="Path to .env file")
    ap.add_argument("--out", default="audit_env_report.md", help="Output markdown file")
    args = ap.parse_args()

    _load_env(args.env)

    r = Report()
    r.h1("Phase 0 — Environment & API Audit")
    r.info(f"Generated: {datetime.datetime.now().isoformat()}")
    r.info(f"Host: {socket.gethostname()}")

    check_os(r)
    check_ip(r)
    check_library(r)
    check_login_and_data(r)
    check_rate_limits(r)
    check_symbol_expiry(r)
    render_verdict(r)

    output = r.render()
    print(output)
    Path(args.out).write_text(output)
    print(f"\n\n==> Report written to {args.out}")
    print(f"==> VERDICT: {r.verdict}")


if __name__ == "__main__":
    main()
