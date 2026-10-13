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

Dynamic Policy Swap (NEW)
--------------------------
  After DYNAMIC_SWAP_DELAY_SECS seconds of operation, the controller
  automatically swaps the min-rate of Queue 0 (h1) and Queue 2 (h4):
    Phase 1 (0–20 s) : Queue 0 = 6 Mbps (High),  Queue 2 = 1 Mbps (Low)
    Phase 2 (20 s +) : Queue 0 = 1 Mbps (Low),   Queue 2 = 6 Mbps (High)
  This swap is fully controller-driven via update_queue_rates(); no manual
  ovs-vsctl commands are needed and OpenFlow flows are NOT disturbed.

Usage
-----
  ryu-manager qos_controller.py --ofp-tcp-listen-port 6653
"""

from __future__ import annotations
import re
import subprocess
import logging
from typing import Dict, List, Optional, Set, Tuple

from ryu.base import app_manager
from ryu.controller import ofp_event
from ryu.controller.handler import CONFIG_DISPATCHER, MAIN_DISPATCHER, set_ev_cls
from ryu.lib import hub                          # Ryu's cooperative greenthread pool
from ryu.ofproto import ofproto_v1_3
from ryu.lib.packet import packet, ethernet, arp, ipv4
from ryu.lib.packet import ether_types


# ──────────────────────────────────────────────────────────────────────────────
# Constants
# ──────────────────────────────────────────────────────────────────────────────

# DPID of s1 (the QoS-enforcing switch).  Mininet assigns s1 → dpid=1.
QOS_SWITCH_DPID = 1

# OVS interface name on s1 that faces s2 (populated at runtime).
S1_S2_PORT_NAME: str = ""

# HTB total bandwidth on the s1→s2 link (bits per second)
LINK_BW_BPS = 10_000_000  # 10 Mbps

# ── Phase-1 queue configuration (initial / startup) ───────────────────────────
# Format: (queue_id, min_rate_bps, label)
QUEUE_CONFIG_PHASE1: List[Tuple[int, int, str]] = [
    (0, 6_000_000, "High-Priority   (h1 / 10.0.0.1)"),
    (1, 3_000_000, "Medium-Priority (h2 / 10.0.0.2)"),
    (2, 1_000_000, "Best-Effort     (h4 / 10.0.0.4)"),
]

# ── Phase-2 queue configuration (after dynamic swap) ──────────────────────────
# Queue 0 (h1) and Queue 2 (h4) swap their min-rates.  Queue 1 unchanged.
QUEUE_CONFIG_PHASE2: List[Tuple[int, int, str]] = [
    (0, 1_000_000, "Demoted         (h1 / 10.0.0.1)  ← was 6 Mbps"),
    (1, 3_000_000, "Medium-Priority (h2 / 10.0.0.2)  unchanged"),
    (2, 6_000_000, "Promoted        (h4 / 10.0.0.4)  ← was 1 Mbps"),
]

# Seconds after initial QoS setup before the dynamic policy swap fires.
DYNAMIC_SWAP_DELAY_SECS: int = 20

# IP → Queue mapping used when installing proactive flows (unchanged by swap;
# the OpenFlow queue *assignment* stays constant — only HTB min-rates change).
IP_QUEUE_MAP: Dict[str, int] = {
    "10.0.0.1": 0,  # h1 → Queue 0
    "10.0.0.2": 1,  # h2 → Queue 1
    "10.0.0.4": 2,  # h4 → Queue 2
}

# OpenFlow flow priorities
PRIORITY_QOS_FLOW   = 200   # IP QoS classification rules
PRIORITY_ARP        = 100   # ARP reactive forwarding
PRIORITY_L2_LEARNED = 50    # Reactively learned L2 entries
PRIORITY_TABLE_MISS = 0     # Catch-all → send to controller


# ──────────────────────────────────────────────────────────────────────────────
# Module-level helper: run a shell command
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
      • Proactive IP-based + DSCP QoS flow classification
      • Bidirectional explicit flows (forward + reverse)
      • Reactive L2 learning switch (ARP + unknown unicast)
      • Dynamic mid-run policy swap via ryu.lib.hub greenthread
    """

    OFP_VERSIONS = [ofproto_v1_3.OFP_VERSION]

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.logger.setLevel(logging.INFO)

        # mac_to_port[dpid][mac] = port_number
        self.mac_to_port: Dict[int, Dict[str, int]] = {}

        # port_to_name[dpid][port_no] = interface name (populated from PortDesc)
        self.port_to_name: Dict[int, Dict[int, str]] = {}

        # Tracks which switches have had QoS queues installed
        self._qos_installed: set = set()

        # Holds the OVS UUID of the QoS object on the egress port so that
        # update_queue_rates() can resolve queue UUIDs without re-querying.
        # _qos_uuid[port_name] = {"qos_uuid": str, "queue_uuids": {qid: str}}
        self._qos_meta: Dict[str, dict] = {}

        self.logger.info("=" * 60)
        self.logger.info("  QoS SDN Controller starting (OpenFlow 1.3)")
        self.logger.info("  Dynamic swap fires after %d s", DYNAMIC_SWAP_DELAY_SECS)
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
    # 3a. Inter-Switch Port Detection
    # ──────────────────────────────────────────────────────────────

    def _find_interswitch_port(self, dpid: int) -> Optional[Tuple[int, str]]:
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
            fall back to max(port_no) — documented explicitly as a last resort.

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
        pno  = max(candidates.keys())
        name = candidates[pno]
        self.logger.warning(
            "[PORT] Strategy A yielded no peer — falling back to highest "
            "port number: port %d (%s)", pno, name
        )
        return pno, name

    # ──────────────────────────────────────────────────────────────
    # 3b. OVS HTB Queue Provisioning (s1 only)
    # ──────────────────────────────────────────────────────────────

    def _install_qos_queues(self, datapath):
        """
        Programmatically configure Linux HTB QoS on the s1↔s2 egress port
        using ovs-vsctl, then install proactive OpenFlow classification flows,
        and finally spawn the dynamic-swap background timer.

        OVS QoS model used:
          QoS (type=linux-htb, max-rate=10 Mbps)
            └─ Queue 0  min-rate=6 Mbps   (High)
            └─ Queue 1  min-rate=3 Mbps   (Medium)
            └─ Queue 2  min-rate=1 Mbps   (Best-Effort)
        """
        dpid = datapath.id

        # ── Identify the inter-switch egress port ─────────────────────────────
        result = self._find_interswitch_port(dpid)
        if result is None:
            self.logger.error("[DPID %016x] No ports found – aborting QoS setup", dpid)
            return

        s1_s2_port_no, s1_s2_port_name = result
        self.logger.info("[DPID %016x] QoS egress port → port %d (%s)",
                         dpid, s1_s2_port_no, s1_s2_port_name)

        global S1_S2_PORT_NAME
        S1_S2_PORT_NAME = s1_s2_port_name

        # ── Build ovs-vsctl commands ──────────────────────────────────────────
        try:
            # --if-exists prevents non-zero exit when port has no prior QoS
            self.logger.info("[QoS] Clearing any existing QoS on %s …", s1_s2_port_name)
            _run(f"ovs-vsctl --if-exists clear port {s1_s2_port_name} qos", self.logger)

            self.logger.info("[QoS] Creating HTB QoS object (Phase 1) …")
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
            uuids_raw = _run(qos_cmd, self.logger)
            # ovs-vsctl prints one UUID per created object (qos, q0, q1, q2)
            uuids = uuids_raw.splitlines()
            self.logger.info("[QoS] HTB QoS created. UUIDs: %s", uuids)

            # Cache UUIDs so update_queue_rates() can target queues directly
            if len(uuids) >= 4:
                self._qos_meta[s1_s2_port_name] = {
                    "qos_uuid":    uuids[0],
                    "queue_uuids": {0: uuids[1], 1: uuids[2], 2: uuids[3]},
                }

            for qid, min_rate, label in QUEUE_CONFIG_PHASE1:
                self.logger.info(
                    "[QoS] Queue %d → min-rate=%d bps  [%s]",
                    qid, min_rate, label
                )

        except RuntimeError as exc:
            self.logger.error("[QoS] Queue setup FAILED: %s", exc)
            self.logger.warning("[QoS] Continuing without hardware QoS queues.")

        # ── Install proactive IP + DSCP + reverse flows ───────────────────────
        self._install_qos_flows(datapath, s1_s2_port_no)
        self._qos_installed.add(dpid)

        # ── Spawn dynamic-swap background timer (NEW) ─────────────────────────
        self.start_dynamic_swap_timer(datapath, delay_seconds=DYNAMIC_SWAP_DELAY_SECS)

    # ──────────────────────────────────────────────────────────────
    # 4. Proactive QoS Flow Installation
    # ──────────────────────────────────────────────────────────────

    def _install_qos_flows(self, datapath, s1_s2_port_no: int):
        """
        Install proactive OpenFlow rules on s1 covering:

        Forward path (hX → h3):
          Rule A — Source-IP match (always installed):
            Matches packets whose IPv4 source is the host's address.
          Rule B — DSCP / DiffServ match (additionally installed):
            Matches packets by the IP ToS DSCP field so that any endpoint
            that marks its traffic with a standard DSCP value is also
            classified correctly without relying on hardcoded IPs.

            DSCP values used per RFC 4594 / RFC 2474:
              Queue 0 (High)        → ip_dscp=46  (EF — Expedited Forwarding)
              Queue 1 (Medium)      → ip_dscp=26  (AF31 — Assured Forwarding)
              Queue 2 (Best-Effort) → ip_dscp=0   (BE — Default)

        Reverse path (h3 → hX):
          Explicit return flows eliminate the first-packet delay from reactive
          L2 learning. These run at PRIORITY_QOS_FLOW - 1.
        """
        ofproto = datapath.ofproto
        parser  = datapath.ofproto_parser

        # DSCP values per RFC 4594
        DSCP_MAP = {
            0: 46,   # Queue 0 (High)        ↔ EF
            1: 26,   # Queue 1 (Medium)      ↔ AF31
            2:  0,   # Queue 2 (Best-Effort) ↔ BE
        }

        # Mininet autoSetMacs=True → 10.0.0.X maps to 00:00:00:00:00:0X
        host_mac_to_ip: Dict[str, str] = {
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
                "[FLOW] Forward IP:   src=%s → Queue %d → port %d",
                src_ip, queue_id, s1_s2_port_no
            )

            # ── Rule B: DSCP / DiffServ classification ────────────────────────
            dscp_val = DSCP_MAP[queue_id]
            if dscp_val > 0:   # skip DSCP=0 (matches everything; too broad)
                fwd_match_dscp = parser.OFPMatch(
                    eth_type=ether_types.ETH_TYPE_IP,
                    ip_dscp=dscp_val,
                    ipv4_dst="10.0.0.3",
                )
                # Priority 199 so src-IP rule wins when both match
                self._add_flow(datapath, PRIORITY_QOS_FLOW - 1,
                               fwd_match_dscp, fwd_actions)
                self.logger.info(
                    "[FLOW] Forward DSCP: dscp=%d → Queue %d → port %d",
                    dscp_val, queue_id, s1_s2_port_no
                )

        # ── Reverse flows: h3 → hX ────────────────────────────────────────────
        mac_table = self.mac_to_port.get(datapath.id, {})
        ip_to_host_port: Dict[str, int] = {}
        for mac, port in mac_table.items():
            ip = host_mac_to_ip.get(mac)
            if ip:
                ip_to_host_port[ip] = port

        for src_ip in IP_QUEUE_MAP:
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
            rev_actions = [parser.OFPActionOutput(host_port)]
            self._add_flow(datapath, PRIORITY_QOS_FLOW - 1, rev_match, rev_actions)
            self.logger.info(
                "[FLOW] Reverse:      h3 → %s → port %d (no queue needed)",
                src_ip, host_port
            )

    # ──────────────────────────────────────────────────────────────
    # 5. Dynamic Policy Helpers (NEW)
    # ──────────────────────────────────────────────────────────────

    def update_queue_rates(
        self,
        datapath,
        new_queue_configs: List[Tuple[int, int, str]],
    ) -> bool:
        """
        Update OVS HTB queue minimum rates at runtime without touching any
        OpenFlow flow entries or restarting the controller.

        The OpenFlow SET_QUEUE action on s1 continues pointing to queue IDs
        0 / 1 / 2.  Only the *HTB scheduler parameters* (min-rate) change,
        so the effective bandwidth guarantee shifts immediately without
        any flow-table churn.

        Implementation strategy
        -----------------------
        OVS stores queue parameters in a ``Queue`` table row identified by
        its UUID.  We resolve each queue's UUID by asking:
          ``ovs-vsctl get qos <qos_uuid> queues:<queue_id>``
        and then set the new min-rate with:
          ``ovs-vsctl set queue <queue_uuid> other-config:min-rate=<rate>``

        Both calls are cheap (< 5 ms each) and fully atomic per queue row.

        Args:
            datapath:          Datapath of the QoS-enforcing switch (s1).
            new_queue_configs: List of (queue_id, new_min_rate_bps, label)
                               tuples. Only queues listed here are updated;
                               unlisted queues are left unchanged.

        Returns:
            True if all updates succeeded, False if any individual update
            failed (partial updates are possible; errors are logged).
        """
        port_name = S1_S2_PORT_NAME
        if not port_name:
            self.logger.error("[DYN] update_queue_rates: egress port not yet known")
            return False

        self.logger.info("[DYN] ── Dynamic queue-rate update on %s ──", port_name)

        all_ok = True
        for queue_id, new_rate_bps, label in new_queue_configs:
            try:
                # Resolve QoS UUID for this port
                qos_uuid = _run(
                    f"ovs-vsctl get port {port_name} qos",
                    self.logger
                ).strip()

                # Resolve the specific queue UUID inside that QoS object
                queue_uuid = _run(
                    f"ovs-vsctl get qos {qos_uuid} queues:{queue_id}",
                    self.logger
                ).strip()

                # Apply the new min-rate to that queue row
                _run(
                    f"ovs-vsctl set queue {queue_uuid} "
                    f"other-config:min-rate={new_rate_bps}",
                    self.logger
                )

                self.logger.info(
                    "[DYN] Queue %d updated → min-rate=%d bps  [%s]",
                    queue_id, new_rate_bps, label
                )

            except RuntimeError as exc:
                self.logger.error(
                    "[DYN] Failed to update Queue %d: %s", queue_id, exc
                )
                all_ok = False

        status = "OK" if all_ok else "PARTIAL FAILURE"
        self.logger.info("[DYN] Queue-rate update complete — %s", status)
        return all_ok

    def start_dynamic_swap_timer(self, datapath, delay_seconds: int = 20):
        """
        Spawn a non-blocking ryu.lib.hub greenthread that sleeps for
        *delay_seconds* and then calls update_queue_rates() to swap the
        min-rates of Queue 0 (h1, High) and Queue 2 (h4, Best-Effort).

        Design notes
        ------------
        • Uses ``hub.spawn()`` — Ryu's cooperative greenthread wrapper around
          eventlet — so it does NOT block the OpenFlow event loop.
        • The greenthread captures *datapath* by closure so the swap can log
          the DPID even though it runs asynchronously.
        • OpenFlow flow entries are intentionally NOT modified: the queue IDs
          in OFPActionSetQueue remain 0/1/2; only the HTB bandwidth guarantee
          changes.  This is the correct OVS/HTB behaviour — the "label" of a
          queue (its ID) is decoupled from its scheduling parameters.
        • If QoS setup earlier failed and S1_S2_PORT_NAME is empty,
          update_queue_rates() will log an error and return False gracefully.

        Args:
            datapath:      Datapath of s1 (needed for DPID logging).
            delay_seconds: Seconds to wait before executing the swap.
        """
        dpid = datapath.id

        def _swap_task():
            self.logger.info(
                "[DYN] Timer armed — policy swap will fire in %d s (DPID %016x)",
                delay_seconds, dpid
            )
            hub.sleep(delay_seconds)

            self.logger.info("=" * 60)
            self.logger.info(
                "[DYN] POLICY SWAP TRIGGERED (t = +%d s, DPID %016x)",
                delay_seconds, dpid
            )
            self.logger.info(
                "[DYN] Phase 1 → Phase 2:"
                "  Queue 0: 6 Mbps → 1 Mbps  |  Queue 2: 1 Mbps → 6 Mbps"
            )
            self.logger.info("=" * 60)

            success = self.update_queue_rates(datapath, QUEUE_CONFIG_PHASE2)

            if success:
                self.logger.info(
                    "[DYN] Swap complete. h4 (Queue 2) now has priority (6 Mbps); "
                    "h1 (Queue 0) demoted to best-effort (1 Mbps)."
                )
            else:
                self.logger.warning(
                    "[DYN] Swap finished with errors — check logs above."
                )

        # Spawn as a detached greenthread; returns immediately to the caller
        hub.spawn(_swap_task)
        self.logger.info(
            "[DYN] Background swap timer spawned (fires in %d s)", delay_seconds
        )

    # ──────────────────────────────────────────────────────────────
    # 6. Packet-In Handler (Reactive L2 Learning Switch)
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

        dst_mac   = eth_pkt.dst
        src_mac   = eth_pkt.src
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
            if msg.buffer_id != ofproto.OFP_NO_BUFFER:
                self._add_flow(datapath, PRIORITY_L2_LEARNED, match, actions,
                               buffer_id=msg.buffer_id)
                return  # OVS handles the buffered packet
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
    # 7. Utility: Add Flow Entry
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
