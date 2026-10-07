"""
qos_controller.py
=================
Production-grade Ryu OpenFlow 1.3 SDN Controller for QoS Traffic Management.

Topology Context
----------------
  h1 (10.0.0.1) ─┐
  h2 (10.0.0.2) ─┤── s1 ══(10 Mbps / 10 ms)══ s2 ── h3 (10.0.0.3)
  h4 (10.0.0.4) ─┘

QoS Priority Matrix (mapped to OVS HTB queues on s1's egress port to s2)
--------------------------------------------------------------------------
  Host │ IP          │ Queue │ Min-Rate   │ Priority
  ─────┼─────────────┼───────┼────────────┼──────────
  h1   │ 10.0.0.1   │  0    │  6 Mbps   │ High
  h2   │ 10.0.0.2   │  1    │  3 Mbps   │ Medium
  h4   │ 10.0.0.4   │  2    │  1 Mbps   │ Best-Effort

Usage
-----
  ryu-manager qos_controller.py --ofp-tcp-listen-port 6653
"""

import re
import subprocess
import logging

from ryu.base import app_manager
from ryu.controller import ofp_event
from ryu.controller.handler import CONFIG_DISPATCHER, MAIN_DISPATCHER, set_ev_cls
from ryu.ofproto import ofproto_v1_3
from ryu.lib.packet import packet, ethernet, arp, ipv4
from ryu.lib.packet import ether_types


# ──────────────────────────────────────────────────────────────────────────────
# Constants
# ──────────────────────────────────────────────────────────────────────────────

# DPID of s1 (the QoS-enforcing switch).  Mininet assigns s1 → dpid=1.
QOS_SWITCH_DPID = 1

# OVS interface name on s1 that faces s2.
# Mininet names inter-switch links as "s1-eth<N>"; we discover the exact port
# number dynamically when the switch connects (see _install_qos_queues()).
# The variable below holds the discovered OVS port name.
S1_S2_PORT_NAME: str = ""

# HTB total bandwidth on the s1→s2 link (bits per second)
LINK_BW_BPS = 10_000_000  # 10 Mbps

# Queue configuration: (queue_id, min_rate_bps, label)
QUEUE_CONFIG = [
    (0, 6_000_000, "High-Priority   (h1 / 10.0.0.1)"),
    (1, 3_000_000, "Medium-Priority (h2 / 10.0.0.2)"),
    (2, 1_000_000, "Best-Effort     (h4 / 10.0.0.4)"),
]

# IP → Queue mapping used when installing proactive flows
IP_QUEUE_MAP = {
    "10.0.0.1": 0,  # h1 → Queue 0 (High)
    "10.0.0.2": 1,  # h2 → Queue 1 (Medium)
    "10.0.0.4": 2,  # h4 → Queue 2 (Best-Effort)
}

# OpenFlow flow priorities
PRIORITY_QOS_FLOW    = 200   # IP QoS classification rules
PRIORITY_ARP         = 100   # ARP reactive forwarding
PRIORITY_L2_LEARNED  = 50    # Reactively learned L2 entries
PRIORITY_TABLE_MISS  = 0     # Catch-all → send to controller


# ──────────────────────────────────────────────────────────────────────────────
# Helper: run a shell command and log result
# ──────────────────────────────────────────────────────────────────────────────

def _run(cmd: str, logger: logging.Logger) -> str:
    """Execute *cmd* in a subprocess shell; return stdout. Raises on failure."""
    logger.debug("CMD: %s", cmd)
    result = subprocess.run(
        cmd, shell=True, capture_output=True, text=True, timeout=10
    )
    if result.returncode != 0:
        logger.error("CMD failed [rc=%d]: %s\nSTDERR: %s",
                     result.returncode, cmd, result.stderr.strip())
        raise RuntimeError(f"Command failed: {cmd}")
    return result.stdout.strip()


# ──────────────────────────────────────────────────────────────────────────────
# Ryu Application
# ──────────────────────────────────────────────────────────────────────────────

class QoSController(app_manager.RyuApp):
    """
    OpenFlow 1.3 QoS controller combining:
      • Automatic OVS HTB queue provisioning (s1 only)
      • Proactive IP-based QoS flow classification
      • Reactive L2 learning switch (handles ARP + unicast forwarding)
    """

    OFP_VERSIONS = [ofproto_v1_3.OFP_VERSION]

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.logger.setLevel(logging.INFO)

        # mac_to_port[dpid][mac] = port_number
        self.mac_to_port: dict[int, dict[str, int]] = {}

        # port_to_name[dpid][port_no] = interface name (populated from PortDesc)
        self.port_to_name: dict[int, dict[int, str]] = {}

        # Track which switches have already had QoS queues installed
        self._qos_installed: set[int] = set()

        self.logger.info("=" * 60)
        self.logger.info("  QoS SDN Controller starting (OpenFlow 1.3)")
        self.logger.info("=" * 60)

    # ──────────────────────────────────────────────────────────────
    # 1. Switch Handshake – Features Reply
    # ──────────────────────────────────────────────────────────────

    @set_ev_cls(ofp_event.EventOFPSwitchFeatures, CONFIG_DISPATCHER)
    def _switch_features_handler(self, ev):
        """
        Triggered once per switch connection.
        • Installs a table-miss flow (packet-in everything initially).
        • Requests port description to resolve port names.
        """
        datapath = ev.msg.datapath
        dpid     = datapath.id
        ofproto  = datapath.ofproto
        parser   = datapath.ofproto_parser

        self.logger.info("[DPID %016x] Connected", dpid)
        self.mac_to_port.setdefault(dpid, {})
        self.port_to_name.setdefault(dpid, {})

        # ── Table-miss: send unmatched packets to the controller ──────────────
        match   = parser.OFPMatch()
        actions = [parser.OFPActionOutput(ofproto.OFPP_CONTROLLER,
                                          ofproto.OFPCML_NO_BUFFER)]
        self._add_flow(datapath, PRIORITY_TABLE_MISS, match, actions)
        self.logger.info("[DPID %016x] Table-miss flow installed", dpid)

        # ── Request port descriptions so we can find s1↔s2 port by name ─────
        req = parser.OFPPortDescStatsRequest(datapath, 0)
        datapath.send_msg(req)

    # ──────────────────────────────────────────────────────────────
    # 2. Port Description Reply → resolve interface names
    # ──────────────────────────────────────────────────────────────

    @set_ev_cls(ofp_event.EventOFPPortDescStatsReply, MAIN_DISPATCHER)
    def _port_desc_reply_handler(self, ev):
        """
        Store port_no → name mapping and, for s1, trigger QoS setup once we
        know which port connects to s2.
        """
        datapath = ev.msg.datapath
        dpid     = datapath.id

        self.port_to_name.setdefault(dpid, {})
        for port in ev.msg.body:
            if port.port_no >= 0xFFFFFF00:   # Skip pseudo-ports (LOCAL, etc.)
                continue
            self.port_to_name[dpid][port.port_no] = port.name.decode()
            self.logger.info("[DPID %016x] Port %d → %s",
                             dpid, port.port_no, port.name.decode())

        # Trigger QoS provisioning on s1 after we have all port names
        if dpid == QOS_SWITCH_DPID and dpid not in self._qos_installed:
            self._install_qos_queues(datapath)

    # ──────────────────────────────────────────────────────────────
    # 3. OVS HTB Queue Provisioning (s1 only)
    # ──────────────────────────────────────────────────────────────

    def _find_interswitch_port(self, dpid: int) -> tuple[int, str] | None:
        """
        Identify the port on s1 that connects to s2 using two strategies,
        applied in priority order:

        Strategy A — OVS peer query (authoritative):
            For each candidate "s<N>-eth<M>" port, call
            ``ovs-vsctl get interface <name> options:peer`` to check whether
            OVS knows the peer bridge. If it resolves to another bridge name
            (e.g. "s2") the port is the inter-switch egress port.

        Strategy B — Highest port number fallback:
            If OVS returns no peer information (e.g. veth-pair style links),
            fall back to max(port_no) — the original heuristic — but now
            explicitly documented as a last resort only.

        Returns:
            (port_no, port_name) tuple, or None if no ports are known.
        """
        ports = {
            pno: name
            for pno, name in self.port_to_name.get(dpid, {}).items()
        }
        if not ports:
            return None

        # Filter to ports whose name looks like "s<digit>-eth<digit>"
        # (excludes host-facing "h<N>-eth1" and LOCAL pseudo-ports)
        switch_facing = {
            pno: name for pno, name in ports.items()
            if re.match(r's\d+-eth\d+', name)
        }
        candidates = switch_facing if switch_facing else ports

        # ── Strategy A: ask OVS for the peer bridge of each candidate port ────
        for pno, name in sorted(candidates.items()):
            try:
                peer = _run(
                    f"ovs-vsctl get interface {name} options:peer",
                    self.logger
                ).strip().strip('"')
                if peer and peer != "[]":
                    # Verify the peer is actually an OVS bridge (switch)
                    bridges = _run("ovs-vsctl list-br", self.logger).split()
                    if peer in bridges:
                        self.logger.info(
                            "[PORT] Strategy A: port %d (%s) → peer bridge '%s'",
                            pno, name, peer
                        )
                        return pno, name
            except RuntimeError:
                pass   # ovs-vsctl not available or interface not found; try next

        # ── Strategy B: highest port number among switch-facing candidates ────
        pno = max(candidates.keys())
        name = candidates[pno]
        self.logger.warning(
            "[PORT] Strategy A yielded no peer — falling back to highest "
            "port number: port %d (%s)", pno, name
        )
        return pno, name

    def _install_qos_queues(self, datapath):
        """
        Programmatically configure Linux HTB QoS on the s1↔s2 egress port
        using ovs-vsctl, then install proactive OpenFlow classification flows.

        OVS QoS model used:
          QoS (type=linux-htb, max-rate=10 Mbps)
            └─ Queue 0  min-rate=6 Mbps   (High)
            └─ Queue 1  min-rate=3 Mbps   (Medium)
            └─ Queue 2  min-rate=1 Mbps   (Best-Effort)
        """
        dpid = datapath.id

        # ── Identify the inter-switch egress port (Fix 1) ─────────────────────
        result = self._find_interswitch_port(dpid)
        if result is None:
            self.logger.error("[DPID %016x] No ports found – aborting QoS setup", dpid)
            return

        s1_s2_port_no, s1_s2_port_name = result
        self.logger.info("[DPID %016x] QoS egress port → port %d (%s)",
                         dpid, s1_s2_port_no, s1_s2_port_name)

        # Store globally for reference
        global S1_S2_PORT_NAME
        S1_S2_PORT_NAME = s1_s2_port_name

        # ── Build ovs-vsctl commands ──────────────────────────────────────────
        try:
            # Fix 2: use --if-exists so the clear is safe even when no prior
            # QoS config exists on this port (avoids non-zero exit on fresh OVS)
            self.logger.info("[QoS] Clearing any existing QoS on %s …", s1_s2_port_name)
            _run(f"ovs-vsctl --if-exists clear port {s1_s2_port_name} qos", self.logger)

            self.logger.info("[QoS] Creating HTB QoS object …")
            # Create the QoS object with 3 queues in one atomic command
            qos_cmd = (
                f"ovs-vsctl set port {s1_s2_port_name} "
                f"qos=@qos1 "
                f"-- --id=@qos1 create qos type=linux-htb "
                f"other-config:max-rate={LINK_BW_BPS} "
                f"queues=0=@q0,1=@q1,2=@q2 "
                f"-- --id=@q0 create queue other-config:min-rate=6000000 "
                f"-- --id=@q1 create queue other-config:min-rate=3000000 "
                f"-- --id=@q2 create queue other-config:min-rate=1000000"
            )
            uuids = _run(qos_cmd, self.logger)
            self.logger.info("[QoS] HTB QoS object created. UUIDs:\n%s", uuids)

            for qid, min_rate, label in QUEUE_CONFIG:
                self.logger.info(
                    "[QoS] Queue %d configured → min-rate=%d bps  [%s]",
                    qid, min_rate, label
                )

        except RuntimeError as exc:
            self.logger.error("[QoS] Queue setup FAILED: %s", exc)
            self.logger.warning("[QoS] Continuing without hardware QoS queues.")

        # ── Install proactive IP classification flows (+ DSCP + reverse) ──────
        self._install_qos_flows(datapath, s1_s2_port_no)
        self._qos_installed.add(dpid)

    # ──────────────────────────────────────────────────────────────
    # 4. Proactive QoS Flow Installation
    # ──────────────────────────────────────────────────────────────

    def _install_qos_flows(self, datapath, s1_s2_port_no: int):
        """
        Install proactive OpenFlow rules on s1 covering:

        Forward path (hX → h3):
          Rule A — Source-IP match (always installed):
            Matches packets whose IPv4 source is the host's address.
            Direct, unambiguous classification regardless of DSCP marking.
          Rule B — DSCP / DiffServ match (Fix 3, additionally installed):
            Matches packets by the IP ToS DSCP field so that any endpoint
            that already marks its traffic with a standard DSCP value
            (e.g. EF=46, AF31=26, BE=0) is also classified correctly,
            without relying on hardcoded IPs.

            DSCP values used per RFC 4594 / RFC 2474:
              Queue 0 (High)       → ip_dscp=46  (EF — Expedited Forwarding)
              Queue 1 (Medium)     → ip_dscp=26  (AF31 — Assured Forwarding)
              Queue 2 (Best-Effort)→ ip_dscp=0   (BE — Default / Best-Effort)

        Reverse path (h3 → hX, Fix 4):
          Explicit return flows eliminate the initial packet delay that
          occurs when reactive L2 learning handles the first reply from h3.
          These flows run at PRIORITY_QOS_FLOW - 1 so forward QoS rules
          always win when traffic originates at h3.
        """
        ofproto = datapath.ofproto
        parser  = datapath.ofproto_parser

        # DSCP values per RFC 4594:
        #   EF=46 (101110b), AF31=26 (011010b), BE=0 (000000b)
        DSCP_MAP = {
            0: 46,   # Queue 0 (High)       ↔ EF
            1: 26,   # Queue 1 (Medium)     ↔ AF31
            2:  0,   # Queue 2 (Best-Effort)↔ BE
        }

        # Host-port table for reverse flows (port on s1 facing each source host)
        # Keyed by destination IP (= source host IP for return traffic from h3)
        # We derive this from the in-port field already stored in mac_to_port
        # by looking up the MAC that Mininet assigns to each host IP.
        # autoSetMacs=True makes MACs deterministic: 10.0.0.X → 00:00:00:00:00:0X
        host_mac_to_ip = {
            "00:00:00:00:00:01": "10.0.0.1",
            "00:00:00:00:00:02": "10.0.0.2",
            "00:00:00:00:00:04": "10.0.0.4",
        }

        for src_ip, queue_id in IP_QUEUE_MAP.items():
            # ── Rule A: Source-IP classification ─────────────────────────────
            fwd_match_ip = parser.OFPMatch(
                eth_type=ether_types.ETH_TYPE_IP,
                ipv4_src=src_ip,
                ipv4_dst="10.0.0.3",
            )
            fwd_actions = [
                parser.OFPActionSetQueue(queue_id),
                parser.OFPActionOutput(s1_s2_port_no),
            ]
            self._add_flow(datapath, PRIORITY_QOS_FLOW, fwd_match_ip, fwd_actions)
            self.logger.info(
                "[FLOW] Forward IP:  src=%s → Queue %d → port %d",
                src_ip, queue_id, s1_s2_port_no
            )

            # ── Rule B: DSCP / DiffServ classification (Fix 3) ───────────────
            dscp_val = DSCP_MAP[queue_id]
            if dscp_val > 0:   # skip DSCP=0 (matches everything, too broad)
                fwd_match_dscp = parser.OFPMatch(
                    eth_type=ether_types.ETH_TYPE_IP,
                    ip_dscp=dscp_val,
                    ipv4_dst="10.0.0.3",
                )
                # Lower priority than the IP-based rule so src-IP wins on
                # hosts where both criteria match (prevents double-counting)
                self._add_flow(datapath, PRIORITY_QOS_FLOW - 1,
                               fwd_match_dscp, fwd_actions)
                self.logger.info(
                    "[FLOW] Forward DSCP: dscp=%d → Queue %d → port %d",
                    dscp_val, queue_id, s1_s2_port_no
                )

        # ── Reverse flows: h3 → hX (Fix 4) ──────────────────────────────────
        # Derive the per-host s1 port from the mac_to_port table if available
        # (populated by the first ARP exchange), otherwise skip gracefully.
        mac_table = self.mac_to_port.get(datapath.id, {})
        ip_to_host_port: dict[str, int] = {}
        for mac, port in mac_table.items():
            ip = host_mac_to_ip.get(mac)
            if ip:
                ip_to_host_port[ip] = port

        for src_ip, queue_id in IP_QUEUE_MAP.items():
            if src_ip not in ip_to_host_port:
                self.logger.info(
                    "[FLOW] Reverse: MAC for %s not yet learned — "
                    "reactive L2 will handle first return packet", src_ip
                )
                continue

            host_port = ip_to_host_port[src_ip]
            rev_match = parser.OFPMatch(
                eth_type=ether_types.ETH_TYPE_IP,
                ipv4_src="10.0.0.3",
                ipv4_dst=src_ip,
            )
            # Return path needs no queue manipulation — congestion is on s1→s2,
            # not s2→s1. Just forward out the correct host-facing port.
            rev_actions = [parser.OFPActionOutput(host_port)]
            self._add_flow(datapath, PRIORITY_QOS_FLOW - 1, rev_match, rev_actions)
            self.logger.info(
                "[FLOW] Reverse:    h3 → %s → port %d (no queue needed)",
                src_ip, host_port
            )


    # ──────────────────────────────────────────────────────────────
    # 5. Packet-In Handler (Reactive L2 Learning Switch)
    # ──────────────────────────────────────────────────────────────

    @set_ev_cls(ofp_event.EventOFPPacketIn, MAIN_DISPATCHER)
    def _packet_in_handler(self, ev):
        """
        Reactive learning switch logic that handles:
          • ARP broadcasts (learn MAC, flood or unicast reply)
          • Unknown unicast (flood)
          • Known unicast (forward + install L2 flow)

        QoS flows (IP traffic h1/h2/h4 → h3) are handled proactively and
        therefore should NOT reach here once the proactive rules are in place.
        """
        msg      = ev.msg
        datapath = msg.datapath
        dpid     = datapath.id
        ofproto  = datapath.ofproto
        parser   = datapath.ofproto_parser
        in_port  = msg.match["in_port"]

        pkt     = packet.Packet(msg.data)
        eth_pkt = pkt.get_protocol(ethernet.ethernet)
        if eth_pkt is None:
            return

        dst_mac = eth_pkt.dst
        src_mac = eth_pkt.src
        ethertype = eth_pkt.ethertype

        # ── Learn source MAC ──────────────────────────────────────────────────
        self.mac_to_port.setdefault(dpid, {})
        if self.mac_to_port[dpid].get(src_mac) != in_port:
            self.mac_to_port[dpid][src_mac] = in_port
            self.logger.info(
                "[L2] DPID %016x  learned  %s → port %d",
                dpid, src_mac, in_port
            )

        # ── Determine output port ─────────────────────────────────────────────
        if dst_mac in self.mac_to_port[dpid]:
            out_port = self.mac_to_port[dpid][dst_mac]
        else:
            out_port = ofproto.OFPP_FLOOD

        actions = [parser.OFPActionOutput(out_port)]

        # ── Install a unicast L2 flow to avoid future packet-ins ─────────────
        if out_port != ofproto.OFPP_FLOOD:
            match = parser.OFPMatch(
                in_port=in_port,
                eth_dst=dst_mac,
                eth_src=src_mac,
            )
            # Only install if we have a buffer or actual packet
            if msg.buffer_id != ofproto.OFP_NO_BUFFER:
                self._add_flow(datapath, PRIORITY_L2_LEARNED, match, actions,
                               buffer_id=msg.buffer_id)
                return  # OVS will handle packet from buffer; no need to output
            else:
                self._add_flow(datapath, PRIORITY_L2_LEARNED, match, actions)

        # ── Send the current packet out ───────────────────────────────────────
        data = None
        if msg.buffer_id == ofproto.OFP_NO_BUFFER:
            data = msg.data

        out = parser.OFPPacketOut(
            datapath=datapath,
            buffer_id=msg.buffer_id,
            in_port=in_port,
            actions=actions,
            data=data,
        )
        datapath.send_msg(out)

        # ── Log interesting traffic ───────────────────────────────────────────
        if ethertype == ether_types.ETH_TYPE_ARP:
            arp_pkt = pkt.get_protocol(arp.arp)
            if arp_pkt:
                self.logger.info(
                    "[ARP] DPID %016x  %s → %s  (who-has %s)",
                    dpid, src_mac, dst_mac, arp_pkt.dst_ip
                )
        elif ethertype == ether_types.ETH_TYPE_IP:
            ip_pkt = pkt.get_protocol(ipv4.ipv4)
            if ip_pkt:
                self.logger.info(
                    "[IP ] DPID %016x  %s→%s  port %d→%s",
                    dpid, ip_pkt.src, ip_pkt.dst, in_port,
                    str(out_port) if out_port != ofproto.OFPP_FLOOD else "FLOOD"
                )

    # ──────────────────────────────────────────────────────────────
    # 6. Utility: Add Flow Entry
    # ──────────────────────────────────────────────────────────────

    def _add_flow(self, datapath, priority: int, match,
                  actions: list, buffer_id=None, idle_timeout=0, hard_timeout=0):
        """
        Helper to build and send an OFPFlowMod message.

        Args:
            datapath:     Target switch datapath object.
            priority:     Flow entry priority (higher = evaluated first).
            match:        OFPMatch object defining traffic selector.
            actions:      List of OFPAction* objects to apply.
            buffer_id:    Optional buffered packet ID.
            idle_timeout: Seconds of inactivity before removal (0 = permanent).
            hard_timeout: Absolute lifetime in seconds (0 = permanent).
        """
        ofproto = datapath.ofproto
        parser  = datapath.ofproto_parser

        inst = [
            parser.OFPInstructionActions(ofproto.OFPIT_APPLY_ACTIONS, actions)
        ]

        kwargs = dict(
            datapath=datapath,
            priority=priority,
            match=match,
            instructions=inst,
            idle_timeout=idle_timeout,
            hard_timeout=hard_timeout,
        )
        if buffer_id is not None and buffer_id != ofproto.OFP_NO_BUFFER:
            kwargs["buffer_id"] = buffer_id

        mod = parser.OFPFlowMod(**kwargs)
        datapath.send_msg(mod)
