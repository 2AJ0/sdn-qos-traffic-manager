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


class QoSCLI(CLI):
    """Custom Mininet CLI with built-in commands for the QoS demonstration."""

    def do_traffic(self, line):
        """Run simultaneous multi-class UDP QoS traffic across h1, h2, h4 to h3."""
        h1, h2, h3, h4 = self.mn.get('h1', 'h2', 'h3', 'h4')
        info("\n*** Stopping any existing traffic...\n")
        for h in [h1, h2, h3, h4]:
            h.cmd('pkill -9 -f udp_server.py')
            h.cmd('pkill -9 -f udp_client.py')

        info("*** Starting low-level UDP Server on h3 (listening on 5001, 5002, 5003)...\n")
        h3.cmd('python3 udp_server.py &')
        time.sleep(1.5)

        duration = line.strip() if line.strip().isdigit() else "40"
        info(f"*** Launching simultaneous UDP senders (Duration: {duration}s)...\n")
        info("    • h1: High Priority   (VoIP)  -> 10 Mbps target -> Queue 0 (6 Mbps)\n")
        info("    • h2: Medium Priority (Video) -> 10 Mbps target -> Queue 1 (3 Mbps)\n")
        info("    • h4: Best Effort     (Bulk)  -> 10 Mbps target -> Queue 2 (1 Mbps)\n")

        h1.cmd(f'python3 udp_client.py --server 10.0.0.3 --port 5001 --class-type high --rate 10M --time {duration} &')
        h2.cmd(f'python3 udp_client.py --server 10.0.0.3 --port 5002 --class-type medium --rate 10M --time {duration} &')
        h4.cmd(f'python3 udp_client.py --server 10.0.0.3 --port 5003 --class-type best_effort --rate 10M --time {duration} &')

        info(f"*** Traffic started! Open http://localhost:8080 to watch the live QoS enforcement! ***\n\n")

    def do_stop_traffic(self, line):
        """Stop all running UDP client and server processes."""
        for h in self.mn.get('h1', 'h2', 'h3', 'h4'):
            h.cmd('pkill -9 -f udp_server.py')
            h.cmd('pkill -9 -f udp_client.py')
        info("*** All UDP traffic generators and servers stopped.\n")


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

    # ── Drop to Mininet CLI with built-in QoS demo commands ───────────────────
    info("\n" + "=" * 58 + "\n")
    info("  READY! Type 'traffic' in the mininet prompt to start\n")
    info("  the simultaneous multi-flow QoS experiment!\n")
    info("=" * 58 + "\n\n")
    QoSCLI(net)

    # ── Teardown ──────────────────────────────────────────────────────────────
    net.stop()


if __name__ == '__main__':
    setLogLevel('info')
    create_network()