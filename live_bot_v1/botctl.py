#!/usr/bin/env python3
"""
botctl — Control CLI for the running bot.

Commands
--------
  botctl status           Show mode, expiry, selections, positions, risk state
  botctl stop-entries     Halt new entries (open positions still managed)
  botctl resume           Re-enable entries
  botctl flatten          Square off ALL open positions (asks for confirmation)
  botctl tail-logs        Tail the live bot.log
  botctl dump-today       Dump today's state as JSON

The bot writes a control socket file at <STATE_DIR>/botctl.sock.
botctl sends JSON commands and reads JSON responses.
"""

from __future__ import annotations

import argparse
import json
import os
import socket
import subprocess
import sys
from pathlib import Path

DEFAULT_STATE_DIR = Path(os.environ.get("BOT_STATE_DIR", Path(__file__).parent / "state"))
SOCK_PATH = DEFAULT_STATE_DIR / "botctl.sock"
LOG_PATH = Path(os.environ.get("BOT_LOG_DIR", Path(__file__).parent / "logs")) / "bot.log"


def _send_command(state_dir: Path, command: dict) -> dict:
    sock_path = state_dir / "botctl.sock"
    if not sock_path.exists():
        return {"error": f"Socket not found: {sock_path}. Is the bot running?"}
    try:
        with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as s:
            s.connect(str(sock_path))
            s.sendall(json.dumps(command).encode() + b"\n")
            data = b""
            while True:
                chunk = s.recv(4096)
                if not chunk:
                    break
                data += chunk
            return json.loads(data.decode())
    except Exception as e:
        return {"error": str(e)}


def cmd_status(state_dir: Path) -> None:
    resp = _send_command(state_dir, {"cmd": "status"})
    if "error" in resp:
        print(f"ERROR: {resp['error']}")
        sys.exit(1)
    print(json.dumps(resp, indent=2))


def cmd_stop_entries(state_dir: Path) -> None:
    resp = _send_command(state_dir, {"cmd": "stop_entries"})
    if "error" in resp:
        print(f"ERROR: {resp['error']}")
        sys.exit(1)
    print("Entries halted:", resp)


def cmd_resume(state_dir: Path) -> None:
    resp = _send_command(state_dir, {"cmd": "resume"})
    if "error" in resp:
        print(f"ERROR: {resp['error']}")
        sys.exit(1)
    print("Entries resumed:", resp)


def cmd_flatten(state_dir: Path) -> None:
    confirm = input(
        "⚠️  FLATTEN: This will SELL ALL open option positions at market. "
        "Type 'CONFIRM' to proceed: "
    ).strip()
    if confirm != "CONFIRM":
        print("Aborted.")
        return
    resp = _send_command(state_dir, {"cmd": "flatten"})
    if "error" in resp:
        print(f"ERROR: {resp['error']}")
        sys.exit(1)
    print("Flatten result:", resp)


def cmd_tail_logs(log_dir: Path) -> None:
    log_file = log_dir / "bot.log"
    if not log_file.exists():
        print(f"Log file not found: {log_file}")
        sys.exit(1)
    try:
        subprocess.run(["tail", "-f", str(log_file)])
    except KeyboardInterrupt:
        pass


def cmd_dump_today(state_dir: Path) -> None:
    resp = _send_command(state_dir, {"cmd": "dump_today"})
    if "error" in resp:
        print(f"ERROR: {resp['error']}")
        sys.exit(1)
    print(json.dumps(resp, indent=2))


def main() -> None:
    ap = argparse.ArgumentParser(description="Bot control CLI")
    ap.add_argument(
        "--state-dir", type=Path,
        default=DEFAULT_STATE_DIR,
    )
    ap.add_argument(
        "--log-dir", type=Path,
        default=Path(os.environ.get("BOT_LOG_DIR", Path(__file__).parent / "logs")),
    )
    sub = ap.add_subparsers(dest="command", required=True)
    sub.add_parser("status")
    sub.add_parser("stop-entries")
    sub.add_parser("resume")
    sub.add_parser("flatten")
    sub.add_parser("tail-logs")
    sub.add_parser("dump-today")
    args = ap.parse_args()

    if args.command == "status":
        cmd_status(args.state_dir)
    elif args.command == "stop-entries":
        cmd_stop_entries(args.state_dir)
    elif args.command == "resume":
        cmd_resume(args.state_dir)
    elif args.command == "flatten":
        cmd_flatten(args.state_dir)
    elif args.command == "tail-logs":
        cmd_tail_logs(args.log_dir)
    elif args.command == "dump-today":
        cmd_dump_today(args.state_dir)


if __name__ == "__main__":
    main()
