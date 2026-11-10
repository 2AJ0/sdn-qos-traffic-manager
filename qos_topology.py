"""
qos_topology.py
===============
Custom Mininet topology for the QoS SDN proof-of-concept.

Topology
--------
  h1 (10.0.0.1) ─┐
  h2 (10.0.0.2) ─┤── s1 ══(10 Mbps / 10 ms)══ s2 ── h3 (10.0.0.3)
  h4 (10.0.0.4) ─┘

Controller
----------
  Remote Ryu controller at 127.0.0.1:6653 (qos_controller.py)

Usage
-----
  sudo python3 qos_topology.py
  (Start qos_controller.py first with ryu-manager)
"""

from mininet.net import Mininet
from mininet.node import OVSSwitch, RemoteController
from mininet.cli import CLI
from mininet.link import TCLink
from mininet.log import setLogLevel, info
import time


def append_system_log(msg: str):
    """Appends timestamped event string to shared logs file for dashboard."""
    try:
        ts = time.strftime("%H:%M:%S")
        with open("/tmp/qos_logs.txt", "a") as f:
            f.write(f"[{ts}] {msg}\n")
    except Exception:
        pass


def start_qos_traffic(net, config="40"):
    """Starts UDP server on h3 and simultaneous clients on selected hosts with custom rates."""
    import json
    h1, h2, h3, h4 = net.get('h1', 'h2', 'h3', 'h4')
    info("\n*** Stopping any existing traffic...\n")
    for h in [h1, h2, h3, h4]:
        h.cmd('pkill -9 -f udp_server.py')
        h.cmd('pkill -9 -f udp_client.py')

    info("*** Starting low-level UDP Server on h3 (listening on 5001, 5002, 5003)...\n")
    h3.cmd('python3 udp_server.py &')
    time.sleep(1.5)

    duration = "40"
    h1_cfg = {"enabled": True, "rate": 10}
    h2_cfg = {"enabled": True, "rate": 10}
    h4_cfg = {"enabled": True, "rate": 10}

    if isinstance(config, dict):
        duration = str(config.get("duration", 40))
        h1_cfg = config.get("h1", h1_cfg)
        h2_cfg = config.get("h2", h2_cfg)
        h4_cfg = config.get("h4", h4_cfg)
    elif isinstance(config, str) and config.strip().isdigit():
        duration = config.strip()

    info(f"*** Launching configured UDP senders (Duration: {duration}s)...\n")
    active_hosts = []

    if h1_cfg.get("enabled", True):
        r1 = h1_cfg.get("rate", 10)
        info(f"    • h1: High Priority   (VoIP)  -> {r1} Mbps target -> Queue 0 (6 Mbps)\n")
        h1.cmd(f'python3 udp_client.py --server 10.0.0.3 --port 5001 --class-type high --rate {r1}M --time {duration} &')
        active_hosts.append(f"h1 ({r1}M)")

    if h2_cfg.get("enabled", True):
        r2 = h2_cfg.get("rate", 10)
        info(f"    • h2: Medium Priority (Video) -> {r2} Mbps target -> Queue 1 (3 Mbps)\n")
        h2.cmd(f'python3 udp_client.py --server 10.0.0.3 --port 5002 --class-type medium --rate {r2}M --time {duration} &')
        active_hosts.append(f"h2 ({r2}M)")

    if h4_cfg.get("enabled", True):
        r4 = h4_cfg.get("rate", 10)
        info(f"    • h4: Best Effort     (Bulk)  -> {r4} Mbps target -> Queue 2 (1 Mbps)\n")
        h4.cmd(f'python3 udp_client.py --server 10.0.0.3 --port 5003 --class-type best_effort --rate {r4}M --time {duration} &')
        active_hosts.append(f"h4 ({r4}M)")

    hosts_str = ", ".join(active_hosts) if active_hosts else "None"
    append_system_log(f"Mininet launched experiment: [{hosts_str}] for {duration}s")
    info(f"*** Traffic started! Open http://localhost:8080 to watch the live QoS enforcement! ***\n\n")


def stop_qos_traffic(net):
    """Stops all running UDP clients and servers."""
    for h in net.get('h1', 'h2', 'h3', 'h4'):
        h.cmd('pkill -9 -f udp_server.py')
        h.cmd('pkill -9 -f udp_client.py')
    append_system_log("Mininet stopped all running UDP traffic generators")
    info("*** All UDP traffic generators and servers stopped.\n")


class QoSCLI(CLI):
    """Custom Mininet CLI with built-in commands for the QoS demonstration."""

    def do_traffic(self, line):
        """Run simultaneous multi-class UDP QoS traffic across h1, h2, h4 to h3."""
        duration = line.strip() if line.strip().isdigit() else "40"
        start_qos_traffic(self.mn, duration)

    def do_stop_traffic(self, line):
        """Stop all running UDP client and server processes."""
        stop_qos_traffic(self.mn)


def create_network():

    # ── Instantiate network with a remote OpenFlow controller ─────────────────
    net = Mininet(
        controller=None,      # we add our remote controller manually below
        switch=OVSSwitch,
        link=TCLink,
        autoSetMacs=True,     # assign deterministic MACs (00:00:00:00:00:01, …)
        autoStaticArp=False,  # let ARP work through the SDN controller
    )

    # ── Remote Ryu controller (qos_controller.py) ─────────────────────────────
    ctrl = net.addController(
        'c0',
        controller=RemoteController,
        ip='127.0.0.1',
        port=6653,
        protocols='OpenFlow13',
    )

    # ── Hosts ─────────────────────────────────────────────────────────────────
    h1 = net.addHost('h1', ip='10.0.0.1/24')   # High-priority
    h2 = net.addHost('h2', ip='10.0.0.2/24')   # Medium-priority
    h3 = net.addHost('h3', ip='10.0.0.3/24')   # Destination server
    h4 = net.addHost('h4', ip='10.0.0.4/24')   # Best-effort / low-priority

    # ── Switches ──────────────────────────────────────────────────────────────
    # failMode='secure': switch drops traffic unless the controller instructs it
    s1 = net.addSwitch('s1', failMode='secure', protocols='OpenFlow13')
    s2 = net.addSwitch('s2', failMode='secure', protocols='OpenFlow13')

    # ── Access links (host → switch, 100 Mbps, no artificial delay) ───────────
    net.addLink(h1, s1, bw=100)
    net.addLink(h2, s1, bw=100)
    net.addLink(h4, s1, bw=100)
    net.addLink(h3, s2, bw=100)

    # ── Bottleneck inter-switch link (10 Mbps, 10 ms RTT) ────────────────────
    # NOTE: use_htb is intentionally NOT set here.
    # Mininet's TCLink would attach its own Linux HTB root qdisc, which
    # conflicts with the OVS HTB qdisc installed by qos_controller.py via
    # ovs-vsctl.  Let qos_controller.py own the full HTB hierarchy on this port.
    net.addLink(
        s1, s2,
        bw=10,
        delay='10ms',
    )

    # ── Start ─────────────────────────────────────────────────────────────────
    net.build()
    ctrl.start()
    s1.start([ctrl])
    s2.start([ctrl])

    info("\n" + "=" * 58 + "\n")
    info("         QoS TRAFFIC MANAGEMENT SYSTEM\n")
    info("=" * 58 + "\n")
    info("  h1 (10.0.0.1) → HIGH PRIORITY    → OVS Queue 0 (6 Mbps)\n")
    info("  h2 (10.0.0.2) → MEDIUM PRIORITY  → OVS Queue 1 (3 Mbps)\n")
    info("  h4 (10.0.0.4) → BEST-EFFORT      → OVS Queue 2 (1 Mbps)\n")
    info("  h3 (10.0.0.3) → DESTINATION SERVER\n")
    info("  Bottleneck link: s1 ←→ s2 @ 10 Mbps / 10 ms\n")
    info("  Controller: RemoteController @ 127.0.0.1:6653\n")
    info("=" * 58 + "\n\n")

    # ── Basic connectivity test ───────────────────────────────────────────────
    # Fix 5: Block until all switches complete the OpenFlow handshake with the
    # Ryu controller (OFPT_FEATURES_REPLY exchanged).  Without this barrier,
    # pingAll races against QoS flow installation and the first packets are
    # silently dropped because no forwarding rules exist yet.
    info(">>> Waiting for switches to connect to controller …\n")
    net.waitConnected()
    info(">>> Running pingAll to verify connectivity …\n")
    net.pingAll()

    # ── Background listener for Dashboard 1-Click Start/Stop buttons ──────────
    import os
    import threading

    CMD_FILE = "/tmp/qos_cmd.txt"
    if os.path.exists(CMD_FILE):
        try:
            os.remove(CMD_FILE)
        except Exception:
            pass

    def _dashboard_cmd_poller():
        while True:
            try:
                if os.path.exists(CMD_FILE):
                    with open(CMD_FILE, "r") as f:
                        cmd = f.read().strip()
                    if cmd:
                        try:
                            os.remove(CMD_FILE)
                        except Exception:
                            pass
                        if cmd == "START":
                            info("\n[DASHBOARD TRIGGER] ▶ Start Traffic received from Web UI!\n")
                            start_qos_traffic(net, "40")
                        elif cmd.startswith("{"):
                            import json
                            try:
                                cfg = json.loads(cmd)
                                info("\n[DASHBOARD TRIGGER] ▶ Custom Experiment received from Web UI!\n")
                                start_qos_traffic(net, cfg)
                            except Exception as e:
                                info(f"\n[DASHBOARD ERROR] Failed to parse custom config: {e}\n")
                        elif cmd == "STOP":
                            info("\n[DASHBOARD TRIGGER] ⏹ Stop Traffic received from Web UI!\n")
                            stop_qos_traffic(net)
            except Exception:
                pass
            time.sleep(0.5)

    poller_thread = threading.Thread(target=_dashboard_cmd_poller, daemon=True)
    poller_thread.start()

    # ── Drop to Mininet CLI with built-in QoS demo commands ───────────────────
    info("\n" + "=" * 58 + "\n")
    info("  READY! Click 'Start Traffic' on http://localhost:8080 OR\n")
    info("  type 'traffic' in the mininet prompt below!\n")
    info("=" * 58 + "\n\n")
    QoSCLI(net)

    # ── Teardown ──────────────────────────────────────────────────────────────
    net.stop()


if __name__ == '__main__':
    setLogLevel('info')
    create_network()