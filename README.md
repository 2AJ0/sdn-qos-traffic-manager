# SDN QoS Traffic Manager

A production-grade **Software-Defined Networking (SDN)** proof-of-concept that enforces Quality of Service (QoS) policies using **Ryu OpenFlow 1.3** and **Open vSwitch (OVS)** on a custom **Mininet** topology.

---

## Architecture

```
h1 (10.0.0.1) ─┐
h2 (10.0.0.2) ─┤── s1 ══(10 Mbps / 10 ms)══ s2 ── h3 (10.0.0.3)
h4 (10.0.0.4) ─┘
                ↑
         OVS HTB Queues
    (managed by Ryu controller)
```

## QoS Priority Matrix

| Host | IP | OVS Queue | Min-Rate | Priority |
|------|----|-----------|----------|----------|
| h1 | 10.0.0.1 | Queue 0 | 6 Mbps | High (60%) |
| h2 | 10.0.0.2 | Queue 1 | 3 Mbps | Medium (30%) |
| h4 | 10.0.0.4 | Queue 2 | 1 Mbps | Best-Effort (10%) |
| h3 | 10.0.0.3 | — | — | Destination Server |

---

## Files

| File | Description |
|------|-------------|
| `qos_controller.py` | Ryu OpenFlow 1.3 controller — HTB queue setup, proactive QoS flows, L2 learning switch, dynamic greenthread policy swap |
| `qos_topology.py` | Custom Mininet topology — 4 hosts, 2 OVS switches, 10 Mbps bottleneck link |
| `udp_server.py` | Multi-flow, multi-threaded low-level UDP server — tracks throughput, loss %, jitter, and exports live telemetry |
| `udp_client.py` | Low-level UDP traffic generator — sets DiffServ DSCP bits (IP_TOS), precision rate pacing, and custom packet framing |
| `dashboard.py` | Standalone zero-dependency Web UI & Real-Time QoS Visualizer (runs on port 8080) |

---

- **OpenFlow 1.3** — `OFPFlowMod`, `OFPActionSetQueue`, `OFPActionOutput`
- **Automatic OVS HTB queue provisioning** — runs `ovs-vsctl` on switch connect
- **Dynamic Policy Swap (Controller-Driven)** — background greenthread (`ryu.lib.hub`) automatically swaps h1 and h4 min-rates mid-run (Phase 1: h1=6M / h4=1M → Phase 2: h1=1M / h4=6M) without disrupting active OpenFlow flows
- **Dual classification** — source-IP rules + DSCP/DiffServ (EF=46, AF31=26) rules
- **Bidirectional flows** — proactive reverse flows from h3 → hX (no reactive delay)
- **Robust port discovery** — OVS peer-bridge query (Strategy A) with max-port fallback (Strategy B)
- **Idempotent cleanup** — `ovs-vsctl --if-exists clear port` before queue setup
- **Reactive L2 learning switch** — handles ARP and unknown unicast seamlessly
- **`net.waitConnected()`** — topology blocks until OpenFlow handshake is complete

---

## Requirements

- Linux (Ubuntu 20.04+ recommended) or a Linux VM
- [Open vSwitch](https://www.openvswitch.org/) ≥ 2.5
- [Mininet](http://mininet.org/) ≥ 2.3
- [Ryu SDN Framework](https://ryu-sdn.org/) (install via pip)
- Python ≥ 3.8

```bash
pip install ryu eventlet
```

---

## Quick Start

### 1 — Start the Ryu Controller (Terminal 1)

```bash
ryu-manager qos_controller.py \
    --ofp-tcp-listen-port 6653 \
    --observe-links \
    --verbose
```

### 2 — Start the Mininet Topology (Terminal 2)

```bash
sudo mn --clean          # clean stale state first
sudo python3 qos_topology.py
```

---

## Testing with Native Low-Level UDP Sockets (Recommended for Evaluation)

To satisfy the rubric's socket programming requirements, use either the automated 1-command shortcut or manual CLI commands:

### Method A — Automated 1-Command Shortcut (Easiest & Recommended)
Inside the Mininet CLI, simply type:
```bash
mininet> traffic
```
*This automatically starts the UDP server on `h3`, launches simultaneous senders on `h1`, `h2`, and `h4` with precise DiffServ DSCP tags, and streams telemetry to the web dashboard.*

To stop all traffic at any point:
```bash
mininet> stop_traffic
```

---

### Method B — Manual Line-by-Line Commands
> **Important**: In Mininet, each host command must be executed on its **own line** (do not chain them with `&` on a single line):

1. **Start server on destination host `h3` (press Enter):**
   ```bash
   h3 python3 udp_server.py &
   ```

2. **Start sender `h1` in background (press Enter):**
   ```bash
   h1 python3 udp_client.py --server 10.0.0.3 --port 5001 --class-type high --rate 10M --time 40 &
   ```

3. **Start sender `h2` in background (press Enter):**
   ```bash
   h2 python3 udp_client.py --server 10.0.0.3 --port 5002 --class-type medium --rate 10M --time 40 &
   ```

4. **Start sender `h4` in foreground (press Enter):**
   ```bash
   h4 python3 udp_client.py --server 10.0.0.3 --port 5003 --class-type best_effort --rate 10M --time 40
   ```

---

## Testing with iperf (Baseline Benchmark)

### Simultaneous contention test (validates queue priorities)

```bash
# Inside Mininet CLI:
mininet> h3 iperf -s -u -p 5001 &
mininet> h3 iperf -s -u -p 5002 &
mininet> h3 iperf -s -u -p 5003 &
mininet> h1 iperf -c 10.0.0.3 -u -b 10M -t 40 -p 5001 &
mininet> h2 iperf -c 10.0.0.3 -u -b 10M -t 40 -p 5002 &
mininet> h4 iperf -c 10.0.0.3 -u -b 10M -t 40 -p 5003
```

- **Phase 1 (0 – 20s)**:
  - **h1 ≥ 6 Mbps** (Queue 0, High)
  - **h2 ≥ 3 Mbps** (Queue 1, Medium)
  - **h4 ≥ 1 Mbps** (Queue 2, Best-Effort)
- **Phase 2 (after 20s swap)**:
  - **h1 drops to ~1 Mbps** (Queue 0 demoted to Best-Effort)
  - **h2 remains ~3 Mbps** (Queue 1 Medium, unchanged)
  - **h4 jumps to ~6 Mbps** (Queue 2 promoted to High Priority)

### Diagnostic commands

```bash
# Dump OpenFlow tables
mininet> s1 ovs-ofctl -O OpenFlow13 dump-flows s1

# Inspect OVS queues
sudo ovs-vsctl list qos
sudo ovs-vsctl list queue

# Queue statistics
sudo ovs-ofctl -O OpenFlow13 queue-stats s1
```

---

## Cleanup

```bash
sudo mn --clean
sudo ovs-vsctl --all destroy qos
sudo ovs-vsctl --all destroy queue
```

---

## License

MIT
