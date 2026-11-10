#!/usr/bin/env python3
"""
udp_client.py
=============
Low-Level UDP Traffic Generator Client for the QoS Traffic Management System.

Demonstrates low-level socket programming:
  - Low-level datagram socket creation (socket.socket(AF_INET, SOCK_DGRAM))
  - Setting IP Type of Service (IP_TOS / DiffServ DSCP) directly on the socket
  - Precise bandwidth pacing algorithm to create controlled link congestion
  - Custom binary packet framing with sequence numbers and timestamps
  - Configurable traffic classes:
      • High-Priority   (VoIP / Urgent)       -> DSCP 46 (EF)   -> Target: Queue 0 (6 Mbps)
      • Medium-Priority (Video Streaming)     -> DSCP 26 (AF31) -> Target: Queue 1 (3 Mbps)
      • Best-Effort     (Background Transfer) -> DSCP 0  (BE)   -> Target: Queue 2 (1 Mbps)
"""

import sys
import time
import struct
import socket
import logging
import argparse

HEADER_FORMAT = "!IId"
HEADER_SIZE = struct.calcsize(HEADER_FORMAT)

# Standard DiffServ DSCP values (RFC 4594 / RFC 2474)
DSCP_MAP = {
    "high": 46,       # Expedited Forwarding (EF)
    "medium": 26,     # Assured Forwarding 31 (AF31)
    "best_effort": 0  # Best Effort (BE)
}

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%H:%M:%S"
)
logger = logging.getLogger("UDPClient")


def parse_rate_mbps(rate_str: str) -> float:
    """Parses rate strings like '10M', '500K', or '10.5' into float Mbps."""
    s = rate_str.strip().upper()
    if s.endswith("M") or s.endswith("MBPS"):
        num = s.rstrip("MBPS")
        return float(num)
    elif s.endswith("K") or s.endswith("KBPS"):
        num = s.rstrip("KBPS")
        return float(num) / 1000.0
    return float(s)


def run_client(
    server_ip: str,
    server_port: int,
    rate_mbps: float,
    duration: float,
    packet_size: int,
    class_type: str,
    flow_id: int
):
    # ── 1. Create low-level UDP socket ────────────────────────────────────────
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    
    # ── 2. Configure DiffServ DSCP (Socket-SDN Integration) ───────────────────
    # In IPv4, TOS byte holds 6-bit DSCP shifted left by 2 bits.
    dscp_val = DSCP_MAP.get(class_type.lower(), 0)
    tos_val = dscp_val << 2
    try:
        sock.setsockopt(socket.IPPROTO_IP, socket.IP_TOS, tos_val)
        logger.info(f"Socket IP_TOS set to {tos_val} (DSCP {dscp_val}) for class '{class_type}'")
    except Exception as exc:
        logger.warning(f"Could not set IP_TOS ({exc}). Proceeding with standard header.")

    # Prepare payload padding
    if packet_size < HEADER_SIZE:
        packet_size = HEADER_SIZE + 64
    payload_padding = b"X" * (packet_size - HEADER_SIZE)

    # Transmission pacing parameters
    packet_bits = packet_size * 8
    target_bps = rate_mbps * 1_000_000.0
    packets_per_sec = target_bps / packet_bits
    inter_packet_interval = 1.0 / packets_per_sec if packets_per_sec > 0 else 0.001

    logger.info("=" * 65)
    logger.info(f"  TARGET: {server_ip}:{server_port}")
    logger.info(f"  CLASS: {class_type.upper()} | DSCP: {dscp_val}")
    logger.info(f"  SEND RATE: {rate_mbps} Mbps (~{packets_per_sec:.0f} pkts/sec)")
    logger.info(f"  PACKET SIZE: {packet_size} bytes | DURATION: {duration}s")
    logger.info("=" * 65)

    seq_num = 0
    total_bytes_sent = 0
    start_time = time.time()
    next_send_time = start_time

    try:
        while True:
            now = time.time()
            elapsed = now - start_time
            if elapsed >= duration:
                break

            # Pack binary protocol header: (seq_num, flow_id, send_timestamp)
            header = struct.pack(HEADER_FORMAT, seq_num, flow_id, now)
            packet_data = header + payload_padding

            # Low-level datagram transmission
            sock.sendto(packet_data, (server_ip, server_port))
            total_bytes_sent += len(packet_data)
            seq_num += 1

            # Precision pacing
            next_send_time += inter_packet_interval
            sleep_duration = next_send_time - time.time()
            if sleep_duration > 0.0005:
                time.sleep(sleep_duration)

    except KeyboardInterrupt:
        logger.info("Interrupted by user.")
    finally:
        sock.close()

    total_time = time.time() - start_time
    effective_mbps = (total_bytes_sent * 8.0) / (total_time * 1_000_000.0) if total_time > 0 else 0.0

    logger.info("=" * 65)
    logger.info("                   TRANSMISSION FINISHED")
    logger.info(f"  Packets Sent : {seq_num}")
    logger.info(f"  Total Data   : {total_bytes_sent / 1e6:.2f} MB")
    logger.info(f"  Actual Rate  : {effective_mbps:.2f} Mbps")
    logger.info(f"  Duration     : {total_time:.2f} s")
    logger.info("=" * 65)


def main():
    parser = argparse.ArgumentParser(description="SDN QoS Low-Level UDP Client Generator")
    parser.add_argument("--server", default="10.0.0.3", help="Target server IP address (default: 10.0.0.3)")
    parser.add_argument("--port", type=int, default=5001, help="Target UDP port (default: 5001)")
    parser.add_argument("--rate", default="10M", help="Transmission rate (e.g., 10M, 3M, 1M) (default: 10M)")
    parser.add_argument("--time", type=float, default=30.0, help="Transmission duration in seconds (default: 30)")
    parser.add_argument("--size", type=int, default=1400, help="Datagram payload size in bytes (default: 1400)")
    parser.add_argument("--class-type", choices=["high", "medium", "best_effort"], default="high",
                        help="QoS traffic class: high, medium, best_effort (default: high)")
    parser.add_argument("--flow-id", type=int, default=1, help="Flow identifier integer (default: 1)")

    args = parser.parse_args()
    rate_mbps = parse_rate_mbps(args.rate)

    run_client(
        server_ip=args.server,
        server_port=args.port,
        rate_mbps=rate_mbps,
        duration=args.time,
        packet_size=args.size,
        class_type=args.class_type,
        flow_id=args.flow_id
    )


if __name__ == "__main__":
    main()
