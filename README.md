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
| `qos_controller.py` | Ryu OpenFlow 1.3 controller — HTB queue setup, proactive QoS flows, L2 learning switch |
| `qos_topology.py` | Custom Mininet topology — 4 hosts, 2 OVS switches, 10 Mbps bottleneck link |

---

## Features

- **OpenFlow 1.3** — `OFPFlowMod`, `OFPActionSetQueue`, `OFPActionOutput`
- **Automatic OVS HTB queue provisioning** — runs `ovs-vsctl` on switch connect
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

## Testing with iperf

### Simultaneous contention test (validates queue priorities)

```bash
# Inside Mininet CLI:
mininet> h3 iperf -s -u -p 5001 &
mininet> h3 iperf -s -u -p 5002 &
mininet> h3 iperf -s -u -p 5003 &
mininet> h1 iperf -c 10.0.0.3 -u -b 10M -t 30 -p 5001 &
mininet> h2 iperf -c 10.0.0.3 -u -b 10M -t 30 -p 5002 &
mininet> h4 iperf -c 10.0.0.3 -u -b 10M -t 30 -p 5003
```

Expected results when all three hosts saturate the 10 Mbps link:
- **h1 ≥ 6 Mbps** (Queue 0, High)
- **h2 ≥ 3 Mbps** (Queue 1, Medium)
- **h4 ≥ 1 Mbps** (Queue 2, Best-Effort)

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
