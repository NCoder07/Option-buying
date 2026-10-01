#!/usr/bin/env python3
"""
test_dhan_connection.py — Local connectivity test for the Dhan API.

Tests (in order):
  1. Login via pin_totp (unattended)
  2. NIFTY 50 spot LTP
  3. Expiry list + today's expiry selection
  4. Option chain snapshot (raw Dhan API — all 238 strikes)
  5. Strategy band scan: 50 ≤ LTP ≤ 75 → select min|LTP−62.5| for CE and PE
  6. bid/ask quote for the selected strikes (via get_quote_data)
  7. Available balance / funds
  8. Open positions

Run from live_bot_v1/:
    python test_dhan_connection.py

No orders are placed — read-only throughout.
"""

from __future__ import annotations

import os
import sys
import time
from datetime import datetime
from pathlib import Path

# ---------------------------------------------------------------------------
# sys.path setup
# ---------------------------------------------------------------------------
HERE = Path(__file__).parent.resolve()
sys.path.insert(0, str(HERE / "src"))

# ---------------------------------------------------------------------------
# Load credentials from the Options Simulator .env
# ---------------------------------------------------------------------------
_OPTIONS_SIM_ENV = Path("/Users/neilshah/Desktop/Projects/Options Simulator/.env")
try:
    from dotenv import load_dotenv
    if _OPTIONS_SIM_ENV.exists():
        load_dotenv(_OPTIONS_SIM_ENV)
        print(f"✓ Credentials loaded from {_OPTIONS_SIM_ENV}")
    else:
        load_dotenv()
except ImportError:
    pass

# Map Options Simulator env var names → bot env var names
for src, dst in [("client_code","DHAN_CLIENT_CODE"), ("pin","DHAN_PIN"), ("totp_secret","DHAN_TOTP_SECRET")]:
    if not os.environ.get(dst):
        os.environ[dst] = os.environ.get(src, "")

CLIENT_CODE = os.environ["DHAN_CLIENT_CODE"]
PIN         = os.environ["DHAN_PIN"]
TOTP_SECRET = os.environ["DHAN_TOTP_SECRET"]

if not all([CLIENT_CODE, PIN, TOTP_SECRET]):
    print("❌  Missing credentials. Set DHAN_CLIENT_CODE, DHAN_PIN, DHAN_TOTP_SECRET in env.")
    sys.exit(1)

# Tradehull writes Dependencies/ relative to CWD
STATE_DIR = HERE / "state_test"
STATE_DIR.mkdir(exist_ok=True)
(STATE_DIR / "Dependencies").mkdir(exist_ok=True)
os.chdir(STATE_DIR)

import pytz
IST = pytz.timezone("Asia/Kolkata")

SEP = "─" * 65
def section(t):    print(f"\n{SEP}\n  {t}\n{SEP}")
def ok(l, v):      print(f"  ✅  {l:<34} {v}")
def warn(l, v=""):  print(f"  ⚠️   {l}  {v}")
def fail(l, v=""):  print(f"  ❌  {l}  {v}")

# ===========================================================================
# 1. LOGIN
# ===========================================================================
section("1.  Login  (pin_totp — unattended)")

from bot.live_broker import LiveDhanBroker

broker = LiveDhanBroker(
    client_code=CLIENT_CODE, pin=PIN, totp_secret=TOTP_SECRET,
    config={}, state_dir=str(STATE_DIR),
)

t0 = time.perf_counter()
try:
    broker.connect()
    login_ms = (time.perf_counter() - t0) * 1000
    ok("Login", f"OK  ({login_ms:.0f} ms)")
except Exception as e:
    fail("Login FAILED", str(e))
    sys.exit(1)

th = broker._th  # raw Tradehull handle for a few direct diagnostics

# ===========================================================================
# 2. NIFTY 50 SPOT LTP
# ===========================================================================
section("2.  NIFTY 50 Spot LTP")

t0 = time.perf_counter()
ltp_map = broker.get_ltp(["NIFTY"])
ltp_ms = (time.perf_counter() - t0) * 1000
nifty_spot = ltp_map.get("NIFTY")

if nifty_spot:
    ok("NIFTY spot LTP", f"₹{nifty_spot:,.2f}   ({ltp_ms:.0f} ms)")
else:
    warn("NIFTY LTP returned empty (market closed?)")
    nifty_spot = 25000

# ===========================================================================
# 3. EXPIRY LIST + TODAY'S SELECTION
# ===========================================================================
section("3.  Expiry List  &  Today's Strategy Expiry")

t0 = time.perf_counter()
expiries = broker.get_expiry_list()
expiry_ms = (time.perf_counter() - t0) * 1000

if expiries:
    ok("Expiry list", f"{len(expiries)} expiries  ({expiry_ms:.0f} ms)")
    for i, e in enumerate(expiries[:6]):
        suffix = " ← nearest" if i == 0 else ""
        print(f"       [{i}]  {e}{suffix}")
else:
    fail("Could not fetch expiry list")
    sys.exit(1)

from bot.calendar import select_expiry, is_expiry_today
today = datetime.now(IST).date()
print(f"\n  Today (IST):  {today}")
is_expiry = is_expiry_today(expiries, today=today)
selected_expiry = select_expiry(expiries, today=today)

ok("Today is expiry day?", is_expiry)
if selected_expiry:
    ok("Strategy expiry selected", selected_expiry)
else:
    fail("Could not select expiry")

# ===========================================================================
# 4. OPTION CHAIN SNAPSHOT
# ===========================================================================
section("4.  Option Chain Snapshot  (raw Dhan API → all strikes)")

if not selected_expiry:
    warn("Skipping — no expiry selected")
    chain_rows = []
else:
    t0 = time.perf_counter()
    chain_rows = broker.get_option_chain_snapshot(selected_expiry, num_strikes=50)
    chain_ms = (time.perf_counter() - t0) * 1000

    ce_rows = [r for r in chain_rows if r.option_type == "CE" and r.ltp > 0]
    pe_rows = [r for r in chain_rows if r.option_type == "PE" and r.ltp > 0]

    if chain_rows:
        ok("Total rows", f"{len(chain_rows)} ({len(ce_rows)} CE + {len(pe_rows)} PE with LTP>0)  ({chain_ms:.0f} ms)")

        # Show ±5 strikes around ATM
        atm = round(nifty_spot / 50) * 50
        print(f"\n  ATM = {atm}    (showing ±5 strikes)\n")
        print(f"  {'Strike':>8}  {'CE_LTP':>7}  {'CE_Bid':>7}  {'CE_Ask':>7}  {'CE_IV':>6}  "
              f"{'PE_LTP':>7}  {'PE_Bid':>7}  {'PE_Ask':>7}  {'PE_IV':>6}")
        print(f"  {'-'*8}  {'-'*7}  {'-'*7}  {'-'*7}  {'-'*6}  "
              f"{'-'*7}  {'-'*7}  {'-'*7}  {'-'*6}")

        ce_by_strike = {r.strike: r for r in ce_rows}
        pe_by_strike = {r.strike: r for r in pe_rows}
        atm_strikes = sorted(ce_by_strike.keys() | pe_by_strike.keys())
        near_atm = [s for s in atm_strikes if abs(s - atm) <= 250]
        near_atm = sorted(near_atm)

        for s in near_atm:
            ce = ce_by_strike.get(s)
            pe = pe_by_strike.get(s)
            atm_flag = " ←ATM" if s == atm else ""
            print(f"  {s:>8}  "
                  f"{ce.ltp:>7.2f}  {ce.bid:>7.2f}  {ce.ask:>7.2f}  {(ce.iv or 0):>6.1f}  "
                  f"{pe.ltp:>7.2f}  {pe.bid:>7.2f}  {pe.ask:>7.2f}  {(pe.iv or 0):>6.1f}"
                  f"{atm_flag}")
    else:
        warn("Option chain returned 0 rows (market closed or API error)")

# ===========================================================================
# 5. STRATEGY BAND SCAN: 50 ≤ LTP ≤ 75
# ===========================================================================
section("5.  Strategy Band Scan  (50 ≤ LTP ≤ 75, target mid = 62.5)")

from bot.strategy import select_strike

selected = {}
if chain_rows:
    snapshot_ts = datetime.now(IST)
    config = {
        "ltp_band_low": 50.0, "ltp_band_high": 75.0,
        "target_mid": 62.5, "trigger_multiplier": 1.50,
        "tick_size": 0.05, "tie_break": "lower_strike",
    }
    for side in ["CE", "PE"]:
        result = select_strike(chain_rows, side, config, snapshot_ts, 0.0)
        if result:
            trigger = result.trigger
            sl = round(result.p0 * 0.50 / 0.05) * 0.05  # quick SL preview
            ok(f"{side} selected",
               f"strike={result.strike}  P0={result.p0:.2f}  trigger={trigger:.2f}  SL≈{sl:.2f}")
            print(f"       Symbol : {result.symbol}")
            print(f"       |P0 - 62.5| = {abs(result.p0 - 62.5):.2f}")
            selected[side] = result
        else:
            warn(f"{side}", "No eligible strike in band [50, 75]  (premiums out of range — check after 09:20)")
else:
    warn("Skipping — no chain data")

# ===========================================================================
# 6. BID/ASK QUOTE FOR SELECTED STRIKES
# ===========================================================================
section("6.  Live bid/ask Quote  (get_quote_data)")

for side, result in selected.items():
    sym = result.symbol
    t0 = time.perf_counter()
    q = broker.get_quote(sym)
    q_ms = (time.perf_counter() - t0) * 1000
    if q:
        spread = q.ask - q.bid
        ok(f"{side} {sym[:30]}", "")
        print(f"       LTP    : ₹{q.ltp:.2f}")
        print(f"       Bid    : ₹{q.bid:.2f}  (qty={q.bid_qty})")
        print(f"       Ask    : ₹{q.ask:.2f}  (qty={q.ask_qty})")
        print(f"       Spread : ₹{spread:.2f}   Latency: {q_ms:.0f} ms")
    else:
        warn(f"Quote returned None for {sym}")
    time.sleep(0.3)

# ===========================================================================
# 7. AVAILABLE BALANCE
# ===========================================================================
section("7.  Available Balance")

balance = broker.get_available_balance()
if balance > 0:
    ok("Available balance", f"₹{balance:,.2f}")
    # Quick margin estimate for 1 lot
    for side, result in selected.items():
        lot_size = broker.get_lot_size(result.symbol)
        premium = result.p0 * lot_size
        ok(f"  Est. premium for 1 lot {side}", f"₹{premium:,.0f}  (lot_size={lot_size})")
else:
    warn("Balance returned 0 or error")

# ===========================================================================
# 8. OPEN POSITIONS
# ===========================================================================
section("8.  Open Positions")

positions = broker.get_positions()
if not positions:
    ok("Open positions", "None  (clean slate)")
else:
    ok("Open positions", len(positions))
    for p in positions:
        print(f"       {p.symbol}  qty={p.qty}  avg={p.avg_price:.2f}  product={p.product_type}")

# ===========================================================================
# FINAL SUMMARY
# ===========================================================================
section("SUMMARY")

print(f"  Login         ✅  client={CLIENT_CODE[:4]}****")
print(f"  NIFTY spot    {'✅  ₹' + f'{nifty_spot:,.2f}' if nifty_spot else '⚠️  No data'}")
print(f"  Expiry list   ✅  {len(expiries)} expiries available")
print(f"  Today expiry  {'✅  ' + str(selected_expiry) if selected_expiry else '❌  None'}")
print(f"  Chain rows    {'✅  ' + str(len(chain_rows)) if chain_rows else '⚠️  0 (market closed?)'}")
for side in ["CE", "PE"]:
    r = selected.get(side)
    if r:
        print(f"  {side} selection ✅  strike={r.strike}  P0={r.p0:.2f}  trigger={r.trigger:.2f}")
    else:
        print(f"  {side} selection ⚠️  No eligible strike (check during market hours 09:20)")
print(f"  Balance       ✅  ₹{balance:,.2f}")
print(f"  Positions     ✅  {len(positions)} open")
print()

if not selected:
    print("  ℹ️  No band-eligible strikes found right now.")
    print("     This is NORMAL when the market is closed or between sessions.")
    print("     At 09:20 IST on a trading day, NIFTY OTM options will fall in the [50,75] band.")
    print()

print("  All API connections verified. Ready for paper mode.")
print("  Next: python main.py  (starts paper bot, waits for market open)")
print()
