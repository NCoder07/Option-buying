#!/usr/bin/env python3
"""
main.py — Entry point for the NIFTY 50 weekly option buying bot.

Usage
-----
  # Paper mode (default; no --live flag = paper):
  python main.py

  # Live mode (requires CLI flag AND env var):
  CONFIRM_LIVE=YES python main.py --live

  # Override config or paths:
  python main.py --config /etc/nifty_bot/config.yaml --state-dir /var/lib/nifty_bot

Environment variables (from /etc/nifty_bot/bot.env or .env):
  DHAN_CLIENT_CODE, DHAN_PIN, DHAN_TOTP_SECRET
  TELEGRAM_BOT_TOKEN, TELEGRAM_CHAT_ID
  HEARTBEAT_URL
  EXPECTED_STATIC_IP
  CONFIRM_LIVE     (must be "YES" together with --live)

All paths come from config/CLI/env — NO hard-coded /Users/... paths.
"""

from __future__ import annotations

import argparse
import logging
import logging.handlers
import os
import sys
import threading
from pathlib import Path

import pytz

# ---------------------------------------------------------------------------
# Path setup: add live_bot_v1/src to sys.path
# ---------------------------------------------------------------------------
_THIS = Path(__file__).parent.resolve()
sys.path.insert(0, str(_THIS / "src"))


def _setup_logging(log_dir: Path, level: str) -> None:
    log_dir.mkdir(parents=True, exist_ok=True)
    fmt = logging.Formatter(
        "%(asctime)s %(levelname)-8s %(name)s - %(message)s",
        datefmt="%Y-%m-%dT%H:%M:%S",
    )
    root = logging.getLogger()
    root.setLevel(getattr(logging, level.upper(), logging.INFO))

    # Console handler — force UTF-8 on Windows (cp1252 can't handle arrows/emoji)
    if sys.platform == "win32":
        import io
        utf8_stdout = io.TextIOWrapper(
            sys.stdout.buffer, encoding="utf-8", errors="replace", line_buffering=True
        )
        ch = logging.StreamHandler(utf8_stdout)
    else:
        ch = logging.StreamHandler(sys.stdout)
    ch.setFormatter(fmt)
    root.addHandler(ch)

    # Rotating file handler (10 MB × 5 backups) — always UTF-8
    fh = logging.handlers.RotatingFileHandler(
        log_dir / "bot.log", maxBytes=10 * 1024 * 1024, backupCount=5,
        encoding="utf-8",
    )
    fh.setFormatter(fmt)
    root.addHandler(fh)

    # Suppress noisy third-party loggers
    for noisy in ["urllib3", "requests", "websockets", "asyncio"]:
        logging.getLogger(noisy).setLevel(logging.WARNING)


def _load_env(env_file: Path | None) -> None:
    try:
        from dotenv import load_dotenv
        if env_file and env_file.exists():
            load_dotenv(env_file)
            logging.getLogger(__name__).info("Loaded env from %s", env_file)
        else:
            load_dotenv()
    except ImportError:
        pass


def _load_config(config_path: Path) -> dict:
    import yaml
    with config_path.open() as f:
        cfg = yaml.safe_load(f) or {}
    return cfg


def _print_live_banner(config: dict, broker, ip: str) -> None:
    """Print a prominent banner before enabling live trading."""
    lots = int(config.get("lots", 1))
    max_exposure = float(config.get("max_order_value", 200_000)) * 2  # CE + PE
    client = os.environ.get("DHAN_CLIENT_CODE", "???")
    masked_client = client[:4] + "****" if len(client) > 4 else "****"

    banner = f"""
╔══════════════════════════════════════════════════════════════╗
║           *** LIVE TRADING MODE ACTIVATED ***               ║
╠══════════════════════════════════════════════════════════════╣
║  Strategy : NIFTY 50 Weekly Option Buying v1                ║
║  Lots     : {lots:<48} ║
║  Max exp  : ₹{max_exposure:,.0f}{'':<42} ║
║  Account  : {masked_client:<48} ║
║  VM IP    : {ip:<48} ║
╚══════════════════════════════════════════════════════════════╝
"""
    print(banner)
    logging.getLogger(__name__).warning("LIVE MODE ACTIVE — lots=%d account=%s IP=%s",
                                         lots, masked_client, ip)


def _acquire_lock(lock_path: Path):
    """
    Acquire an exclusive OS-level lock on lock_path.

    Returns the open file handle on success (keep it open for the life of the
    process — the OS releases the lock automatically when the process exits or
    the handle is closed).

    Returns None if the lock is already held by another process.

    Works on both Windows (msvcrt.locking) and Linux/macOS (fcntl.flock).
    """
    try:
        f = open(lock_path, "w")
        if sys.platform == "win32":
            import msvcrt
            # LK_NBLCK: exclusive lock, fail immediately if already locked
            msvcrt.locking(f.fileno(), msvcrt.LK_NBLCK, 1)
        else:
            import fcntl
            fcntl.flock(f, fcntl.LOCK_EX | fcntl.LOCK_NB)
        f.write(str(os.getpid()))
        f.flush()
        return f          # caller must keep this reference alive
    except (OSError, IOError):
        try:
            f.close()
        except Exception:
            pass
        return None       # lock already held


def main() -> None:
    ap = argparse.ArgumentParser(
        description="NIFTY 50 weekly option buying bot"
    )
    ap.add_argument(
        "--live", action="store_true",
        help="Enable live trading (ALSO requires env CONFIRM_LIVE=YES)"
    )
    ap.add_argument(
        "--config", type=Path,
        default=_THIS / "config" / "config.yaml",
        help="Path to config YAML",
    )
    ap.add_argument(
        "--state-dir", type=Path,
        default=Path(os.environ.get("BOT_STATE_DIR", str(_THIS / "state"))),
        help="Directory for SQLite DB and Dhan Dependencies/",
    )
    ap.add_argument(
        "--data-dir", type=Path,
        default=Path(os.environ.get("BOT_DATA_DIR", str(_THIS / "data"))),
        help="Directory for tick data, journal, chain snapshots",
    )
    ap.add_argument(
        "--log-dir", type=Path,
        default=Path(os.environ.get("BOT_LOG_DIR", str(_THIS / "logs"))),
        help="Directory for rotating log files",
    )
    ap.add_argument(
        "--env", type=Path, default=None,
        help="Path to .env file (default: .env in CWD or system env)",
    )
    ap.add_argument("--log-level", default="INFO")
    args = ap.parse_args()

    _load_env(args.env)
    _setup_logging(args.log_dir, args.log_level)
    logger = logging.getLogger(__name__)

    # Load config
    if not args.config.exists():
        logger.error("Config not found: %s", args.config)
        sys.exit(1)
    config = _load_config(args.config)

    # Determine mode
    mode = "paper"
    if args.live:
        confirm = os.environ.get("CONFIRM_LIVE", "").strip().upper()
        if confirm != "YES":
            logger.error(
                "Live mode requires BOTH --live flag AND env CONFIRM_LIVE=YES. "
                "Set CONFIRM_LIVE=YES in your environment and retry."
            )
            sys.exit(1)
        mode = "live"
    config["mode"] = mode
    logger.info("Mode: %s", mode.upper())

    # Create directories
    args.state_dir.mkdir(parents=True, exist_ok=True)
    args.data_dir.mkdir(parents=True, exist_ok=True)
    (args.data_dir / "journal").mkdir(parents=True, exist_ok=True)
    (args.state_dir / "Dependencies").mkdir(parents=True, exist_ok=True)

    # ------------------------------------------------------------------
    # Single-instance lock: refuse to start if another copy is running.
    # Uses an OS-level exclusive file lock (works on Windows and Linux).
    # ------------------------------------------------------------------
    lock_path = args.state_dir / "bot.lock"
    lock_file = _acquire_lock(lock_path)
    if lock_file is None:
        pid_path = args.state_dir / "bot.pid"
        existing_pid = pid_path.read_text().strip() if pid_path.exists() else "unknown"
        logger.error(
            "DUPLICATE INSTANCE BLOCKED: another bot process (PID %s) is already "
            "running. Lock file: %s  Kill the other process first, then retry.",
            existing_pid, lock_path,
        )
        print(
            f"\n*** ERROR: Bot is already running (PID {existing_pid}). ***\n"
            "Run:  Get-Process python  to see all Python processes.\n"
            f"Run:  Stop-Process -Id {existing_pid}  to stop the existing one.\n"
            "Then start this script again.\n"
        )
        sys.exit(1)

    # Change CWD to state_dir so Dhan_Tradehull writes Dependencies/ there
    os.chdir(args.state_dir)
    logger.info("CWD set to: %s (Tradehull Dependencies/ will be written here)", args.state_dir)

    # Alerts
    from bot.alerts import Alerts
    alerts = Alerts(mode=mode)

    # Build broker
    client_code = os.environ.get("DHAN_CLIENT_CODE", "")
    pin = os.environ.get("DHAN_PIN", "")
    totp_secret = os.environ.get("DHAN_TOTP_SECRET", "")

    if not all([client_code, pin, totp_secret]):
        logger.error(
            "Missing credentials: DHAN_CLIENT_CODE, DHAN_PIN, DHAN_TOTP_SECRET "
            "must all be set in environment / .env file"
        )
        sys.exit(1)

    from bot.live_broker import LiveDhanBroker
    live_broker = LiveDhanBroker(
        client_code=client_code,
        pin=pin,
        totp_secret=totp_secret,
        config=config,
        state_dir=str(args.state_dir),
    )

    if mode == "paper":
        from bot.paper_broker import PaperBroker
        broker = PaperBroker(live_broker)
    else:
        broker = live_broker

    # Live mode: IP guard + banner
    if mode == "live":
        from bot.ip_guard import get_public_ip
        vm_ip = get_public_ip() or "unknown"
        _print_live_banner(config, broker, vm_ip)
        alerts.startup(mode, int(config.get("lots", 1)),
                       float(config.get("max_order_value", 200_000)) * 2,
                       client_code, vm_ip)
    else:
        alerts.startup(mode, int(config.get("lots", 1)),
                       float(config.get("max_order_value", 200_000)),
                       client_code, "N/A (paper)")

    # Write PID file for botctl
    pid_path = args.state_dir / "bot.pid"
    pid_path.write_text(str(os.getpid()))
    logger.info("PID %d written to %s", os.getpid(), pid_path)

    # Start main loop
    stop_event = threading.Event()

    from bot.lifecycle import run_bot
    try:
        run_bot(
            broker=broker,
            config=config,
            state_dir=args.state_dir,
            data_dir=args.data_dir,
            mode=mode,
            alerts=alerts,
            stop_event=stop_event,
        )
    except KeyboardInterrupt:
        logger.info("KeyboardInterrupt — shutting down")
    except Exception as e:
        logger.critical("Unhandled exception in main loop: %s", e, exc_info=True)
        alerts.halt(f"Unhandled exception: {e}")
        sys.exit(1)
    finally:
        try:
            pid_path.unlink()
        except Exception:
            pass
        # Release the instance lock — allows a clean restart
        try:
            lock_file.close()
            lock_path.unlink(missing_ok=True)
        except Exception:
            pass
        logger.info("Bot exited")


if __name__ == "__main__":
    main()
