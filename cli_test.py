# This Source Code Form is subject to the terms of the Mozilla Public
# License, v. 2.0. If a copy of the MPL was not distributed with this
# file, You can obtain one at https://mozilla.org/MPL/2.0/.

#!/usr/bin/env python3
"""Standalone CLI tester for the Aprilaire 8800 RS-485 driver.

Run this FIRST on real hardware, before installing the HA integration. It
exercises the protocol layer directly and prints every line the bus
produces, plus parsed messages, so you can confirm:

  1. The serial wiring (or TCP gateway path) is correct.
  2. The thermostat is responding on the addresses you expect.
  3. Timing is healthy - no responses are being cut off mid-line.
  4. Setpoint and mode writes actually take effect (verify on the device LCD).

Usage examples:

    # Through an 8811 adapter on a local USB-RS-232 port.
    python cli_test.py /dev/ttyUSB0 --discover

    # Through a TCP-to-RS-485 gateway (raw TCP). Verify the gateway is
    # 4-wire / RS-422 capable BEFORE trying this.
    python cli_test.py socket://192.168.1.50:8000 --discover

    # Through an RFC 2217 (Moxa NPort etc.) gateway.
    python cli_test.py rfc2217://192.168.1.50:2217 --discover

    # Watch live traffic from node 1.
    python cli_test.py /dev/ttyUSB0 --watch 1

    # Send a one-shot query.
    python cli_test.py /dev/ttyUSB0 --addr 1 --query T

    # Set a heat setpoint.
    python cli_test.py /dev/ttyUSB0 --addr 1 --cmd SH --value 68

    # Run a self-test that probes most read-only commands.
    python cli_test.py /dev/ttyUSB0 --addr 1 --selftest
"""

from __future__ import annotations

import argparse
import logging
import sys
import time
from pathlib import Path

# Make the integration's protocol module importable without installing the
# package. The integration lives under custom_components/aprilaire_rs485/, but
# importing it that way would pull in homeassistant via __init__.py.
sys.path.insert(
    0,
    str(Path(__file__).resolve().parent / "custom_components" / "aprilaire_rs485"),
)

from protocol import (  # noqa: E402
    Aprilaire8800Protocol,
    NodeMessage,
    decode_errors,
    decode_humidity,
    decode_hvac,
    decode_temperature,
)


def _print_msg(msg: NodeMessage) -> None:
    # Pretty-print interesting ones.
    decorated = ""
    if msg.command in ("T", "TEMP"):
        v, s = decode_temperature(msg.value or "")
        decorated = f"  (decoded: {v} {s})"
    elif msg.command == "HUM":
        decorated = f"  (decoded: {decode_humidity(msg.value or '')}%)"
    elif msg.command == "HVAC":
        decorated = f"  (relays: {decode_hvac(msg.value or '')})"
    elif msg.command == "ERROR":
        decorated = f"  (errors: {decode_errors(msg.value or '')})"
    name = f" [{msg.name}]" if msg.name else ""
    print(f"  SN{msg.address}{name} {msg.command} = {msg.value!r}{decorated}")


def cmd_discover(proto: Aprilaire8800Protocol, args: argparse.Namespace) -> int:
    wait_s = proto.slot_seconds * proto.max_address + 1.0
    print(f"Broadcasting global query and waiting {wait_s:.1f}s for replies...")
    proto.send(None, "", query=True)
    time.sleep(wait_s)
    return 0


def cmd_watch(proto: Aprilaire8800Protocol, args: argparse.Namespace) -> int:
    duration = args.duration
    print(f"Watching node {args.watch} for {duration}s. Ctrl-C to stop early.")
    # Just listen; the protocol will keep COS messages flowing if the node
    # already has them enabled. We don't touch any settings here.
    end = time.monotonic() + duration
    try:
        while time.monotonic() < end:
            time.sleep(0.5)
    except KeyboardInterrupt:
        pass
    return 0


def cmd_one_off(proto: Aprilaire8800Protocol, args: argparse.Namespace) -> int:
    if args.query:
        print(f"Querying SN{args.addr} {args.query}?")
        proto.send(args.addr, args.query, query=True)
    elif args.cmd:
        print(f"Sending SN{args.addr} {args.cmd}={args.value}")
        proto.send(args.addr, args.cmd, args.value)
    else:
        print("Specify --query <CMD> or --cmd <CMD> --value <V>", file=sys.stderr)
        return 2
    time.sleep(1.5)  # let the response come back
    return 0


def cmd_selftest(proto: Aprilaire8800Protocol, args: argparse.Namespace) -> int:
    """Run a battery of read-only queries against one node.

    Useful sanity check that the device is actually wired correctly and the
    parser handles its responses.
    """
    addr = args.addr
    queries = [
        "ID",
        "NAME",
        "CT",
        "M",
        "F",
        "T",
        "HUM",
        "SH",
        "SC",
        "SHUM",
        "SDEH",
        "OT",
        "OH",
        "BIHUM",
        "RTS",
        "HVAC",
        "HOLDSTAT",
        "RECOVSTAT",
        "FLTALM",
        "WPALM",
        "DEHALM",
        "SYSALM",
        "ERROR",
        "SCALE",
        "TIME",
        "DATE",
        "PROGFMT",
        "EVTSDAY",
        "BAUD",
        "NETST",
        "RSM",
    ]
    print(f"Self-test on SN{addr}: {len(queries)} queries...")
    for q in queries:
        proto.send(addr, q, query=True)
    # Wait long enough for everything to come back. Each query needs
    # min_gap + a bit of response time. Conservative: 1s per query.
    wait_s = max(8.0, len(queries) * 0.5)
    time.sleep(wait_s)
    return 0


def main() -> int:
    ap = argparse.ArgumentParser(
        description="Standalone tester for the Aprilaire 8800 RS-485 protocol.",
        epilog=(
            "URL examples:\n"
            "  /dev/ttyUSB0            local serial (8811 adapter via USB-RS-232)\n"
            "  COM3                    Windows local serial\n"
            "  hwgrep://USB-RS422      find local serial by device description\n"
            "  socket://192.168.1.50:8000   TCP-to-RS-485 gateway (raw TCP)\n"
            "  rfc2217://192.168.1.50:2217  RFC 2217 remote serial\n"
        ),
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    ap.add_argument("url", metavar="URL", help="Serial port path or pyserial URL (see epilog)")
    ap.add_argument(
        "--baud",
        type=int,
        default=9600,
        choices=[9600, 19200],
        help="Thermostat bus baud rate (irrelevant to TCP link itself)",
    )
    ap.add_argument("--max-address", type=int, default=8)
    ap.add_argument("--addr", type=int, help="Node address for targeted commands")
    ap.add_argument("--query", help="Query command keyword (e.g. T)")
    ap.add_argument("--cmd", help="Assignment command keyword (e.g. SH)")
    ap.add_argument("--value", help="Assignment value (e.g. 68)")
    ap.add_argument("--watch", type=int, help="Watch this node address only")
    ap.add_argument("--duration", type=float, default=60.0, help="Watch duration seconds")
    ap.add_argument("--discover", action="store_true")
    ap.add_argument("--selftest", action="store_true")
    ap.add_argument("-v", "--verbose", action="count", default=0)
    args = ap.parse_args()

    level = logging.WARNING - 10 * args.verbose
    logging.basicConfig(
        level=max(level, logging.DEBUG), format="%(asctime)s %(levelname)s %(name)s %(message)s"
    )

    proto = Aprilaire8800Protocol(args.url, baud=args.baud, max_address=args.max_address)
    proto.add_listener(_print_msg)
    proto.add_raw_listener(lambda line: print(f"RX: {line}"))
    proto.start()
    try:
        if args.discover:
            return cmd_discover(proto, args)
        if args.selftest:
            if args.addr is None:
                print("--selftest requires --addr", file=sys.stderr)
                return 2
            return cmd_selftest(proto, args)
        if args.watch:
            args.watch_addr = args.watch
            return cmd_watch(proto, args)
        if args.query or args.cmd:
            if args.addr is None:
                print("--query/--cmd require --addr", file=sys.stderr)
                return 2
            return cmd_one_off(proto, args)
        print("Nothing to do. Try --discover, --selftest, --watch, or --query.", file=sys.stderr)
        return 2
    finally:
        proto.stop()


if __name__ == "__main__":
    raise SystemExit(main())
