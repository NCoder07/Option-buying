"""
lifecycle.py — Daily lifecycle orchestrator for the NIFTY bot.

Implements the full trading day state machine:

  08:45  login → instrument load → reconcile → overnight position check
  09:15  start monitoring overnight positions from first tick
  09:20  option chain snapshot → strike selection → persist
  09:25  mandatory time exit of overnight positions
  09:20+ poll selected contracts → detect fresh cross → entry → SL arm
  15:25  entry cutoff
  15:35  persist state → go idle (service stays running)
  next day 09:15 → repeat

The bot uses a 1-second polling loop.  WebSocket integration can replace
the polling calls by updating a shared quote cache from a background thread.

All time comparisons are tz-aware Asia/Kolkata datetimes.
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
import signal
import time
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Optional

import pytz
import yaml

from .alerts import Alerts
from .broker_port import BrokerPort, Fill, OrderStatus
from .calendar import is_expiry_today, is_trading_day, select_expiry, today_ist, dte
from .clock_guard import ClockGuard
from .ip_guard import IPGuard
from .journal import Journal, compute_pnl
from .risk_guard import RiskGuard, RiskState
from .state_db import DailyStateRecord, PositionRecord, StateDB
from .strategy import (
    SelectionResult,
    compute_exit_price,
    compute_sl,
    execute_entry,
    execute_exit,
    is_fresh_cross,
    is_sl_breached,
    select_strike,
)

logger = logging.getLogger(__name__)
IST = pytz.timezone("Asia/Kolkata")
SIDES = ["CE", "PE"]


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _now_ist() -> datetime:
    return datetime.now(IST)


def _ist(h: int, m: int, s: int = 0, d: Optional[date] = None) -> datetime:
    """Return a tz-aware IST datetime for today (or given date) at h:m:s."""
    if d is None:
        d = today_ist()
    return IST.localize(datetime(d.year, d.month, d.day, h, m, s))


def _parse_time(s: str) -> tuple[int, int, int]:
    """Parse 'HH:MM:SS' or 'HH:MM' into (h, m, s)."""
    parts = s.split(":")
    h, m = int(parts[0]), int(parts[1])
    s_ = int(parts[2]) if len(parts) > 2 else 0
    return h, m, s_


def _config_hash(config: dict) -> str:
    return hashlib.md5(json.dumps(config, sort_keys=True).encode()).hexdigest()[:8]


def _get_code_version() -> str:
    try:
        import subprocess
        return subprocess.check_output(
            ["git", "rev-parse", "--short", "HEAD"],
            stderr=subprocess.DEVNULL,
        ).decode().strip()
    except Exception:
        return "unknown"


# ---------------------------------------------------------------------------
# Reconciliation
# ---------------------------------------------------------------------------

def reconcile(
    broker: BrokerPort,
    db: StateDB,
    alerts: Alerts,
    today: date,
) -> bool:
    """
    Compare DB state with broker's order list and positions.
    Returns True if reconciliation passed (or no open positions).
    On unknown order state: halts new entries + alerts.
    """
    logger.info("Reconciling with broker…")
    open_positions = db.get_open_positions()
    if not open_positions:
        logger.info("Reconcile: no open DB positions")
        return True

    broker_positions = broker.get_positions()
    broker_symbols = {p.symbol for p in broker_positions}

    for pos in open_positions:
        if pos.symbol not in broker_symbols:
            # Position in DB but not at broker — could have been manually closed
            # or this is from a prior day that was already exited.
            logger.warning(
                "Reconcile: DB position %s not found at broker. "
                "Marking closed with reason='reconcile_missing'.",
                pos.symbol,
            )
            pos.status = "CLOSED"
            pos.exit_reason = "reconcile_missing"
            pos.closed_at = _now_ist().isoformat()
            db.upsert_position(pos)
            alerts.send(
                f"Reconcile: DB position {pos.symbol} not found at broker — marked closed. "
                "Verify manually.",
                level="WARN",
            )

        # Check order status for any pending entry orders
        if pos.entry_order_id:
            fill = broker.get_order_status(pos.entry_order_id)
            if fill.status == OrderStatus.UNKNOWN:
                msg = (
                    f"Reconcile: unknown order status for {pos.entry_order_id} "
                    f"({pos.symbol}) — halting new entries"
                )
                logger.error(msg)
                alerts.halt(msg)
                return False

    logger.info("Reconcile passed (%d open positions)", len(open_positions))
    return True


# ---------------------------------------------------------------------------
# Lifecycle phases
# ---------------------------------------------------------------------------

class DayLifecycle:
    """
    Manages one trading day's complete lifecycle.

    Instantiated at market-open each day; reused across overnight periods.
    """

    def __init__(
        self,
        broker: BrokerPort,
        db: StateDB,
        config: dict,
        alerts: Alerts,
        risk: RiskGuard,
        journal: Journal,
        ip_guard: IPGuard,
        clock_guard: ClockGuard,
        mode: str,
    ) -> None:
        self._broker = broker
        self._db = db
        self._cfg = config
        self._alerts = alerts
        self._risk = risk
        self._journal = journal
        self._ip_guard = ip_guard
        self._clock_guard = clock_guard
        self._mode = mode

        # Per-day state
        self._today: date = today_ist()
        self._today_str: str = str(self._today)
        self._expiry: Optional[str] = None
        self._selections: dict[str, Optional[SelectionResult]] = {s: None for s in SIDES}
        self._prev_ltp: dict[str, Optional[float]] = {s: None for s in SIDES}
        self._entry_done: dict[str, bool] = {s: False for s in SIDES}
        self._open_positions: dict[str, Optional[PositionRecord]] = {s: None for s in SIDES}
        self._reference_established: dict[str, bool] = {s: False for s in SIDES}
        self._lot_sizes: dict[str, int] = {}

    # ------------------------------------------------------------------
    # 08:45 — Morning init
    # ------------------------------------------------------------------

    def morning_init(self) -> bool:
        """
        08:45: login, instrument load, expiry selection, reconcile, overnight check.
        Returns True if ready to trade.
        """
        logger.info("=== Morning init: %s ===", self._today)
        self._today = today_ist()
        self._today_str = str(self._today)

        # IP guard
        if not self._ip_guard.check(mode=self._mode):
            self._alerts.halt("IP mismatch — live entries refused")
            if self._mode == "live":
                return False

        # Clock guard
        if not self._clock_guard.check():
            self._alerts.halt("Clock drift too high")
            self._risk.stop_entries("clock_drift")

        # Check it's a trading day
        if not is_trading_day(self._today):
            logger.info("Today (%s) is not a trading day — skipping", self._today)
            return False

        # Reconnect / refresh token
        try:
            self._broker.reconnect_if_needed()
            self._alerts.login_ok()
        except Exception as e:
            self._alerts.login_failed(str(e))
            return False

        # Load expiry list and decide today's expiry
        expiry_list = self._broker.get_expiry_list()
        if not expiry_list:
            logger.error("Could not get expiry list")
            return False
        self._expiry = select_expiry(expiry_list, today=self._today)
        if not self._expiry:
            logger.error("Could not select expiry for %s", self._today)
            return False
        logger.info("Today's expiry: %s", self._expiry)

        # Assert exit date <= expiry date for overnight positions
        self._check_expiry_integrity()

        # Reconcile with broker
        if not reconcile(self._broker, self._db, self._alerts, self._today):
            return False

        # Load overnight open positions
        for side in SIDES:
            daily = self._db.get_or_create_daily_state(self._today_str, side)
            if daily.status in ("ENTERED",):
                # Position from today still open
                pass  # will be handled in post-09:25

        # Load ANY open positions from DB (could be from prior days)
        for pos in self._db.get_open_positions():
            side_guess = "CE" if "CE" in pos.symbol else "PE"
            self._open_positions[side_guess] = pos
            lot_size = self._broker.get_lot_size(pos.symbol)
            self._lot_sizes[pos.symbol] = lot_size
            logger.info("Overnight position loaded: %s %s qty=%d SL=%.2f",
                        side_guess, pos.symbol, pos.qty, pos.sl_price)
            self._alerts.overnight_position(side_guess, pos.symbol, pos.qty, pos.sl_price, pos.expiry)

        return True

    def _check_expiry_integrity(self) -> None:
        """Assert exit date <= expiry date for all open positions."""
        for pos in self._db.get_open_positions():
            if pos.expiry and str(self._today) > pos.expiry:
                self._alerts.expiry_breach(pos.symbol, str(self._today), pos.expiry)

    # ------------------------------------------------------------------
    # 09:15 — Start overnight SL monitoring
    # ------------------------------------------------------------------

    def on_tick_overnight(self, now: datetime) -> None:
        """
        09:15–09:25: Monitor overnight positions from the opening tick.
        Check SL on every tick.  Record gap info (prior close vs first tick).
        """
        for side in SIDES:
            pos = self._open_positions.get(side)
            if pos is None or pos.status != "OPEN":
                continue
            quote = self._broker.get_quote(pos.symbol)
            if quote is None:
                continue
            ltp = quote.ltp
            # Record tick data
            self._journal.write_tick(
                self._today_str, pos.symbol, ltp, quote.bid, quote.ask,
                event="overnight_monitor",
            )
            # SL check
            if is_sl_breached(ltp, pos.sl_price):
                logger.warning("OVERNIGHT SL HIT: %s LTP=%.2f SL=%.2f", pos.symbol, ltp, pos.sl_price)
                self._alerts.sl_hit(side, pos.symbol, ltp, pos.sl_price)
                self._exit_position(pos, side, "sl_overnight", quote.bid, now)

    # ------------------------------------------------------------------
    # 09:20 — Snapshot and selection
    # ------------------------------------------------------------------

    def on_selection_time(self, now: datetime) -> None:
        """
        09:20: Take option chain snapshot, select CE and PE strikes, set triggers.
        """
        logger.info("09:20 selection snapshot (expiry=%s)", self._expiry)
        t0 = time.perf_counter()
        chain = self._broker.get_option_chain_snapshot(self._expiry, num_strikes=20)
        lat = (time.perf_counter() - t0) * 1000

        self._journal.write_chain_snapshot(self._today_str, chain, now.isoformat())

        for side in SIDES:
            daily = self._db.get_or_create_daily_state(self._today_str, side)

            if daily.status not in ("PENDING",):
                # Already selected from earlier today (e.g., late restart)
                if daily.status == "MISSED_REFERENCE":
                    logger.info("Side %s: missed_reference — will not enter today", side)
                self._restore_selection(daily, side)
                continue

            result = select_strike(chain, side, self._cfg, now, lat)

            if result is None:
                daily.status = "NO_TRADE"
                daily.no_trade_reason = "no_eligible_strike"
                self._db.update_daily_state(daily)
                self._alerts.no_trade(side, "no eligible strike in LTP band")
                self._journal.write_daily({
                    "date": self._today_str, "side": side,
                    "expiry": self._expiry, "status": "NO_TRADE",
                    "no_trade_reason": "no_eligible_strike",
                })
                continue

            # Save selection
            daily.expiry = self._expiry
            daily.strike = result.strike
            daily.symbol = result.symbol
            daily.p0 = result.p0
            daily.trigger = result.trigger
            daily.reference_ts = result.reference_ts
            daily.reference_latency_ms = result.reference_latency_ms
            daily.status = "SELECTED"
            self._db.update_daily_state(daily)
            self._selections[side] = result
            self._reference_established[side] = True

            # Get lot size
            lot_size = self._broker.get_lot_size(result.symbol)
            self._lot_sizes[result.symbol] = lot_size

            self._alerts.selected(
                side, result.strike, result.symbol,
                result.p0, result.trigger, self._expiry
            )
            logger.info(
                "SELECTED %s: strike=%d symbol=%s P0=%.2f trigger=%.2f lot_size=%d",
                side, result.strike, result.symbol, result.p0, result.trigger, lot_size
            )

    def _restore_selection(self, daily: DailyStateRecord, side: str) -> None:
        """Restore selection from DB after a restart."""
        if daily.status in ("SELECTED", "TRIGGERED", "ENTERED") and daily.symbol:
            self._selections[side] = SelectionResult(
                strike=daily.strike or 0,
                symbol=daily.symbol,
                p0=daily.p0 or 0,
                trigger=daily.trigger or 0,
                reference_ts=daily.reference_ts or "",
                reference_latency_ms=daily.reference_latency_ms or 0,
            )
            self._reference_established[side] = True
            logger.info("Restored selection for %s: %s trigger=%.2f",
                        side, daily.symbol, daily.trigger or 0)

    # ------------------------------------------------------------------
    # 09:25 — Mandatory time exit
    # ------------------------------------------------------------------

    def on_exit_time(self, now: datetime) -> None:
        """09:25: exit any overnight position that hasn't been stopped yet."""
        for side in SIDES:
            pos = self._open_positions.get(side)
            if pos is None or pos.status != "OPEN":
                continue
            # Only exit if position date != today (it's a carried-over position)
            if pos.date == self._today_str:
                continue  # today's position, not yet time exit
            logger.info("TIME EXIT (09:25): %s %s", side, pos.symbol)
            quote = self._broker.get_quote(pos.symbol)
            bid = quote.bid if quote else 0
            self._exit_position(pos, side, "time_exit_0925", bid, now)

    # ------------------------------------------------------------------
    # Intraday polling loop (09:20 → 15:25)
    # ------------------------------------------------------------------

    def on_tick_intraday(self, now: datetime) -> None:
        """
        Called ~1 Hz from 09:20 onwards.
        Monitors selected options for fresh trigger cross and SL on open positions.
        """
        h_mm, m = now.hour, now.minute
        ts_str = now.isoformat()
        date_str = self._today_str

        for side in SIDES:
            sel = self._selections.get(side)
            if sel is None:
                continue
            if self._entry_done[side]:
                # Monitor SL on open position
                pos = self._open_positions.get(side)
                if pos and pos.status == "OPEN":
                    quote = self._broker.get_quote(pos.symbol)
                    if quote is None:
                        continue
                    ltp = quote.ltp
                    self._journal.write_tick(
                        date_str, pos.symbol, ltp, quote.bid, quote.ask,
                        event="sl_monitor",
                    )
                    if is_sl_breached(ltp, pos.sl_price):
                        self._alerts.sl_hit(side, pos.symbol, ltp, pos.sl_price)
                        self._exit_position(pos, side, "sl_intraday", quote.bid, now)
                continue

            if not self._reference_established[side]:
                continue

            # Entry cutoff check
            cutoff = _parse_time(self._cfg.get("entry_cutoff_time", "15:25:00"))
            if (now.hour, now.minute, now.second) >= cutoff:
                continue

            # Poll LTP/quote
            quote = self._broker.get_quote(sel.symbol)
            if quote is None:
                continue
            ltp = quote.ltp

            # Data freshness check
            max_age = float(self._cfg.get("max_quote_age_sec", 10))
            if quote.timestamp:
                age = (now - quote.timestamp).total_seconds()
                if age > max_age:
                    logger.warning("Stale quote for %s (age=%.1fs)", sel.symbol, age)
                    continue

            self._journal.write_tick(
                date_str, sel.symbol, ltp, quote.bid, quote.ask,
                event="cross_monitor",
            )

            # Fresh cross detection
            should_enter, event_type = is_fresh_cross(
                prev_ltp=self._prev_ltp[side],
                curr_ltp=ltp,
                trigger=sel.trigger,
                is_first_observation=(self._prev_ltp[side] is None),
                gap_cross_policy=str(self._cfg.get("gap_cross_policy", "enter")),
            )
            self._prev_ltp[side] = ltp

            if event_type in ("gap_cross", "gap_cross_skipped"):
                self._alerts.gap_cross(
                    side, sel.symbol, ltp, sel.trigger,
                    self._cfg.get("gap_cross_policy", "enter"),
                )
                self._journal.write_tick(
                    date_str, sel.symbol, ltp, quote.bid, quote.ask,
                    event=event_type,
                )

            if not should_enter:
                continue

            # Risk checks before entry
            if not self._risk.may_enter():
                continue
            if not self._risk.check_max_orders():
                continue
            if not self._risk.check_daily_loss():
                continue
            if not self._risk.check_funds(self._broker, sel.symbol,
                                           self._lot_sizes.get(sel.symbol, 75) * int(self._cfg.get("lots", 1)),
                                           ltp):
                continue
            if not self._risk.check_premium_sanity(ltp):
                continue
            if not self._risk.check_disk_space():
                continue

            # Late entry flag
            is_late = (now.hour, now.minute) >= (15, 0)
            if is_late:
                logger.warning("Late entry flagged for %s at %s", side, now.strftime("%H:%M:%S"))

            # IP re-check before live order
            if self._mode == "live":
                if not self._ip_guard.check(mode="live"):
                    self._alerts.halt("IP mismatch before order")
                    self._risk.stop_entries("ip_mismatch")
                    continue

            # Execute entry
            lot_size = self._lot_sizes.get(sel.symbol, 75)
            lots = int(self._cfg.get("lots", 1))
            qty = lot_size * lots

            logger.info(
                "TRIGGER CROSS %s: LTP=%.2f >= trigger=%.2f — entering",
                side, ltp, sel.trigger,
            )
            self._journal.write_tick(
                date_str, sel.symbol, ltp, quote.bid, quote.ask,
                event=f"trigger_cross_{event_type}",
            )

            daily = self._db.get_or_create_daily_state(date_str, side)
            daily.status = "TRIGGERED"
            self._db.update_daily_state(daily)

            t_cross = now.isoformat()
            result = execute_entry(
                broker=self._broker,
                db=self._db,
                symbol=sel.symbol,
                qty=qty,
                ask=quote.ask,
                trigger=sel.trigger,
                config=self._cfg,
                side=side,
                date_str=date_str,
                alerts=self._alerts,
            )

            self._risk.record_order_sent()

            if result.event == "cap_blocked":
                self._alerts.entry_skipped(side, "slippage_cap_blocked")
                daily.status = "NO_TRADE"
                daily.no_trade_reason = "slippage_cap_blocked"
                self._db.update_daily_state(daily)
                self._entry_done[side] = True
                continue

            if not result.success or result.filled_qty == 0:
                self._alerts.entry_skipped(side, result.event)
                self._risk.record_rejection()
                daily.status = "NO_TRADE"
                daily.no_trade_reason = result.event
                self._db.update_daily_state(daily)
                self._entry_done[side] = True
                continue

            self._risk.record_fill()
            avg_fill = result.avg_fill_price
            sl_price = compute_sl(avg_fill, self._cfg)
            actual_qty = result.filled_qty

            # Save position to DB
            pos_rec = PositionRecord(
                date=date_str, side=side, symbol=sel.symbol,
                expiry=self._expiry, strike=sel.strike, qty=actual_qty,
                avg_fill_price=avg_fill, sl_price=sl_price,
                entry_order_id=result.order_id,
                entry_time=_now_ist().isoformat(),
                status="OPEN",
                extra={
                    "p0": sel.p0, "trigger": sel.trigger,
                    "event_type": event_type,
                    "flag_gap_cross": event_type == "gap_cross",
                    "flag_partial_fill": result.event == "partial",
                    "flag_late_entry": is_late,
                    "bid_at_cross": quote.bid,
                    "ask_at_cross": quote.ask,
                    "cross_time": t_cross,
                    "lot_size": lot_size,
                    "lots": lots,
                },
            )
            pos_id = self._db.upsert_position(pos_rec)
            pos_rec.id = pos_id
            self._open_positions[side] = pos_rec
            self._entry_done[side] = True

            daily.status = "ENTERED"
            self._db.update_daily_state(daily)

            self._alerts.entry(side, sel.symbol, actual_qty, avg_fill, sl_price)
            logger.info(
                "ENTERED %s: symbol=%s qty=%d avg_fill=%.2f SL=%.2f",
                side, sel.symbol, actual_qty, avg_fill, sl_price,
            )

    # ------------------------------------------------------------------
    # 15:35 — End of day
    # ------------------------------------------------------------------

    def on_eod(self, now: datetime) -> None:
        """
        15:35: Persist state, write daily journal rows, send EOD summary.
        """
        total_pnl = 0.0
        closed_count = 0

        for side in SIDES:
            pos = self._open_positions.get(side)
            daily = self._db.get_or_create_daily_state(self._today_str, side)

            if pos and pos.status == "CLOSED":
                closed_count += 1
                entry_pnl = (pos.exit_fill_price or 0) - pos.avg_fill_price
                lot_size = self._lot_sizes.get(pos.symbol, 75)
                pnl_pts = round(entry_pnl, 2)
                pnl_inr = round(entry_pnl * pos.qty, 2)
                total_pnl += pnl_inr
                self._risk.record_pnl(pnl_inr)

                self._journal.write_daily({
                    "date": self._today_str, "side": side,
                    "expiry": pos.expiry, "status": daily.status,
                    "strike": pos.strike, "symbol": pos.symbol,
                    "p0": daily.p0, "trigger": daily.trigger,
                    "pnl_points": pnl_pts, "pnl_inr": pnl_inr,
                })
            elif daily.status in ("NO_TRADE", "MISSED_REFERENCE", "PENDING"):
                self._journal.write_daily({
                    "date": self._today_str, "side": side,
                    "expiry": self._expiry, "status": daily.status,
                    "no_trade_reason": daily.no_trade_reason,
                    "pnl_points": 0, "pnl_inr": 0,
                })

        self._alerts.eod_summary(self._today_str, closed_count, total_pnl)
        self._alerts.heartbeat()   # EOD heartbeat
        logger.info("EOD: closed=%d total_pnl=₹%.0f", closed_count, total_pnl)

    # ------------------------------------------------------------------
    # Internal: exit a position
    # ------------------------------------------------------------------

    def _exit_position(
        self,
        pos: PositionRecord,
        side: str,
        reason: str,
        bid: float,
        now: datetime,
    ) -> None:
        """Place exit order, record fill, update DB and journal."""
        if pos.status != "OPEN":
            return

        order_id = execute_exit(
            broker=self._broker,
            db=self._db,
            pos=pos,
            reason=reason,
            config=self._cfg,
            date_str=self._today_str,
            bid=bid,
        )
        if order_id is None:
            self._alerts.exit_failed(side, pos.symbol)
            return

        # Get fill price
        fill = self._broker.get_order_status(order_id)
        exit_price = fill.avg_price if fill.avg_price > 0 else bid

        lot_size = self._lot_sizes.get(pos.symbol, 75)
        pnl_pts, pnl_inr = compute_pnl(
            pos.avg_fill_price, exit_price, pos.qty, lot_size,
            int(self._cfg.get("lots", 1))
        )

        pos.status = "CLOSED"
        pos.exit_reason = reason
        pos.exit_order_id = order_id
        pos.exit_fill_price = exit_price
        pos.closed_at = now.isoformat()
        self._db.upsert_position(pos)

        self._alerts.exit_done(side, pos.symbol, reason, exit_price, pnl_pts, pnl_inr)

        extra = pos.extra or {}
        self._journal.write_trade({
            "date": pos.date,
            "side": side,
            "expiry": pos.expiry,
            "dte": dte(pos.expiry),
            "strike": pos.strike,
            "symbol": pos.symbol,
            "p0": extra.get("p0"),
            "trigger": extra.get("trigger"),
            "reference_ts": None,
            "cross_detected_time": extra.get("cross_time"),
            "bid_at_cross": extra.get("bid_at_cross"),
            "ask_at_cross": extra.get("ask_at_cross"),
            "fill_time": pos.entry_time,
            "avg_fill_price": pos.avg_fill_price,
            "entry_slippage_vs_ask": round(pos.avg_fill_price - (extra.get("ask_at_cross") or pos.avg_fill_price), 2),
            "entry_slippage_vs_trigger": round(pos.avg_fill_price - (extra.get("trigger") or pos.avg_fill_price), 2),
            "sl_price": pos.sl_price,
            "exit_reason": reason,
            "exit_fill_time": now.isoformat(),
            "exit_fill_price": exit_price,
            "exit_slippage_vs_bid": round(bid - exit_price, 2),
            "gap_through_stop": reason in ("sl_overnight",),
            "pnl_points": pnl_pts,
            "pnl_inr": pnl_inr,
            "lots": extra.get("lots"),
            "lot_size": lot_size,
            "qty": pos.qty,
            "flag_gap_cross": extra.get("flag_gap_cross", False),
            "flag_partial_fill": extra.get("flag_partial_fill", False),
            "flag_late_entry": extra.get("flag_late_entry", False),
            "flag_retries": extra.get("retries", 0),
            "mode": self._mode,
        })
        logger.info("Position closed: %s %s reason=%s pnl_pts=%.2f",
                    side, pos.symbol, reason, pnl_pts)

    # ------------------------------------------------------------------
    # Flatten (botctl flatten)
    # ------------------------------------------------------------------

    def flatten_all(self, now: datetime) -> None:
        """
        Square off all open positions.  Called by botctl flatten with user confirmation.
        """
        logger.warning("FLATTEN: squaring off all open positions")
        for side in SIDES:
            pos = self._open_positions.get(side)
            if pos and pos.status == "OPEN":
                quote = self._broker.get_quote(pos.symbol)
                bid = quote.bid if quote else 0
                self._exit_position(pos, side, "flatten", bid, now)

    # ------------------------------------------------------------------
    # Status snapshot (for botctl status)
    # ------------------------------------------------------------------

    def status_snapshot(self) -> dict:
        snap = {
            "mode": self._mode,
            "today": self._today_str,
            "expiry": self._expiry,
            "risk": self._risk.status_dict(),
        }
        for side in SIDES:
            sel = self._selections.get(side)
            pos = self._open_positions.get(side)
            daily = self._db.get_or_create_daily_state(self._today_str, side)
            snap[side] = {
                "status": daily.status,
                "strike": sel.strike if sel else None,
                "symbol": sel.symbol if sel else None,
                "trigger": sel.trigger if sel else None,
                "p0": sel.p0 if sel else None,
                "position": {
                    "qty": pos.qty, "avg_fill": pos.avg_fill_price,
                    "sl": pos.sl_price, "expiry": pos.expiry,
                } if pos and pos.status == "OPEN" else None,
            }
        return snap


# ---------------------------------------------------------------------------
# Main loop
# ---------------------------------------------------------------------------

def run_bot(
    broker: BrokerPort,
    config: dict,
    state_dir: Path,
    data_dir: Path,
    mode: str,
    alerts: Alerts,
    stop_event=None,   # threading.Event for graceful shutdown
) -> None:
    """
    The always-on main loop.  Runs the lifecycle scheduler.
    The service keeps running overnight; it does not exit at EOD.
    """
    db_path = state_dir / "bot_state.sqlite"
    db = StateDB(db_path)
    risk_state = RiskState()
    risk = RiskGuard(config, risk_state, alerts=alerts, data_dir=data_dir)
    ip_guard = IPGuard(os.environ.get("EXPECTED_STATIC_IP", ""), alerts=alerts)
    clock_guard = ClockGuard(
        max_drift_sec=float(config.get("max_clock_drift_sec", 2.0)),
        alerts=alerts,
    )
    code_version = _get_code_version()
    cfg_hash = _config_hash(config)
    journal = Journal(data_dir, code_version, cfg_hash, mode)

    heartbeat_min = float(config.get("heartbeat_interval_min", 5))
    alerts.start_heartbeat_thread(heartbeat_min)

    # Parse schedule times
    exit_mon_h, exit_mon_m, _ = _parse_time(config.get("exit_monitor_start", "09:15:00"))
    sel_h, sel_m, _ = _parse_time(config.get("selection_time", "09:20:00"))
    exit_h, exit_m, _ = _parse_time(config.get("exit_time", "09:25:00"))
    cutoff_h, cutoff_m, _ = _parse_time(config.get("entry_cutoff_time", "15:25:00"))
    eod_h, eod_m, _ = _parse_time(config.get("eod_summary_time", "15:40:00"))

    # Phase flags for the current day
    morning_done = False
    selection_done = False
    exit_time_done = False
    eod_done = False
    last_date = None

    day = None

    logger.info("Bot main loop starting. mode=%s", mode)

    import signal as _signal
    def _shutdown(signum, frame):
        logger.info("Received signal %d — stopping", signum)
        if stop_event:
            stop_event.set()
    _signal.signal(_signal.SIGTERM, _shutdown)
    _signal.signal(_signal.SIGINT, _shutdown)

    while stop_event is None or not stop_event.is_set():
        now = _now_ist()
        today = now.date()
        h, m, s = now.hour, now.minute, now.second

        # New day reset
        if today != last_date:
            last_date = today
            morning_done = False
            selection_done = False
            exit_time_done = False
            eod_done = False
            risk_state.orders_today = 0
            risk_state.consecutive_rejections = 0
            risk_state.entries_halted = False
            risk_state.halt_reason = None
            risk_state.daily_loss_inr = 0.0
            risk_state.stop_entries_flag = False

            day = DayLifecycle(
                broker=broker, db=db, config=config,
                alerts=alerts, risk=risk, journal=journal,
                ip_guard=ip_guard, clock_guard=clock_guard, mode=mode,
            )
            logger.info("New day: %s", today)

        if day is None:
            time.sleep(1)
            continue

        # --- 08:45 morning init ---
        if not morning_done and (h, m) >= (8, 45):
            if is_trading_day(today):
                morning_done = day.morning_init()
                if not morning_done:
                    logger.warning("Morning init failed — retrying in 60s")
                    time.sleep(60)
                    morning_done = day.morning_init()
            else:
                morning_done = True  # skip non-trading day

        # --- 09:15 overnight SL monitor ---
        if morning_done and (h, m) >= (exit_mon_h, exit_mon_m) and (h, m) < (exit_h, exit_m):
            day.on_tick_overnight(now)

        # --- 09:20 snapshot & selection ---
        if morning_done and not selection_done and (h, m) >= (sel_h, sel_m):
            day.on_selection_time(now)
            selection_done = True

        # --- 09:25 time exit ---
        if morning_done and not exit_time_done and (h, m) >= (exit_h, exit_m):
            day.on_exit_time(now)
            exit_time_done = True

        # --- Intraday loop (09:20 → 15:25) ---
        if morning_done and selection_done and (h, m) >= (sel_h, sel_m) and (h, m) < (cutoff_h, cutoff_m + 1):
            day.on_tick_intraday(now)

        # --- EOD ---
        if morning_done and not eod_done and (h, m) >= (eod_h, eod_m):
            day.on_eod(now)
            eod_done = True

        time.sleep(1)   # ~1 Hz polling

    journal.close()
    logger.info("Bot main loop exited gracefully")
