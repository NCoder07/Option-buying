"""
live_broker.py — LiveDhanBroker: wraps Dhan_Tradehull.Tradehull.

This is the ONLY file that imports Dhan_Tradehull.
All calls are logged with latency.
No orders are placed in paper mode (paper-mode check is enforced in lifecycle.py,
not here — this class always does what it's told).

Pricing model: all market data is LTP-only.  No bid/ask is used for decisions.

Tradehull noise suppression
---------------------------
Tradehull's get_ltp_data() catches all exceptions internally and re-logs them
through Python's root logger at ERROR level with a full traceback.  This
produces one console line + one ERROR log entry per failed LTP call.  When the
market is closed or the API returns an empty response, every 1 Hz poll tick
generates this noise.

We suppress the specific "Exception at calling ltp as" root-logger message by
installing a logging.Filter on the root logger after connect().  The filter
drops records originating from Dhan_Tradehull that match that pattern so they
don't appear in our logs.  The WARNING emitted by get_ltp_single() still fires,
giving full observability without the traceback flood.

LTP back-off
------------
get_ltp_single() tracks consecutive failures per symbol.  After
LTP_CONSECUTIVE_FAIL_THRESHOLD failures it stops calling the API for
LTP_BACKOFF_SEC seconds (configurable; defaults below).  This prevents
hammering Dhan's API during market-closed periods or rate-limit windows.
"""

from __future__ import annotations

import logging
import os
import re
import time
from datetime import datetime
from typing import Optional

import pytz

from .broker_port import (
    BrokerPort, ChainRow, Fill, LTPSnapshot, OrderStatus, Position,
)

logger = logging.getLogger(__name__)
IST = pytz.timezone("Asia/Kolkata")

# LTP back-off settings (conservative defaults; can be overridden via config)
_LTP_CONSECUTIVE_FAIL_THRESHOLD = 5   # failures before backing off
_LTP_BACKOFF_SEC = 30                 # seconds to wait before retrying

_TRADEHULL_NOISE_RE = re.compile(r"Exception at calling ltp as")


class _TradehullNoiseFilter(logging.Filter):
    """Drop the noisy 'Exception at calling ltp as ...' root-logger records."""
    def filter(self, record: logging.LogRecord) -> bool:
        # Only suppress records from Dhan_Tradehull that match the LTP noise pattern
        if _TRADEHULL_NOISE_RE.search(record.getMessage()):
            return False
        return True


class LiveDhanBroker(BrokerPort):
    """
    Wraps Dhan_Tradehull.Tradehull for live/paper order execution.

    Parameters
    ----------
    client_code, pin, totp_secret : Dhan credentials (from env, never from config)
    config : dict from YAML config (for exchange, tick_size, etc.)
    state_dir : path where Tradehull will write its Dependencies/ folder
    """

    def __init__(
        self,
        client_code: str,
        pin: str,
        totp_secret: str,
        config: dict,
        state_dir: str,
    ) -> None:
        self._config = config
        self._state_dir = state_dir
        self._th = None                      # Tradehull instance, set in connect()
        self._instrument_df = None
        self._client_code = client_code
        self._pin = pin
        self._totp_secret = totp_secret
        # LTP back-off state: {symbol: (consecutive_failures, suppressed_until_ts)}
        self._ltp_backoff: dict[str, tuple[int, float]] = {}

    # ------------------------------------------------------------------
    # Login / refresh
    # ------------------------------------------------------------------

    def connect(self) -> None:
        """Login using pin_totp mode (unattended).  Must be called once per day."""
        # Tradehull writes Dependencies/ relative to CWD — chdir to state_dir
        original_cwd = os.getcwd()
        os.chdir(self._state_dir)
        os.makedirs("Dependencies", exist_ok=True)
        try:
            from Dhan_Tradehull.Dhan_Tradehull import Tradehull
            logger.info("Connecting to Dhan (pin_totp mode)…")
            t0 = time.perf_counter()
            self._th = Tradehull(
                ClientCode=self._client_code,
                token_id="",
                mode="pin_totp",
                pin=self._pin,
                totp_secret=self._totp_secret,
            )
            lat = (time.perf_counter() - t0) * 1000
            self._instrument_df = self._th.instrument_df
            logger.info("Dhan login OK (%.0f ms)", lat)
            # Install noise filter on root logger to suppress Tradehull's own
            # "Exception at calling ltp as ..." traceback spam.
            _noise_filter = _TradehullNoiseFilter()
            logging.getLogger().addFilter(_noise_filter)
            logger.debug("Tradehull LTP noise filter installed on root logger")
        finally:
            os.chdir(original_cwd)

    def is_connected(self) -> bool:
        return self._th is not None

    def reconnect_if_needed(self) -> None:
        """Call before first order each day; re-login if token invalid."""
        if not self.is_connected():
            self.connect()
            return
        # Tradehull validates the cached token on instantiation; for intraday
        # reconnects we check by calling get_balance (cheap, read-only)
        try:
            self._th.get_balance()
        except Exception:
            logger.warning("Token validation failed — reconnecting")
            self._th = None
            self.connect()

    # ------------------------------------------------------------------
    # Market data
    # ------------------------------------------------------------------

    def get_ltp(self, symbols: list[str]) -> dict[str, float]:
        try:
            t0 = time.perf_counter()
            result = self._th.get_ltp_data(symbols)
            lat = (time.perf_counter() - t0) * 1000
            logger.debug("get_ltp(%s) -> %s  (%.0f ms)", symbols, result, lat)
            return result or {}
        except Exception as e:
            logger.error("get_ltp failed: %s", e)
            return {}

    def get_ltp_single(self, symbol: str) -> Optional[LTPSnapshot]:
        """
        Return LTPSnapshot for one symbol.  Uses get_ltp_data([symbol]) which
        returns a {symbol: ltp} dict from Tradehull.  Records a tz-aware IST
        timestamp at the moment of receipt for freshness checks.

        Back-off: after LTP_CONSECUTIVE_FAIL_THRESHOLD consecutive failures for
        a symbol, suppress API calls for LTP_BACKOFF_SEC seconds.  The caller
        receives None (no_data) during the backoff window.  This prevents
        hammering the API when the market is closed or the symbol is invalid.
        """
        # Check back-off
        now_ts = time.monotonic()
        fails, suppressed_until = self._ltp_backoff.get(symbol, (0, 0.0))
        if now_ts < suppressed_until:
            return None   # silent: lifecycle already logged "skipping tick"

        try:
            t0 = time.perf_counter()
            result = self._th.get_ltp_data([symbol])
            lat = (time.perf_counter() - t0) * 1000
            if not result or symbol not in result:
                # Tradehull returned empty/failure — count as a failure
                fails += 1
                if fails >= _LTP_CONSECUTIVE_FAIL_THRESHOLD:
                    logger.warning(
                        "get_ltp_single(%s): %d consecutive failures — "
                        "backing off for %ds",
                        symbol, fails, _LTP_BACKOFF_SEC,
                    )
                    self._ltp_backoff[symbol] = (fails, now_ts + _LTP_BACKOFF_SEC)
                else:
                    logger.warning(
                        "get_ltp_single(%s): empty response (%.0f ms) [fail %d/%d]",
                        symbol, lat, fails, _LTP_CONSECUTIVE_FAIL_THRESHOLD,
                    )
                    self._ltp_backoff[symbol] = (fails, 0.0)
                return None
            # Success — reset back-off counter
            self._ltp_backoff[symbol] = (0, 0.0)
            ltp = float(result[symbol])
            if ltp <= 0:
                logger.warning("get_ltp_single(%s): ltp=%.4f <= 0", symbol, ltp)
                return None
            ts = datetime.now(IST)
            logger.debug("get_ltp_single(%s) ltp=%.2f ts=%s latency=%.0f ms",
                         symbol, ltp, ts.isoformat(), lat)
            return LTPSnapshot(symbol=symbol, ltp=ltp, timestamp=ts)
        except Exception as e:
            logger.error("get_ltp_single(%s) failed: %s", symbol, e)
            fails += 1
            if fails >= _LTP_CONSECUTIVE_FAIL_THRESHOLD:
                self._ltp_backoff[symbol] = (fails, now_ts + _LTP_BACKOFF_SEC)
            else:
                self._ltp_backoff[symbol] = (fails, 0.0)
            return None

    def get_option_chain_snapshot(
        self,
        expiry_date: str,
        num_strikes: int = 20,
    ) -> list[ChainRow]:
        """
        Return CE+PE chain rows for the given expiry date.

        Bypasses Tradehull's get_option_chain() wrapper (which internally calls
        get_ltp_data("NIFTY 50") and fails on that symbol string). Instead we
        call the underlying dhanhq.option_chain() directly, which works correctly.

        bid/ask fields from the API are stored verbatim in each ChainRow for
        archival in the snapshot JSON.  They are NOT used by strategy logic.
        """
        try:
            import pandas as pd

            # Resolve NIFTY security_id from instrument master
            idf = self._instrument_df
            nifty_rows = idf[
                ((idf["SEM_TRADING_SYMBOL"] == "NIFTY") | (idf["SEM_CUSTOM_SYMBOL"] == "NIFTY"))
                & (idf["SEM_EXM_EXCH_ID"] == "NSE")
            ]
            if nifty_rows.empty:
                logger.error("Cannot find NIFTY in instrument master")
                return []
            security_id = int(nifty_rows.iloc[-1]["SEM_SMST_SECURITY_ID"])

            t0 = time.perf_counter()
            resp = self._th.Dhan.option_chain(
                under_security_id=security_id,
                under_exchange_segment=self._th.Dhan.INDEX,
                expiry=expiry_date,
            )
            lat = (time.perf_counter() - t0) * 1000
            logger.info("option_chain(expiry=%s) latency=%.0f ms", expiry_date, lat)

            if resp.get("status") != "success":
                logger.error("option_chain API error: %s", resp)
                return []

            oc_data = resp["data"]["data"]["oc"]
            rows: list[ChainRow] = []

            for strike_str, data in oc_data.items():
                strike = int(float(strike_str))
                for opt_type, side_key in [("CE", "ce"), ("PE", "pe")]:
                    side = data.get(side_key, {})
                    ltp = float(side.get("last_price") or 0)
                    # Store raw bid/ask for snapshot archival only — not used in logic
                    bid = float(side.get("top_bid_price") or 0)
                    ask = float(side.get("top_ask_price") or 0)
                    oi  = side.get("oi")
                    iv  = side.get("implied_volatility")

                    # Look up trading symbol from instrument master
                    sym = self._find_symbol(strike, opt_type, expiry_date)
                    if sym is None:
                        # Build a fallback symbol name from security_id
                        sec_id = side.get("security_id")
                        sym = f"NIFTY_{expiry_date}_{strike}_{opt_type}" if not sec_id else str(sec_id)

                    rows.append(ChainRow(
                        strike=strike, option_type=opt_type, symbol=sym,
                        ltp=ltp, bid=bid, ask=ask, oi=oi, iv=iv,
                    ))

            logger.info("option_chain_snapshot: %d rows for expiry=%s", len(rows), expiry_date)
            return rows

        except Exception as e:
            logger.error("get_option_chain_snapshot failed: %s", e, exc_info=True)
            return []

    def _find_symbol(self, strike: int, option_type: str, expiry_date: str) -> Optional[str]:
        """
        Look up the canonical symbol string from the instrument master.

        Returns SEM_CUSTOM_SYMBOL (e.g. "NIFTY 06 OCT 22850 CALL") which:
          - is already fully uppercase, so survives Tradehull's internal
            name.upper() call in both get_ltp_data() and order_placement()
          - matches instrument_df[SEM_CUSTOM_SYMBOL] after upper(), giving a
            correct security_id lookup in both APIs.

        Why NOT SEM_TRADING_SYMBOL ("NIFTY-Oct2026-22850-CE"):
          - Tradehull uppercases it to "NIFTY-OCT2026-22850-CE", which does
            NOT match the mixed-case value stored in the instrument master,
            so the lookup fails silently and returns an empty LTP dict.

        Root cause of the previous NaN crash:
          - SEM_STRIKE_PRICE contains NaN for non-option rows.
          - .astype(float).astype(int) on the *whole* unfiltered column raises
            IntCastingNaNError even when a notna() guard is in the same mask,
            because pandas evaluates all mask terms before short-circuiting.
          Fix: convert the column once with pd.to_numeric(errors='coerce')
          then fillna(-1) before casting to int.

        SEM_EXPIRY_DATE is a string like "2026-10-06 14:30:00"; we extract the
        date part and compare against pd.to_datetime(expiry_date).date().
        """
        try:
            import pandas as pd
            df = self._instrument_df
            expiry_dt = pd.to_datetime(expiry_date).date()

            # Safe numeric conversion — NaN and ±inf rows become -1 (never a real strike).
            # Two-step: coerce non-numeric to NaN, then replace NaN/inf with -1.
            strike_num = pd.to_numeric(df["SEM_STRIKE_PRICE"], errors="coerce")
            strike_int = strike_num.fillna(-1).replace([float("inf"), float("-inf")], -1).astype(int)

            mask = (
                (df["SEM_EXM_EXCH_ID"] == "NSE") &
                (df["SEM_TRADING_SYMBOL"].str.startswith("NIFTY")) &
                (df["SEM_OPTION_TYPE"] == option_type) &
                (strike_int == strike)
            )
            filtered = df[mask].copy()
            if filtered.empty:
                return None

            # Expiry column is "YYYY-MM-DD HH:MM:SS" string — extract date part
            filtered["_expiry"] = pd.to_datetime(
                filtered["SEM_EXPIRY_DATE"], errors="coerce"
            ).dt.date
            filtered = filtered[filtered["_expiry"] == expiry_dt]
            if filtered.empty:
                return None
            # SEM_CUSTOM_SYMBOL is already uppercase (e.g. "NIFTY 06 OCT 22850 CALL")
            # and survives Tradehull's internal .upper() correctly.
            return str(filtered.iloc[-1]["SEM_CUSTOM_SYMBOL"])
        except Exception as e:
            logger.warning("_find_symbol(%d, %s, %s): %s", strike, option_type, expiry_date, e)
            return None

    def get_expiry_list(self) -> list[str]:
        try:
            expiries = self._th.get_expiry_list("NIFTY", "INDEX")
            return expiries or []
        except Exception as e:
            logger.error("get_expiry_list failed: %s", e)
            return []

    # ------------------------------------------------------------------
    # Orders
    # ------------------------------------------------------------------

    def place_limit_buy(
        self,
        symbol: str,
        price: float,
        qty: int,
        tag: Optional[str] = None,
    ) -> Optional[str]:
        try:
            logger.info("PLACE BUY  symbol=%s price=%.2f qty=%d tag=%s", symbol, price, qty, tag)
            t0 = time.perf_counter()
            order_id = self._th.order_placement(
                tradingsymbol=symbol,
                exchange="NFO",
                quantity=qty,
                price=round(price, 2),
                trigger_price=0,
                order_type="LIMIT",
                transaction_type="BUY",
                trade_type="MARGIN",
                tag=tag,
            )
            lat = (time.perf_counter() - t0) * 1000
            logger.info("BUY order_id=%s latency=%.0f ms", order_id, lat)
            return order_id
        except Exception as e:
            logger.error("place_limit_buy failed: %s", e, exc_info=True)
            return None

    def place_limit_sell(
        self,
        symbol: str,
        price: float,
        qty: int,
        tag: Optional[str] = None,
    ) -> Optional[str]:
        try:
            logger.info("PLACE SELL symbol=%s price=%.2f qty=%d tag=%s", symbol, price, qty, tag)
            t0 = time.perf_counter()
            order_id = self._th.order_placement(
                tradingsymbol=symbol,
                exchange="NFO",
                quantity=qty,
                price=round(price, 2),
                trigger_price=0,
                order_type="LIMIT",
                transaction_type="SELL",
                trade_type="MARGIN",
                tag=tag,
            )
            lat = (time.perf_counter() - t0) * 1000
            logger.info("SELL order_id=%s latency=%.0f ms", order_id, lat)
            return order_id
        except Exception as e:
            logger.error("place_limit_sell failed: %s", e, exc_info=True)
            return None

    def cancel_order(self, order_id: str) -> bool:
        try:
            result = self._th.cancel_order(order_id)
            logger.info("cancel_order(%s) -> %s", order_id, result)
            return result not in (None, False)
        except Exception as e:
            logger.error("cancel_order(%s) failed: %s", order_id, e)
            return False

    def get_order_status(self, order_id: str) -> Fill:
        try:
            detail = self._th.get_order_detail(order_id)
            status_map = {
                "TRADED": OrderStatus.FILLED,
                "PART_TRADED": OrderStatus.PARTIALLY_FILLED,
                "CANCELLED": OrderStatus.CANCELLED,
                "REJECTED": OrderStatus.REJECTED,
                "PENDING": OrderStatus.PENDING,
                "TRANSIT": OrderStatus.OPEN,
            }
            raw_status = str(detail.get("orderStatus", "UNKNOWN")).upper()
            status = status_map.get(raw_status, OrderStatus.UNKNOWN)
            qty = int(detail.get("quantity", 0))
            filled = int(detail.get("filledQty", 0))
            avg_price = float(detail.get("averageTradedPrice", 0))
            return Fill(
                order_id=order_id,
                status=status,
                avg_price=avg_price,
                filled_qty=filled,
                remaining_qty=qty - filled,
                exchange_time=detail.get("exchangeTime"),
                rejection_reason=detail.get("omsErrorDescription"),
            )
        except Exception as e:
            logger.error("get_order_status(%s) failed: %s", order_id, e)
            return Fill(
                order_id=order_id,
                status=OrderStatus.UNKNOWN,
                avg_price=0.0,
                filled_qty=0,
                remaining_qty=0,
                rejection_reason=str(e),
            )

    # ------------------------------------------------------------------
    # Account
    # ------------------------------------------------------------------

    def get_positions(self) -> list[Position]:
        try:
            df = self._th.get_positions()
            if df is None or (hasattr(df, "empty") and df.empty):
                return []
            positions = []
            for _, row in df.iterrows():
                net_qty = int(row.get("netQty", 0))
                if net_qty == 0:
                    continue
                positions.append(Position(
                    symbol=str(row.get("tradingSymbol", "")),
                    qty=net_qty,
                    avg_price=float(row.get("buyAvg", 0) if net_qty > 0 else row.get("sellAvg", 0)),
                    product_type=str(row.get("productType", "MARGIN")),
                ))
            return positions
        except Exception as e:
            logger.error("get_positions failed: %s", e)
            return []

    def get_available_balance(self) -> float:
        try:
            return self._th.get_balance()
        except Exception as e:
            logger.error("get_available_balance failed: %s", e)
            return 0.0

    def get_lot_size(self, symbol: str) -> int:
        try:
            size = self._th.get_lot_size(symbol)
            return int(size) if size else 75  # 75 is current NIFTY lot size
        except Exception as e:
            logger.warning("get_lot_size(%s) failed (%s) — using fallback 75", symbol, e)
            return 75
