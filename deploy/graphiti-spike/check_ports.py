#!/usr/bin/env python3
"""Port preflight for the PRISM Graphiti/Neo4j spike (Phase A template).

Checks whether the host ports the compose template publishes (defaults 7475
HTTP and 7688 Bolt, override via PRISM_GRAPHITI_HTTP_PORT /
PRISM_GRAPHITI_BOLT_PORT) are already occupied before `docker compose up`.

A preflight REDUCES the chance of a port collision; it does NOT guarantee the
port stays free between this check and container start (another process can
bind in between), and it cannot see container-internal NAT conflicts.  If a
port is occupied, change the values in .env and re-run.

Usage:
    python check_ports.py            # check the two default ports
    python check_ports.py 7475 7688  # check explicit ports
Exit code 0 = free, 1 = occupied, 2 = usage error.

Standard library only: runs anywhere without installing PRISM or its
dependencies and never connects to any service.
"""

from __future__ import annotations

import os
import socket
import sys


def port_occupied(port: int, host: str = "127.0.0.1") -> bool:
    """True when something on ``host`` accepts TCP connections on ``port``."""
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.settimeout(0.5)
        return sock.connect_ex((host, port)) == 0


def main(argv: list[str]) -> int:
    defaults = [
        int(os.environ.get("PRISM_GRAPHITI_HTTP_PORT", "7475")),
        int(os.environ.get("PRISM_GRAPHITI_BOLT_PORT", "7688")),
    ]
    try:
        ports = [int(value) for value in (argv[1:] or defaults)]
    except ValueError:
        print("usage: check_ports.py [PORT ...]", file=sys.stderr)
        return 2
    if not ports or any(not 1 <= port <= 65535 for port in ports):
        print("usage: check_ports.py [PORT ...]  (each 1..65535)", file=sys.stderr)
        return 2

    occupied = [port for port in ports if port_occupied(port)]
    if occupied:
        print(
            "port(s) already in use: "
            + ", ".join(str(port) for port in occupied)
            + " - free them or change PRISM_GRAPHITI_HTTP_PORT/"
            "PRISM_GRAPHITI_BOLT_PORT in .env",
            file=sys.stderr,
        )
        return 1
    print("ports are free: " + ", ".join(str(port) for port in ports))
    print(
        "note: this preflight does not guarantee the ports stay free until "
        "the container starts; a concurrent bind can still race it."
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
