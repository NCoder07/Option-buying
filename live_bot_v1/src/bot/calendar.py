"""
calendar.py — Trading calendar for the live bot.

Uses the expiry list from the broker (data-driven) to determine:
  - Whether today is an expiry day
  - Which expiry to trade (nearest if not expiry day, next if expiry day)
  - Whether today is a trading day (via pandas-market-calendars XNSE)

All date/time logic is in Asia/Kolkata timezone.
"""

from __future__ import annotations

import logging
from datetime import date, timedelta
from typing import Optional

import pytz

logger = logging.getLogger(__name__)
IST = pytz.timezone("Asia/Kolkata")


def today_ist() -> date:
    """Return today's date in IST."""
    from datetime import datetime
    return datetime.now(IST).date()


def is_trading_day(d: date) -> bool:
    """Return True if d is an NSE trading day (uses pandas-market-calendars)."""
    try:
        import pandas_market_calendars as mcal
        xnse = mcal.get_calendar("XNSE")
        schedule = xnse.schedule(start_date=str(d), end_date=str(d))
        return not schedule.empty
    except Exception as e:
        logger.warning("is_trading_day(%s) failed: %s — assuming True", d, e)
        return True


def select_expiry(
    expiry_list: list[str],
    today: Optional[date] = None,
) -> Optional[str]:
    """
    Given a sorted list of expiry dates (YYYY-MM-DD strings), return the
    expiry the strategy should trade today.

    Rule:
      - If today is NOT an expiry day → nearest weekly expiry (first in list >= today)
      - If today IS an expiry day     → next weekly expiry (second in list)

    Returns None if no suitable expiry found.
    """
    if today is None:
        today = today_ist()

    future = [e for e in expiry_list if e >= str(today)]
    if not future:
        logger.error("No upcoming expiry in list: %s (today=%s)", expiry_list, today)
        return None

    nearest = future[0]
    nearest_date = date.fromisoformat(nearest)

    if nearest_date == today:
        # Today IS an expiry day — use the next expiry
        if len(future) >= 2:
            logger.info("Today is expiry day (%s) — using next expiry: %s", today, future[1])
            return future[1]
        else:
            logger.error("Today is expiry day but no next expiry found in list")
            return None
    else:
        logger.info("Using nearest expiry: %s (today=%s)", nearest, today)
        return nearest


def is_expiry_today(expiry_list: list[str], today: Optional[date] = None) -> bool:
    """Return True if today is in the expiry list."""
    if today is None:
        today = today_ist()
    return str(today) in expiry_list


def dte(expiry_date: str, today: Optional[date] = None) -> int:
    """Return days to expiry (calendar days)."""
    if today is None:
        today = today_ist()
    return (date.fromisoformat(expiry_date) - today).days
