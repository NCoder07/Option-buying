"""
alerts.py — Telegram alerts and dead-man-switch heartbeat.

All alert functions are best-effort: they log locally on failure but never
raise exceptions that could halt the trading loop.

Sensitive values (tokens, account IDs) are masked in log output.

Telegram 404 fix: the bot token must be in the format
  <bot_id>:<token_string>
e.g. 7891234567:AAExxxxxxxxxxxxxxxxxxxxxxxx
A 404 means either the token is wrong or the chat_id is wrong.

Repeated failure throttle: after MAX_CONSECUTIVE_FAILURES consecutive HTTP
failures, Telegram sends are suppressed for FAILURE_COOLDOWN_SEC seconds.
This prevents log spam when credentials are misconfigured.
"""

from __future__ import annotations

import logging
import os
import threading
import time
import urllib.parse
from datetime import datetime
from typing import Optional

import requests
import pytz

logger = logging.getLogger(__name__)
IST = pytz.timezone("Asia/Kolkata")

_MASK = lambda s: (s[:4] + "****") if s and len(s) > 4 else "****"

# Throttle: suppress Telegram after this many consecutive failures for this long
_MAX_CONSECUTIVE_FAILURES = 3
_FAILURE_COOLDOWN_SEC = 300   # 5 minutes


class Alerts:
    def __init__(
        self,
        bot_token: Optional[str] = None,
        chat_id: Optional[str] = None,
        heartbeat_url: Optional[str] = None,
        mode: str = "paper",
    ) -> None:
        self._token = bot_token or os.environ.get("TELEGRAM_BOT_TOKEN", "")
        self._chat_id = chat_id or os.environ.get("TELEGRAM_CHAT_ID", "")
        self._heartbeat_url = heartbeat_url or os.environ.get("HEARTBEAT_URL", "")
        self._mode = mode
        self._enabled = bool(self._token and self._chat_id)
        if not self._enabled:
            logger.warning("Telegram alerts disabled (no bot_token/chat_id configured)")
        else:
            # Validate token format: must be "<digits>:<alphanum>"
            if ":" not in self._token or not self._token.split(":")[0].isdigit():
                logger.warning(
                    "Telegram bot token appears malformed (expected '<bot_id>:<token>', got '%s...'). "
                    "This will cause HTTP 404. Fix TELEGRAM_BOT_TOKEN in .env.",
                    self._token[:8] if self._token else "",
                )

        # Failure throttle state
        self._consecutive_failures = 0
        self._suppressed_until: float = 0.0
        self._lock = threading.Lock()

    # ------------------------------------------------------------------
    # Core send
    # ------------------------------------------------------------------

    def send(self, message: str, level: str = "INFO") -> bool:
        """Send a Telegram message.  Returns True on success."""
        # ASCII prefix for console/log (Windows cp1252-safe); emoji only in Telegram body
        prefix = {"INFO": "[INFO]", "WARN": "[WARN]", "ERROR": "[ERROR]", "CRITICAL": "[CRIT]"}.get(level, "[INFO]")
        # Emoji prefix for Telegram only
        tg_prefix = {"INFO": "ℹ️", "WARN": "⚠️", "ERROR": "🚨", "CRITICAL": "🆘"}.get(level, "📌")
        env_tag = f"[{self._mode.upper()}]"
        log_msg = f"ALERT: {prefix} {env_tag} {message}"
        tg_msg  = f"{tg_prefix} {env_tag} {message}"
        logger.info("%s", log_msg)

        if not self._enabled:
            return False

        # Check throttle
        with self._lock:
            now_ts = time.time()
            if now_ts < self._suppressed_until:
                remaining = int(self._suppressed_until - now_ts)
                logger.debug("Telegram suppressed for %ds (consecutive failures)", remaining)
                return False

        try:
            url = (
                f"https://api.telegram.org/bot{self._token}/sendMessage"
                f"?chat_id={self._chat_id}&text={urllib.parse.quote(tg_msg)}"
            )
            resp = requests.get(url, timeout=10)
            if resp.status_code == 200:
                with self._lock:
                    self._consecutive_failures = 0
                return True

            # Log failure with actionable hint
            body = resp.text[:200]
            if resp.status_code == 404:
                hint = (
                    "HTTP 404 — token or chat_id is wrong. "
                    "Check TELEGRAM_BOT_TOKEN (format: <bot_id>:<token>) "
                    "and TELEGRAM_CHAT_ID in .env"
                )
            elif resp.status_code == 400:
                hint = f"HTTP 400 — bad request: {body}"
            elif resp.status_code == 401:
                hint = "HTTP 401 — token is invalid or revoked. Regenerate via BotFather."
            else:
                hint = f"HTTP {resp.status_code}: {body}"

            with self._lock:
                self._consecutive_failures += 1
                if self._consecutive_failures >= _MAX_CONSECUTIVE_FAILURES:
                    self._suppressed_until = time.time() + _FAILURE_COOLDOWN_SEC
                    logger.warning(
                        "Telegram alert %s (failure %d/%d) — suppressing for %ds. %s",
                        hint, self._consecutive_failures, _MAX_CONSECUTIVE_FAILURES,
                        _FAILURE_COOLDOWN_SEC, hint,
                    )
                else:
                    logger.warning(
                        "Telegram alert %s (failure %d/%d)",
                        hint, self._consecutive_failures, _MAX_CONSECUTIVE_FAILURES,
                    )
            return False
        except Exception as e:
            with self._lock:
                self._consecutive_failures += 1
                if self._consecutive_failures >= _MAX_CONSECUTIVE_FAILURES:
                    self._suppressed_until = time.time() + _FAILURE_COOLDOWN_SEC
            logger.warning("Telegram alert failed: %s", e)
            return False

    # ------------------------------------------------------------------
    # Named alert types
    # ------------------------------------------------------------------

    def startup(self, mode: str, lots: int, max_exposure: float, account_id: str, ip: str) -> None:
        self.send(
            f"Bot started | mode={mode} | lots={lots} | max_exposure=₹{max_exposure:,.0f}"
            f" | acct={_MASK(account_id)} | IP={ip}",
            level="INFO",
        )

    def login_ok(self) -> None:
        self.send("Dhan login successful", level="INFO")

    def login_failed(self, reason: str) -> None:
        self.send(f"Dhan LOGIN FAILED: {reason}", level="CRITICAL")

    def ip_mismatch(self, actual: str, expected: str) -> None:
        self.send(
            f"IP GUARD: outbound IP {actual} != expected {expected}. "
            "Live trading REFUSED. Monitor-only mode active.",
            level="CRITICAL",
        )

    def clock_drift(self, drift_sec: float) -> None:
        self.send(f"CLOCK DRIFT {drift_sec:.2f}s exceeds tolerance — new entries halted", level="ERROR")

    def disk_low(self, free_gb: float) -> None:
        self.send(f"DISK LOW: {free_gb:.1f} GB free — new entries halted", level="ERROR")

    def selected(self, side: str, strike: int, symbol: str, p0: float, trigger: float, expiry: str) -> None:
        self.send(
            f"SELECTED {side} | strike={strike} | P0={p0:.2f} | trigger={trigger:.2f}"
            f" | expiry={expiry} | {symbol}",
            level="INFO",
        )

    def no_trade(self, side: str, reason: str) -> None:
        self.send(f"NO TRADE {side}: {reason}", level="INFO")

    def entry(self, side: str, symbol: str, qty: int, fill_price: float, sl: float) -> None:
        self.send(
            f"ENTRY {side} | {symbol} | qty={qty} | fill=₹{fill_price:.2f} | SL=₹{sl:.2f}",
            level="INFO",
        )

    def entry_skipped(self, side: str, reason: str) -> None:
        self.send(f"ENTRY SKIPPED {side}: {reason}", level="WARN")

    def sl_hit(self, side: str, symbol: str, ltp: float, sl: float) -> None:
        self.send(
            f"SL HIT {side} | {symbol} | LTP={ltp:.2f} <= SL={sl:.2f}",
            level="ERROR",
        )

    def exit_done(self, side: str, symbol: str, reason: str, fill: float, pnl_pts: float, pnl_inr: float) -> None:
        emoji = "✅" if pnl_pts >= 0 else "❌"
        self.send(
            f"{emoji} EXIT {side} | {symbol} | reason={reason}"
            f" | fill={fill:.2f} | PnL={pnl_pts:+.2f}pts ₹{pnl_inr:+.0f}",
            level="INFO",
        )

    def exit_failed(self, side: str, symbol: str) -> None:
        self.send(
            f"EXIT FAILED {side} | {symbol} — could not close position! "
            "MANUAL INTERVENTION REQUIRED.",
            level="CRITICAL",
        )

    def circuit_breaker(self, reason: str) -> None:
        self.send(f"CIRCUIT BREAKER: {reason} — new entries halted", level="ERROR")

    def gap_cross(self, side: str, symbol: str, ltp: float, trigger: float, policy: str) -> None:
        self.send(
            f"GAP CROSS {side} | {symbol} | LTP={ltp:.2f} >= trigger={trigger:.2f}"
            f" | policy={policy}",
            level="WARN",
        )

    def eod_summary(self, date_str: str, positions_closed: int, pnl_inr: float) -> None:
        self.send(
            f"EOD {date_str} | {positions_closed} position(s) closed"
            f" | Net PnL ₹{pnl_inr:+.0f}",
            level="INFO",
        )

    def overnight_position(self, side: str, symbol: str, qty: int, sl: float, expiry: str) -> None:
        self.send(
            f"OVERNIGHT pos {side} | {symbol} | qty={qty} | SL={sl:.2f} | expiry={expiry}",
            level="INFO",
        )

    def expiry_breach(self, symbol: str, exit_date: str, expiry_date: str) -> None:
        self.send(
            f"EXPIRY BREACH ALERT: exit_date={exit_date} > expiry={expiry_date}"
            f" for {symbol}! Check immediately.",
            level="CRITICAL",
        )

    def halt(self, reason: str) -> None:
        self.send(f"BOT HALTED: {reason}", level="CRITICAL")

    def resume(self) -> None:
        self.send("Bot RESUMED — entries re-enabled", level="INFO")

    # ------------------------------------------------------------------
    # Dead-man heartbeat
    # ------------------------------------------------------------------

    def heartbeat(self) -> None:
        if not self._heartbeat_url:
            return
        try:
            requests.get(self._heartbeat_url, timeout=5)
            logger.debug("Heartbeat sent")
        except Exception as e:
            logger.warning("Heartbeat failed: %s", e)

    def start_heartbeat_thread(self, interval_minutes: float) -> None:
        """Start a background thread sending heartbeats every N minutes."""
        def _loop():
            while True:
                self.heartbeat()
                time.sleep(interval_minutes * 60)
        t = threading.Thread(target=_loop, daemon=True, name="heartbeat")
        t.start()
        logger.info("Heartbeat thread started (interval=%.1f min)", interval_minutes)
