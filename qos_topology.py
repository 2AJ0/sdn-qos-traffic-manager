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
import os
import subprocess
import time


# OVS queue min-rates for each phase (in bps)
_PHASE1_RATES = {0: 6_000_000, 1: 3_000_000, 2: 1_000_000}   # h1 High, h2 Med, h4 Low
_PHASE2_RATES = {0: 1_000_000, 1: 3_000_000, 2: 6_000_000}   # h1 Demoted, h2 unchanged, h4 Promoted

# How long after traffic starts before the dynamic queue swap fires (seconds).
# Change this value to adjust when the swap happens.
SWAP_DELAY_SECS: int = 20


def _run_ovs(cmd: str) -> str:
    """Run an ovs-vsctl command and return stdout (empty string on failure)."""
    try:
        return subprocess.check_output(cmd, shell=True, stderr=subprocess.DEVNULL).decode().strip()
    except Exception:
        return ""


def _apply_queue_rates(rates: dict):
    """
    Apply a dict of {queue_id: min_rate_bps} to the OVS HTB queues on s1's
    egress port toward s2.  Looks up the port and queue UUIDs dynamically so
    this works even if the controller re-created them.
    """
    try:
        # Find s1's egress port toward s2 (the inter-switch link)
        port_name = _run_ovs("ovs-vsctl list-ports s1")
        # Pick the port that is NOT connected to a host (eth1-eth3 are hosts, eth4 is s2)
        ports = [p for p in port_name.splitlines() if p.startswith("s1-eth")]
        # The highest-numbered port is typically the inter-switch link
        egress = sorted(ports)[-1] if ports else ""
        if not egress:
            return
        qos_uuid = _run_ovs(f"ovs-vsctl get port {egress} qos").strip("[]").strip()
        if not qos_uuid or qos_uuid == "[]":
            return
        for qid, rate_bps in rates.items():
            q_uuid = _run_ovs(f"ovs-vsctl get qos {qos_uuid} queues:{qid}").strip()
            if q_uuid:
                _run_ovs(f"ovs-vsctl set queue {q_uuid} other-config:min-rate={rate_bps}")
    except Exception:
        pass


def append_system_log(msg: str):
    """Appends timestamped event string to shared logs file for dashboard."""
    try:
        ts = time.strftime("%H:%M:%S")
        with open("/tmp/qos_logs.txt", "a") as f:
            f.write(f"[{ts}] {msg}\n")
    except Exception:
        pass


CURRENT_EXPERIMENT_TIMER = None


def start_qos_traffic(net, config="40"):
    """Starts UDP server on h3 and simultaneous clients on selected hosts with custom rates."""
    global CURRENT_EXPERIMENT_TIMER
    import json
    import threading
    import urllib.request
    h1, h2, h3, h4 = net.get('h1', 'h2', 'h3', 'h4')

    # Cancel any existing timer
    if CURRENT_EXPERIMENT_TIMER:
        try:
            CURRENT_EXPERIMENT_TIMER.cancel()
        except Exception:
            pass
        CURRENT_EXPERIMENT_TIMER = None

    info("\n*** Stopping any existing traffic...\n")
    for h in [h1, h2, h3, h4]:
        h.cmd('pkill -9 -f udp_server.py')
        h.cmd('pkill -9 -f udp_client.py')

    # ── Signal controller to reset Phase 1 queues & arm fresh swap timer ──────
    # Primary: call the Ryu REST API so the controller owns the swap greenthread
    # (cancels any previous greenthread, resets queues, arms a fresh timer).
    # Fallback: apply queue rates locally if the REST API is unreachable.
    info("*** Signalling controller to reset Phase 1 & arm swap timer...\n")
    _ctrl_notified = False
    try:
        payload = json.dumps({"swap_delay": SWAP_DELAY_SECS}).encode()
        req = urllib.request.Request(
            "http://127.0.0.1:8081/qos/experiment/start",
            data=payload,
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        with urllib.request.urlopen(req, timeout=2) as resp:
            result = json.loads(resp.read().decode())
            info(f"[CTRL] Controller acknowledged: {result}\n")
            _ctrl_notified = True
    except Exception as e:
        info(f"[CTRL] REST call failed ({e}) — applying local Phase 1 reset as fallback\n")

    if not _ctrl_notified:
        # Fallback: topology resets queues and arms its own swap timer
        _apply_queue_rates(_PHASE1_RATES)
        append_system_log("OVS HTB queues reset to Phase 1 (h1=6Mbps, h2=3Mbps, h4=1Mbps)")

    try:
        with open("/tmp/qos_logs.txt", "w") as f:
            f.truncate(0)
    except Exception:
        pass

    info("*** Starting low-level UDP Server on h3 (listening on 5001, 5002, 5003)...\n")
    h3.cmd('python3 udp_server.py &')
    time.sleep(1.2)

    dur = 40
    h1_cfg = {"enabled": True, "rate": 10}
    h2_cfg = {"enabled": True, "rate": 10}
    h4_cfg = {"enabled": True, "rate": 10}

    if isinstance(config, dict):
        dur = int(config.get("duration", 40))
        h1_cfg = config.get("h1", h1_cfg)
        h2_cfg = config.get("h2", h2_cfg)
        h4_cfg = config.get("h4", h4_cfg)
    elif isinstance(config, (str, int)) and str(config).strip().isdigit():
        dur = int(str(config).strip())

    info(f"*** Launching UDP senders for EXACTLY {dur} seconds (swap at t={SWAP_DELAY_SECS}s)...\n")
    active_hosts = []

    if h1_cfg.get("enabled", True):
        r1 = h1_cfg.get("rate", 10)
        info(f"    • h1: High Priority (VoIP)  -> {r1} Mbps target -> Queue 0 (6 Mbps)\n")
        h1.cmd(f'python3 udp_client.py --server 10.0.0.3 --port 5001 --class-type high --rate {r1}M --time {dur} &')
        active_hosts.append(f"h1 ({r1}M)")

    if h2_cfg.get("enabled", True):
        r2 = h2_cfg.get("rate", 10)
        info(f"    • h2: Med Priority  (Video) -> {r2} Mbps target -> Queue 1 (3 Mbps)\n")
        h2.cmd(f'python3 udp_client.py --server 10.0.0.3 --port 5002 --class-type medium --rate {r2}M --time {dur} &')
        active_hosts.append(f"h2 ({r2}M)")

    if h4_cfg.get("enabled", True):
        r4 = h4_cfg.get("rate", 10)
        info(f"    • h4: Best Effort   (Bulk)  -> {r4} Mbps target -> Queue 2 (1 Mbps)\n")
        h4.cmd(f'python3 udp_client.py --server 10.0.0.3 --port 5003 --class-type best_effort --rate {r4}M --time {dur} &')
        active_hosts.append(f"h4 ({r4}M)")

    hosts_str = ", ".join(active_hosts) if active_hosts else "None"
    append_system_log(f"Mininet launched experiment: [{hosts_str}] for {dur}s")

    # ── Dynamic policy swap timer — LOCAL FALLBACK only ───────────────────────
    # When the controller REST call succeeded, the controller owns the swap
    # greenthread.  We only arm a local timer here as a fallback when the REST
    # call failed (_ctrl_notified is False from the outer scope via closure).
    def _do_swap():
        info(f"\n[DYN] Dynamic swap triggered at t={SWAP_DELAY_SECS}s — applying Phase 2 rates...\n")
        _apply_queue_rates(_PHASE2_RATES)
        swap_msg = (f"SDN POLICY SWAP: Queue 0 demoted (6M -> 1M), "
                    f"Queue 2 promoted (1M -> 6M)")
        append_system_log(swap_msg)
        info(f"[DYN] {swap_msg}\n")

    # ── Auto-stop timer (fires dur seconds after start) ───────────────────────
    def _auto_stop_task():
        info(f"\n[TIMER] Experiment duration ({dur}s) reached! Force-stopping traffic...\n")
        stop_qos_traffic(net)
        append_system_log(f"Experiment duration ({dur}s) expired. Traffic stopped.")

    # Arm local swap timer only when the controller REST call failed
    if not _ctrl_notified and SWAP_DELAY_SECS < dur:
        swap_timer = threading.Timer(SWAP_DELAY_SECS, _do_swap)
        swap_timer.daemon = True
        swap_timer.start()
        info(f"[CTRL] Local fallback swap timer armed (fires in {SWAP_DELAY_SECS}s)\n")

    CURRENT_EXPERIMENT_TIMER = threading.Timer(dur + 0.5, _auto_stop_task)
    CURRENT_EXPERIMENT_TIMER.daemon = True
    CURRENT_EXPERIMENT_TIMER.start()

    info(f"*** Traffic started! Swap fires at t={SWAP_DELAY_SECS}s. "
         f"Open http://localhost:8080 to watch! ***\n\n")


def stop_qos_traffic(net):
    """Stops all running UDP clients and servers, clearing telemetry."""
    global CURRENT_EXPERIMENT_TIMER
    import json
    if CURRENT_EXPERIMENT_TIMER:
        try:
            CURRENT_EXPERIMENT_TIMER.cancel()
        except Exception:
            pass
        CURRENT_EXPERIMENT_TIMER = None

    for h in net.get('h1', 'h2', 'h3', 'h4'):
        try:
            h.cmd('pkill -9 -f udp_server.py')
            h.cmd('pkill -9 -f udp_client.py')
        except Exception:
            pass

    # Ensure no zombie processes on host level
    try:
        os.system("pkill -9 -f udp_client.py 2>/dev/null; pkill -9 -f udp_server.py 2>/dev/null")
    except Exception:
        pass

    zero_stats = {
        "timestamp": time.time(),
        "total_mbps": 0.0,
        "link_capacity_mbps": 10.0,
        "flows": [
            {"port": 5001, "class_name": "Host h1 (Queue 0)", "mbps": 0.0, "loss_pct": 0.0, "latency_ms": 0.0},
            {"port": 5002, "class_name": "Host h2 (Queue 1)", "mbps": 0.0, "loss_pct": 0.0, "latency_ms": 0.0},
            {"port": 5003, "class_name": "Host h4 (Queue 2)", "mbps": 0.0, "loss_pct": 0.0, "latency_ms": 0.0}
        ]
    }
    try:
        with open("/tmp/qos_stats.json", "w") as f:
            json.dump(zero_stats, f)
        try:
            os.chmod("/tmp/qos_stats.json", 0o666)
        except Exception:
            pass
    except Exception:
        pass

    append_system_log("All UDP traffic generators and servers stopped.")
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
    import json
    import threading
    import urllib.request

    CMD_FILE = "/tmp/qos_cmd.txt"
    if os.path.exists(CMD_FILE):
        try:
            os.remove(CMD_FILE)
        except Exception:
            pass

    def _dashboard_cmd_poller():
        while True:
            cmd = None
            # 1. Primary channel: query Dashboard in-memory queue via HTTP
            try:
                req = urllib.request.Request("http://127.0.0.1:8080/api/cmd/pending")
                with urllib.request.urlopen(req, timeout=0.8) as resp:
                    resp_data = json.loads(resp.read().decode('utf-8'))
                    if resp_data and resp_data.get("cmd"):
                        cmd = resp_data["cmd"]
            except Exception:
                pass

            # 2. Secondary fallback: check /tmp/qos_cmd.txt file
            if not cmd and os.path.exists(CMD_FILE):
                try:
                    with open(CMD_FILE, "r") as f:
                        cmd = f.read().strip()
                    try:
                        os.remove(CMD_FILE)
                    except Exception:
                        pass
                except Exception:
                    pass

            if cmd:
                if cmd == "START" or (isinstance(cmd, str) and cmd.strip().isdigit()):
                    dur = cmd.strip() if cmd.strip().isdigit() else "40"
                    info(f"\n[DASHBOARD TRIGGER] ▶ Start Traffic ({dur}s) received from Web UI!\n")
                    start_qos_traffic(net, dur)
                elif cmd == "STOP":
                    info("\n[DASHBOARD TRIGGER] ⏹ Stop Traffic received from Web UI!\n")
                    stop_qos_traffic(net)
                elif cmd.startswith("{"):
                    try:
                        cfg = json.loads(cmd)
                        info(f"\n[DASHBOARD TRIGGER] 🚀 Custom Experiment received from Studio!\n")
                        start_qos_traffic(net, cfg)
                    except Exception as e:
                        info(f"\n[DASHBOARD ERROR] Failed to parse custom config: {e}\n")

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