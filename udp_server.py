#!/usr/bin/env python3
"""
udp_server.py
=============
Multi-flow, multi-threaded UDP Server for the QoS Traffic Management System.

Demonstrates low-level socket programming:
  - Low-level datagram socket creation (socket.socket(AF_INET, SOCK_DGRAM))
  - Multi-threaded concurrent listening across multiple UDP ports (e.g., 5001, 5002, 5003)
  - Custom binary protocol parsing (seq_num, flow_id, timestamp, payload)
  - Real-time calculation of Throughput (Mbps), Packet Loss %, and One-Way Delay/Jitter
  - Real-time JSON export for live Web Dashboard integration
"""

import os
import sys
import time
import json
import struct
import socket
import select
import logging
import argparse
import threading
from typing import Dict, List, Optional

# Protocol header format:
# ! = Network byte order (big-endian)
# I = 4-byte unsigned int (sequence number)
# I = 4-byte unsigned int (flow ID / class ID)
# d = 8-byte double (sender timestamp)
HEADER_FORMAT = "!IId"
HEADER_SIZE = struct.calcsize(HEADER_FORMAT)

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%H:%M:%S"
)
logger = logging.getLogger("UDPServer")
LOGS_FILE = "/tmp/qos_logs.txt"


def append_system_log(msg: str):
    """Appends timestamped event string to shared logs file for dashboard."""
    try:
        ts = time.strftime("%H:%M:%S")
        with open(LOGS_FILE, "a") as f:
            f.write(f"[{ts}] {msg}\n")
        try:
            os.chmod(LOGS_FILE, 0o666)
        except Exception:
            pass
    except Exception:
        pass


class FlowTracker:
    """Tracks metrics for an individual UDP flow."""
    def __init__(self, port: int, class_name: str):
        self.port = port
        self.class_name = class_name
        self.lock = threading.Lock()
        
        # Cumulative stats
        self.total_packets_received = 0
        self.total_bytes_received = 0
        self.last_seq = -1
        self.packets_lost = 0
        self.out_of_order = 0
        
        # Latency tracking
        self.latencies_ms: List[float] = []
        
        # Windowed stats for real-time reporting (per second)
        self.window_bytes = 0
        self.window_packets = 0
        self.window_lost = 0
        self.last_window_time = time.time()
        
        # Latest computed rates
        self.current_mbps = 0.0
        self.current_loss_pct = 0.0
        self.current_latency_ms = 0.0

    def record_packet(self, seq_num: int, send_time: float, pkt_len: int, recv_time: float):
        with self.lock:
            self.total_packets_received += 1
            self.total_bytes_received += pkt_len
            self.window_bytes += pkt_len
            self.window_packets += 1
            
            # Sequence number tracking for loss detection
            if self.last_seq != -1:
                seq_diff = seq_num - self.last_seq
                if seq_diff > 1:
                    lost_in_gap = seq_diff - 1
                    self.packets_lost += lost_in_gap
                    self.window_lost += lost_in_gap
                elif seq_diff < 0:
                    self.out_of_order += 1
            self.last_seq = seq_num
            
            # Latency (one-way delay if clocks are synchronized, or relative trend)
            delay = (recv_time - send_time) * 1000.0
            if delay > 0:
                self.latencies_ms.append(delay)
                self.current_latency_ms = delay

    def tick_window(self) -> dict:
        """Calculates and resets the 1-second moving window."""
        with self.lock:
            now = time.time()
            elapsed = now - self.last_window_time
            if elapsed <= 0:
                elapsed = 1.0
            
            # Throughput in Mbps
            self.current_mbps = (self.window_bytes * 8.0) / (elapsed * 1_000_000.0)
            
            # Window loss percentage
            total_expected = self.window_packets + self.window_lost
            if total_expected > 0:
                self.current_loss_pct = (self.window_lost / total_expected) * 100.0
            else:
                self.current_loss_pct = 0.0
            
            # Reset window
            self.window_bytes = 0
            self.window_packets = 0
            self.window_lost = 0
            self.last_window_time = now
            
            return {
                "port": self.port,
                "class_name": self.class_name,
                "mbps": round(self.current_mbps, 2),
                "loss_pct": round(self.current_loss_pct, 1),
                "latency_ms": round(self.current_latency_ms, 2),
                "total_packets": self.total_packets_received,
                "total_lost": self.packets_lost,
            }


class UDPMultiServer:
    """Manages listening sockets and worker threads for multiple traffic classes."""
    def __init__(self, bind_ip: str, ports: List[int], class_labels: Dict[int, str]):
        self.bind_ip = bind_ip
        self.ports = ports
        self.class_labels = class_labels
        self.running = False
        self.sockets: Dict[int, socket.socket] = {}
        self.trackers: Dict[int, FlowTracker] = {}
        self.threads: List[threading.Thread] = []

        for p in ports:
            label = class_labels.get(p, f"Port {p}")
            self.trackers[p] = FlowTracker(p, label)

    def _listen_worker(self, port: int, sock: socket.socket):
        tracker = self.trackers[port]
        logger.info(f"Worker listening on port {port} [{tracker.class_name}]")
        first_packet = True
        
        sock.setblocking(False)
        while self.running:
            # Low-level I/O multiplexing with select
            readable, _, _ = select.select([sock], [], [], 0.5)
            if not readable:
                continue
            
            try:
                data, addr = sock.recvfrom(65535)
                recv_time = time.time()
                if first_packet:
                    append_system_log(f"Incoming traffic stream detected on port {port} from {addr[0]}")
                    first_packet = False
                if len(data) >= HEADER_SIZE:
                    seq_num, flow_id, send_time = struct.unpack(HEADER_FORMAT, data[:HEADER_SIZE])
                    tracker.record_packet(seq_num, send_time, len(data), recv_time)
            except OSError:
                break

    def start(self):
        self.running = True
        logger.info("=" * 65)
        logger.info("       STARTING LOW-LEVEL MULTI-FLOW UDP QOS SERVER")
        logger.info("=" * 65)
        append_system_log(f"UDP Server initialized on ports {self.ports}")
        
        for p in self.ports:
            # Low-level socket creation
            s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
            s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            # Increase kernel receive buffer to avoid drops at OS socket buffer level
            try:
                s.setsockopt(socket.SOL_SOCKET, socket.SO_RCVBUF, 2 * 1024 * 1024)
            except Exception:
                pass
            s.bind((self.bind_ip, p))
            self.sockets[p] = s
            
            t = threading.Thread(target=self._listen_worker, args=(p, s), daemon=True)
            t.start()
            self.threads.append(t)

        # Reporter thread
        reporter = threading.Thread(target=self._stats_reporter, daemon=True)
        reporter.start()

    def _stats_reporter(self):
        """Prints formatted metrics table to terminal every second and saves to JSON."""
        import tempfile
        STATS_PATH = "/tmp/qos_stats.json"
        time.sleep(1.0)
        while self.running:
            time.sleep(1.0)
            stats = [self.trackers[p].tick_window() for p in self.ports]
            total_mbps = sum(s["mbps"] for s in stats)
            
            # Export to /tmp/qos_stats.json using atomic write (temp file + rename)
            # This prevents the dashboard from reading a partial/corrupted file.
            try:
                payload = {
                    "timestamp": time.time(),
                    "total_mbps": round(total_mbps, 2),
                    "link_capacity_mbps": 10.0,
                    "flows": stats
                }
                # Write to a temp file in /tmp (same filesystem → rename is atomic)
                fd, tmp_path = tempfile.mkstemp(dir="/tmp", suffix=".json")
                try:
                    with os.fdopen(fd, "w") as tf:
                        json.dump(payload, tf)
                    os.chmod(tmp_path, 0o666)
                    os.replace(tmp_path, STATS_PATH)   # atomic on Linux
                except Exception:
                    try:
                        os.unlink(tmp_path)
                    except Exception:
                        pass
            except Exception:
                pass
            
            # Print tabular overview
            logger.info("-----------------------------------------------------------------")
            logger.info(f"{'Class / Port':<22} | {'Throughput':>12} | {'Loss %':>8} | {'Delay':>9}")
            logger.info("-----------------------------------------------------------------")
            for s in stats:
                lbl = f"{s['class_name']} ({s['port']})"
                logger.info(f"{lbl:<22} | {s['mbps']:>9.2f} Mbps | {s['loss_pct']:>7.1f}% | {s['latency_ms']:>6.1f} ms")
            logger.info("-----------------------------------------------------------------")
            logger.info(f"{'AGGREGATE RECEIVE':<22} | {total_mbps:>9.2f} Mbps (Bottleneck cap: 10M)")
            logger.info("-----------------------------------------------------------------")

    def stop(self):
        self.running = False
        for s in self.sockets.values():
            try:
                s.close()
            except Exception:
                pass
        for t in self.threads:
            t.join(timeout=1.0)
        logger.info("UDP Server stopped cleanly.")


def main():
    parser = argparse.ArgumentParser(description="SDN QoS Low-Level UDP Receiver Server")
    parser.add_argument("--bind", default="0.0.0.0", help="IP address to bind to (default: 0.0.0.0)")
    parser.add_argument("--ports", nargs="+", type=int, default=[5001, 5002, 5003],
                        help="List of UDP ports to listen on (default: 5001 5002 5003)")
    args = parser.parse_args()

    # Priority mapping matching the SDN controller
    labels = {
        5001: "High-Priority (h1)",
        5002: "Medium-Priority (h2)",
        5003: "Best-Effort (h4)",
    }

    server = UDPMultiServer(args.bind, args.ports, labels)
    server.start()

    try:
        while True:
            time.sleep(0.5)
    except KeyboardInterrupt:
        logger.info("Shutting down server...")
        server.stop()


if __name__ == "__main__":
    main()
