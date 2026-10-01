"""
strategy.py — Core strategy state machine.

This module contains the complete NIFTY 50 Weekly Option Buying Strategy v1
logic.  It is intentionally free of I/O, scheduling, and Dhan SDK imports.
All broker calls go through BrokerPort.  All state mutations go through StateDB.

State machine per side (CE / PE), per day:
  PENDING → SELECTED (09:20 snapshot done)
          → TRIGGERED (fresh upward cross detected)
          → ENTERED (order filled)
          → CLOSED (SL hit / time exit / flatten)
  Any → NO_TRADE (no eligible strike, cap blocked, etc.)
  Any → MISSED_REFERENCE (bot started late, no 09:20 reference)

The strategy is STATELESS within a tick — all decisions are deterministic
given the DailyStateRecord and PositionRecord from the DB.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import date, datetime, time as dtime
from decimal import ROUND_DOWN, ROUND_HALF_UP, Decimal
from typing import Optional

import pytz

from .broker_port import BrokerPort, ChainRow, Fill, OrderStatus
from .state_db import DailyStateRecord, OrderRecord, PositionRecord, StateDB

logger = logging.getLogger(__name__)
IST = pytz.timezone("Asia/Kolkata")

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _round_tick(value: float, tick: float, rounding=ROUND_HALF_UP) -> float:
    """Round value to nearest tick using specified rounding mode."""
    d = Decimal(str(value))
    t = Decimal(str(tick))
    return float((d / t).to_integral_value(rounding=rounding) * t)


def _round_tick_down(value: float, tick: float) -> float:
    """Round DOWN to tick (used for SL price: conservative — makes SL harder to hit)."""
    return _round_tick(value, tick, rounding=ROUND_DOWN)


def _now_ist() -> datetime:
    return datetime.now(IST)


def _ist_time_str(dt: datetime) -> str:
    return dt.isoformat()


# ---------------------------------------------------------------------------
# Selection
# ---------------------------------------------------------------------------

@dataclass
class SelectionResult:
    strike: int
    symbol: str
    p0: float                       # 09:20 LTP (reference price)
    trigger: float                  # p0 * 1.50 rounded to tick
    reference_ts: str               # Actual timestamp of snapshot
    reference_latency_ms: float
    reason: str = "ok"              # "ok" | "no_eligible" | "no_data"


def select_strike(
    chain_rows: list[ChainRow],
    option_type: str,                # "CE" | "PE"
    config: dict,
    snapshot_ts: datetime,
    snapshot_latency_ms: float,
) -> Optional[SelectionResult]:
    """
    From the 09:20 option chain snapshot, select one CE (or PE) strike.

    Eligibility: ltp_band_low <= LTP <= ltp_band_high (inclusive).
    Selection: minimum |LTP - target_mid|.
    Tie-break: "lower_strike" or "higher_premium" (from config).
    Trigger: P0 * trigger_multiplier, rounded to tick (ROUND_HALF_UP).
    """
    band_low = float(config["ltp_band_low"])
    band_high = float(config["ltp_band_high"])
    target_mid = float(config["target_mid"])
    trigger_mult = float(config["trigger_multiplier"])
    tick = float(config["tick_size"])
    tie_break = str(config.get("tie_break", "lower_strike"))

    candidates = [
        r for r in chain_rows
        if r.option_type == option_type
        and r.ltp is not None
        and band_low <= r.ltp <= band_high
    ]

    if not candidates:
        logger.info(
            "select_strike(%s): no eligible candidates in band [%.2f, %.2f]",
            option_type, band_low, band_high,
        )
        return None

    # Sort by |ltp - target_mid|, then by tie-break
    def sort_key(r: ChainRow):
        dist = abs(r.ltp - target_mid)
        if tie_break == "lower_strike":
            return (dist, r.strike)
        else:  # higher_premium
            return (dist, -r.ltp)

    candidates.sort(key=sort_key)
    chosen = candidates[0]
    p0 = chosen.ltp
    trigger = _round_tick(p0 * trigger_mult, tick)

    logger.info(
        "select_strike(%s): chosen strike=%d symbol=%s P0=%.2f trigger=%.2f "
        "(band=[%.2f,%.2f] candidates=%d)",
        option_type, chosen.strike, chosen.symbol, p0, trigger,
        band_low, band_high, len(candidates),
    )
    return SelectionResult(
        strike=chosen.strike,
        symbol=chosen.symbol,
        p0=p0,
        trigger=trigger,
        reference_ts=_ist_time_str(snapshot_ts),
        reference_latency_ms=snapshot_latency_ms,
    )


# ---------------------------------------------------------------------------
# Cross detection
# ---------------------------------------------------------------------------

def is_fresh_cross(
    prev_ltp: Optional[float],
    curr_ltp: float,
    trigger: float,
    is_first_observation: bool,
    gap_cross_policy: str,
) -> tuple[bool, str]:
    """
    Detect a FRESH upward cross of the trigger level.

    Returns (should_enter, event_type) where event_type is:
      "fresh_cross" | "gap_cross" | "already_above" | "below"

    A fresh cross: prev_ltp < trigger <= curr_ltp.
    First observation at or above trigger: "gap_cross" — handled per config.
    Already above on subsequent observations: "already_above" — NOT an entry.
    """
    if prev_ltp is None:
        # First observation after reference
        if curr_ltp >= trigger:
            if gap_cross_policy == "enter":
                return True, "gap_cross"
            else:
                return False, "gap_cross_skipped"
        return False, "below"

    if prev_ltp < trigger <= curr_ltp:
        return True, "fresh_cross"

    if curr_ltp >= trigger:
        return False, "already_above"

    return False, "below"


# ---------------------------------------------------------------------------
# Entry execution
# ---------------------------------------------------------------------------

def compute_entry_price(
    ask: float,
    config: dict,
) -> float:
    """Entry LIMIT price = ask + entry_limit_buffer, rounded to tick."""
    buf = float(config.get("entry_limit_buffer", 0.50))
    tick = float(config["tick_size"])
    return _round_tick(ask + buf, tick)


def entry_exceeds_slippage_cap(
    entry_limit_price: float,
    trigger: float,
    config: dict,
) -> bool:
    """Return True if the entry price exceeds max_entry_slippage_pct over trigger."""
    cap_pct = float(config.get("max_entry_slippage_pct", 5.0))
    cap_price = trigger * (1 + cap_pct / 100.0)
    return entry_limit_price > cap_price


# ---------------------------------------------------------------------------
# SL calculation
# ---------------------------------------------------------------------------

def compute_sl(avg_fill_price: float, config: dict) -> float:
    """SL = sl_multiplier * avg_fill_price, rounded DOWN to tick."""
    mult = float(config.get("sl_multiplier", 0.50))
    tick = float(config["tick_size"])
    raw_sl = avg_fill_price * mult
    sl = _round_tick_down(raw_sl, tick)
    logger.debug("compute_sl: avg_fill=%.2f * %.2f = %.4f → %.2f (rounded down)", 
                 avg_fill_price, mult, raw_sl, sl)
    return sl


# ---------------------------------------------------------------------------
# SL monitoring
# ---------------------------------------------------------------------------

def is_sl_breached(ltp: float, sl_price: float) -> bool:
    """Return True if LTP has breached (is at or below) the SL price."""
    return ltp <= sl_price


# ---------------------------------------------------------------------------
# Exit order
# ---------------------------------------------------------------------------

def compute_exit_price(bid: float, config: dict, extra_buffer: float = 0.0) -> float:
    """Exit LIMIT price = bid - exit_limit_buffer - extra_buffer, rounded to tick."""
    buf = float(config.get("exit_limit_buffer", 0.50))
    tick = float(config["tick_size"])
    return _round_tick(max(bid - buf - extra_buffer, tick), tick)


# ---------------------------------------------------------------------------
# Order execution helper (used by lifecycle.py)
# ---------------------------------------------------------------------------

@dataclass
class EntryResult:
    success: bool
    order_id: Optional[str]
    fill: Optional[Fill]
    avg_fill_price: float
    filled_qty: int
    event: str           # "filled" | "partial" | "timeout_cancelled" | "rejected" | "cap_blocked"


def execute_entry(
    broker: BrokerPort,
    db: StateDB,
    symbol: str,
    qty: int,
    ask: float,
    trigger: float,
    config: dict,
    side: str,
    date_str: str,
    alerts,   # Alerts instance (imported at call site)
) -> EntryResult:
    """
    Place a marketable LIMIT BUY.  Handle fill timeout, one retry, partial fills.
    Never swallows exceptions from order logic.
    """
    import time

    entry_price = compute_entry_price(ask, config)

    # Cap check
    if entry_exceeds_slippage_cap(entry_price, trigger, config):
        logger.warning(
            "Entry cap blocked: entry_price=%.2f > cap (trigger=%.2f, cap_pct=%.1f%%)",
            entry_price, trigger, config.get("max_entry_slippage_pct", 5.0)
        )
        return EntryResult(
            success=False, order_id=None, fill=None,
            avg_fill_price=0.0, filled_qty=0, event="cap_blocked"
        )

    timeout = float(config.get("entry_fill_timeout_sec", 5))
    tag = f"ENTRY_{side}"
    retries = 1 if config.get("entry_retry", True) else 0

    for attempt in range(retries + 1):
        t0 = time.perf_counter()
        order_id = broker.place_limit_buy(symbol, entry_price, qty, tag=tag)
        lat = (time.perf_counter() - t0) * 1000

        if order_id is None:
            logger.error("place_limit_buy returned None (attempt %d)", attempt + 1)
            continue

        # Record order in DB immediately (idempotency)
        db.insert_order(OrderRecord(
            order_id=order_id, date=date_str, side=side, symbol=symbol,
            direction="BUY", qty=qty, submitted_price=entry_price,
            status="PENDING", tag=tag,
            sent_at=_ist_time_str(_now_ist()), latency_ms=lat,
        ))

        # Poll for fill
        deadline = time.time() + timeout
        fill = None
        while time.time() < deadline:
            time.sleep(0.5)
            fill = broker.get_order_status(order_id)
            if fill.status in (OrderStatus.FILLED, OrderStatus.PARTIALLY_FILLED,
                               OrderStatus.CANCELLED, OrderStatus.REJECTED):
                break

        if fill is None:
            fill = broker.get_order_status(order_id)

        # Update DB
        rec = OrderRecord(
            order_id=order_id, date=date_str, side=side, symbol=symbol,
            direction="BUY", qty=qty, submitted_price=entry_price,
            fill_price=fill.avg_price if fill else 0.0,
            status=fill.status.value if fill else "UNKNOWN",
            tag=tag, sent_at=_ist_time_str(_now_ist()),
            filled_at=_ist_time_str(_now_ist()), latency_ms=lat,
        )
        db.update_order(rec)

        if fill and fill.status == OrderStatus.FILLED:
            return EntryResult(
                success=True, order_id=order_id, fill=fill,
                avg_fill_price=fill.avg_price,
                filled_qty=fill.filled_qty, event="filled",
            )

        if fill and fill.status == OrderStatus.PARTIALLY_FILLED and fill.filled_qty > 0:
            logger.warning("Partial fill: filled=%d of %d", fill.filled_qty, qty)
            return EntryResult(
                success=True, order_id=order_id, fill=fill,
                avg_fill_price=fill.avg_price,
                filled_qty=fill.filled_qty, event="partial",
            )

        # Timeout or rejection — cancel if still pending
        if fill and fill.status not in (OrderStatus.CANCELLED, OrderStatus.REJECTED):
            broker.cancel_order(order_id)
            logger.warning("Entry order %s not filled within %.1fs — cancelled (attempt %d)",
                           order_id, timeout, attempt + 1)

        if fill and fill.status == OrderStatus.REJECTED:
            logger.error("Entry rejected: %s", fill.rejection_reason)
            return EntryResult(
                success=False, order_id=order_id, fill=fill,
                avg_fill_price=0.0, filled_qty=0, event="rejected",
            )

        # Retry with fresh ask
        if attempt < retries:
            new_quote = broker.get_quote(symbol)
            if new_quote:
                ask = new_quote.ask
                entry_price = compute_entry_price(ask, config)
                logger.info("Retrying entry at fresh ask=%.2f entry_price=%.2f", ask, entry_price)
                if entry_exceeds_slippage_cap(entry_price, trigger, config):
                    logger.warning("Retry also cap-blocked — giving up")
                    return EntryResult(
                        success=False, order_id=order_id, fill=fill,
                        avg_fill_price=0.0, filled_qty=0, event="cap_blocked",
                    )

    return EntryResult(
        success=False, order_id=None, fill=None,
        avg_fill_price=0.0, filled_qty=0, event="timeout_cancelled",
    )


def execute_exit(
    broker: BrokerPort,
    db: StateDB,
    pos: PositionRecord,
    reason: str,
    config: dict,
    date_str: str,
    bid: float,
) -> Optional[str]:
    """
    Place a marketable LIMIT SELL.  Escalate with wider bids if not filled.
    NEVER leaves position open — retries until flat, then alerts.
    Returns final order_id or None if all retries exhausted (ALERT!).
    """
    import time

    max_retries = int(config.get("exit_escalation_max_retries", 5))
    timeout = float(config.get("exit_fill_timeout_sec", 5))
    extra_buf = 0.0
    tag = f"EXIT_{pos.side}_{reason[:6]}"
    qty = pos.qty

    for attempt in range(max_retries + 1):
        exit_price = compute_exit_price(bid, config, extra_buffer=extra_buf)
        t0 = time.perf_counter()
        order_id = broker.place_limit_sell(pos.symbol, exit_price, qty, tag=tag)
        lat = (time.perf_counter() - t0) * 1000

        if order_id is None:
            logger.error("place_limit_sell returned None (attempt %d)", attempt + 1)
            extra_buf += float(config.get("exit_escalation_additional_buffer", 0.50))
            continue

        db.insert_order(OrderRecord(
            order_id=order_id, date=date_str, side=pos.side, symbol=pos.symbol,
            direction="SELL", qty=qty, submitted_price=exit_price,
            status="PENDING", tag=tag,
            sent_at=_ist_time_str(_now_ist()), latency_ms=lat,
        ))

        deadline = time.time() + timeout
        fill = None
        while time.time() < deadline:
            time.sleep(0.5)
            fill = broker.get_order_status(order_id)
            if fill.status in (OrderStatus.FILLED, OrderStatus.PARTIALLY_FILLED,
                               OrderStatus.CANCELLED, OrderStatus.REJECTED):
                break

        if fill is None:
            fill = broker.get_order_status(order_id)

        rec = OrderRecord(
            order_id=order_id, date=date_str, side=pos.side, symbol=pos.symbol,
            direction="SELL", qty=qty, submitted_price=exit_price,
            fill_price=fill.avg_price if fill else 0.0,
            status=fill.status.value if fill else "UNKNOWN",
            tag=tag, sent_at=_ist_time_str(_now_ist()),
            filled_at=_ist_time_str(_now_ist()), latency_ms=lat,
        )
        db.update_order(rec)

        if fill and fill.status == OrderStatus.FILLED:
            return order_id

        # Partially filled — exit remaining
        if fill and fill.status == OrderStatus.PARTIALLY_FILLED and fill.filled_qty > 0:
            qty -= fill.filled_qty
            logger.warning("Partial exit fill: %d remaining", qty)
            if qty <= 0:
                return order_id

        # Widen buffer for next attempt
        if fill and fill.status not in (OrderStatus.CANCELLED, OrderStatus.REJECTED):
            broker.cancel_order(order_id)
        extra_buf += float(config.get("exit_escalation_additional_buffer", 0.50))
        bid_q = broker.get_quote(pos.symbol)
        if bid_q:
            bid = bid_q.bid
        logger.warning("Exit order %s not filled — retrying with wider buffer=%.2f (attempt %d)",
                       order_id, extra_buf, attempt + 2)

    logger.critical(
        "FATAL: could not exit position %s after %d attempts! Manual intervention required.",
        pos.symbol, max_retries + 1,
    )
    return None
