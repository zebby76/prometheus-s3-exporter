"""A UDP DNS relay that misbehaves on demand.

Sits between the exporters and CoreDNS, where a Kubernetes node's conntrack
table and the DNS service's DNAT sit in production: every query goes through it,
and it can lose some, slow them down, or answer SERVFAIL in the server's stead.
CoreDNS cannot do this itself for a name it actually resolves -- its `erratic`
plugin runs after `hosts` and `forward`, and only ever answers with a fixed
address.

The fault is read from FAULT_FILE on every query, so `make obs/chaos/...`
changes it without a restart. One `key=value` per line:

    mode=off|drop|delay|servfail
    ratio=0.33       share of matching queries affected (drop, servfail)
    delay_ms=300     added before forwarding (delay)
    match=storage    regular expression on the query name; empty matches all

Standard library only. UDP only: the exporters' resolver never asks for more
than a UDP answer holds.
"""

import logging
import os
import random
import re
import socket
import threading
import time

LISTEN = os.getenv("DNSFAULT_LISTEN", "0.0.0.0")
PORT = int(os.getenv("DNSFAULT_PORT", "53"))
UPSTREAM = os.getenv("DNSFAULT_UPSTREAM", "coredns")
UPSTREAM_PORT = int(os.getenv("DNSFAULT_UPSTREAM_PORT", "53"))
FAULT_FILE = os.getenv("FAULT_FILE", "/state/fault.conf")
UPSTREAM_TIMEOUT = 10

QTYPES = {1: "A", 2: "NS", 5: "CNAME", 6: "SOA", 12: "PTR", 28: "AAAA", 33: "SRV"}


def read_fault():
    fault = {"mode": "off", "ratio": "1", "delay_ms": "0", "match": ""}
    try:
        with open(FAULT_FILE, encoding="utf-8") as handle:
            for line in handle:
                key, sep, value = line.strip().partition("=")
                if sep and not key.startswith("#"):
                    fault[key.strip()] = value.strip()
    except FileNotFoundError:
        pass
    return fault


def question(packet):
    """Name, type and end offset of the first question of the packet."""
    labels = []
    offset = 12
    while offset < len(packet):
        length = packet[offset]
        offset += 1
        if length == 0:
            break
        labels.append(packet[offset : offset + length].decode("ascii", "replace"))
        offset += length
    qtype = int.from_bytes(packet[offset : offset + 2], "big")
    # QTYPE and QCLASS, two bytes each.
    return ".".join(labels), QTYPES.get(qtype, str(qtype)), offset + 4


def servfail(packet, end):
    """The query sent back as a response with RCODE 2 and no answer.

    Cut after the question: an EDNS record in the additional section would
    otherwise trail behind an ARCOUNT of zero.
    """
    flags = int.from_bytes(packet[2:4], "big")
    # QR=1, keep opcode and RD, set RA, RCODE=SERVFAIL.
    flags = 0x8000 | (flags & 0x7900) | 0x0080 | 0x0002
    return (
        packet[:2] + flags.to_bytes(2, "big") + packet[4:6] + b"\0" * 6 + packet[12:end]
    )


def handle(server, packet, client):
    name, qtype, end = question(packet)
    fault = read_fault()
    mode = fault["mode"]
    pattern = fault.get("match", "")
    affected = mode != "off" and (not pattern or re.search(pattern, name))
    if affected and mode in ("drop", "servfail"):
        affected = random.random() < float(fault.get("ratio", "1"))

    if affected and mode == "drop":
        logging.info("DROP %s %s from %s", qtype, name, client[0])
        return
    if affected and mode == "servfail":
        logging.info("SERVFAIL %s %s from %s", qtype, name, client[0])
        server.sendto(servfail(packet, end), client)
        return
    if affected and mode == "delay":
        delay = int(fault.get("delay_ms", "0")) / 1000
        logging.info("DELAY %.0fms %s %s from %s", delay * 1000, qtype, name, client[0])
        time.sleep(delay)

    with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as upstream:
        upstream.settimeout(UPSTREAM_TIMEOUT)
        try:
            # Resolved on every query: a recreated CoreDNS comes back on another
            # address, and a relay still sending to the old one is an outage of
            # its own making.
            upstream_addr = (socket.gethostbyname(UPSTREAM), UPSTREAM_PORT)
            upstream.sendto(packet, upstream_addr)
            answer, _ = upstream.recvfrom(4096)
        except OSError as exc:
            logging.warning("upstream failed for %s %s: %s", qtype, name, exc)
            return
    server.sendto(answer, client)


def main():
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(message)s")
    server = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    server.bind((LISTEN, PORT))
    logging.info(
        "dnsfault on %s:%s -> %s:%s, fault file %s",
        LISTEN,
        PORT,
        UPSTREAM,
        UPSTREAM_PORT,
        FAULT_FILE,
    )
    while True:
        packet, client = server.recvfrom(4096)
        if len(packet) < 12:
            continue
        threading.Thread(
            target=handle, args=(server, packet, client), daemon=True
        ).start()


if __name__ == "__main__":
    main()
