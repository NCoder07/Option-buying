"""
test_live_broker_find_symbol.py — Unit tests for LiveDhanBroker._find_symbol().

Tests are fully offline (no real Dhan connection) — we inject a fake
instrument_df that mirrors the exact column names and data format observed
in the real instrument master.

Covers:
  - Correct symbol lookup for CE and PE
  - NaN strike rows (non-option rows) must not cause IntCastingNaNError
  - Inf / non-finite strike values must not cause crash
  - No match for wrong expiry date
  - No match for wrong option type
  - No match when strike absent entirely
  - Returns SEM_CUSTOM_SYMBOL (already uppercase) not SEM_TRADING_SYMBOL
"""

from __future__ import annotations

import types
import pytest
import pandas as pd

from bot.live_broker import LiveDhanBroker


# ---------------------------------------------------------------------------
# Helpers to build a fake instrument_df
# ---------------------------------------------------------------------------

def _make_df(rows: list[dict]) -> pd.DataFrame:
    """Build a minimal instrument_df from a list of row dicts."""
    cols = [
        "SEM_EXM_EXCH_ID",
        "SEM_TRADING_SYMBOL",
        "SEM_CUSTOM_SYMBOL",
        "SEM_OPTION_TYPE",
        "SEM_STRIKE_PRICE",
        "SEM_EXPIRY_DATE",
        "SEM_SMST_SECURITY_ID",
    ]
    return pd.DataFrame(rows, columns=cols)


def _option_row(strike, opt_type, expiry_date, custom_sym=None, trading_sym=None, sec_id=999):
    """Return a dict representing one option row in the instrument master."""
    # SEM_EXPIRY_DATE is stored as "YYYY-MM-DD HH:MM:SS" in real data
    expiry_full = f"{expiry_date} 14:30:00"
    sym = custom_sym or f"NIFTY {expiry_date.replace('-', ' ')} {strike} {'CALL' if opt_type=='CE' else 'PUT'}"
    tsym = trading_sym or f"NIFTY-{expiry_date}-{strike}-{opt_type}"
    return {
        "SEM_EXM_EXCH_ID": "NSE",
        "SEM_TRADING_SYMBOL": tsym,
        "SEM_CUSTOM_SYMBOL": sym,
        "SEM_OPTION_TYPE": opt_type,
        "SEM_STRIKE_PRICE": float(strike),
        "SEM_EXPIRY_DATE": expiry_full,
        "SEM_SMST_SECURITY_ID": sec_id,
    }


def _non_option_row():
    """Equity row with NaN strike — must not cause crash."""
    return {
        "SEM_EXM_EXCH_ID": "NSE",
        "SEM_TRADING_SYMBOL": "RELIANCE",
        "SEM_CUSTOM_SYMBOL": "RELIANCE",
        "SEM_OPTION_TYPE": None,          # no option type
        "SEM_STRIKE_PRICE": float("nan"), # NaN strike — this was the crash
        "SEM_EXPIRY_DATE": None,
        "SEM_SMST_SECURITY_ID": 1234,
    }


def _make_broker(df: pd.DataFrame) -> LiveDhanBroker:
    """Create a LiveDhanBroker stub with the given instrument_df injected."""
    b = LiveDhanBroker.__new__(LiveDhanBroker)
    b._instrument_df = df
    b._config = {}
    return b


# ---------------------------------------------------------------------------
# Test cases
# ---------------------------------------------------------------------------

class TestFindSymbol:
    EXPIRY = "2026-10-06"

    @pytest.fixture
    def df(self):
        return _make_df([
            _non_option_row(),             # NaN strike equity row
            _non_option_row(),             # another NaN
            _option_row(22850, "CE", self.EXPIRY, custom_sym="NIFTY 06 OCT 22850 CALL"),
            _option_row(22850, "PE", self.EXPIRY, custom_sym="NIFTY 06 OCT 22850 PUT"),
            _option_row(22800, "CE", self.EXPIRY, custom_sym="NIFTY 06 OCT 22800 CALL"),
            # Different expiry — must NOT match
            _option_row(22850, "CE", "2026-10-13", custom_sym="NIFTY 13 OCT 22850 CALL"),
        ])

    @pytest.fixture
    def broker(self, df):
        return _make_broker(df)

    def test_ce_found(self, broker):
        sym = broker._find_symbol(22850, "CE", self.EXPIRY)
        assert sym == "NIFTY 06 OCT 22850 CALL"

    def test_pe_found(self, broker):
        sym = broker._find_symbol(22850, "PE", self.EXPIRY)
        assert sym == "NIFTY 06 OCT 22850 PUT"

    def test_different_strike(self, broker):
        sym = broker._find_symbol(22800, "CE", self.EXPIRY)
        assert sym == "NIFTY 06 OCT 22800 CALL"

    def test_wrong_expiry_returns_none(self, broker):
        sym = broker._find_symbol(22850, "CE", "2026-10-20")
        assert sym is None

    def test_wrong_option_type_returns_none(self, broker):
        # 22800 PE doesn't exist in our test df
        sym = broker._find_symbol(22800, "PE", self.EXPIRY)
        assert sym is None

    def test_absent_strike_returns_none(self, broker):
        sym = broker._find_symbol(99999, "CE", self.EXPIRY)
        assert sym is None

    def test_nan_rows_do_not_crash(self, broker):
        """NaN strike rows (equity) must not raise IntCastingNaNError."""
        # This was the exact crash: .astype(float).astype(int) on NaN raises.
        sym = broker._find_symbol(22850, "CE", self.EXPIRY)
        assert sym is not None  # just confirming no exception raised

    def test_inf_rows_do_not_crash(self):
        """Inf strike rows must also not crash (edge case)."""
        inf_row = {
            "SEM_EXM_EXCH_ID": "NSE",
            "SEM_TRADING_SYMBOL": "JUNK",
            "SEM_CUSTOM_SYMBOL": "JUNK",
            "SEM_OPTION_TYPE": None,
            "SEM_STRIKE_PRICE": float("inf"),
            "SEM_EXPIRY_DATE": None,
            "SEM_SMST_SECURITY_ID": 0,
        }
        df = _make_df([inf_row, _option_row(22850, "CE", self.EXPIRY, custom_sym="NIFTY 06 OCT 22850 CALL")])
        broker = _make_broker(df)
        sym = broker._find_symbol(22850, "CE", self.EXPIRY)
        assert sym == "NIFTY 06 OCT 22850 CALL"

    def test_returns_custom_symbol_not_trading_symbol(self, broker):
        """Must return SEM_CUSTOM_SYMBOL (already uppercase, safe for Tradehull.upper())."""
        sym = broker._find_symbol(22850, "CE", self.EXPIRY)
        # SEM_CUSTOM_SYMBOL is "NIFTY 06 OCT 22850 CALL" (already uppercase)
        # SEM_TRADING_SYMBOL was "NIFTY-Oct2026-22850-CE" (mixed case, breaks .upper())
        assert "CALL" in sym or "PUT" in sym  # custom symbol uses CALL/PUT
        assert sym == sym.upper()             # must already be uppercase
