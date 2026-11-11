# SDN QoS Traffic Management System

A production-grade **Software-Defined Networking (SDN)** framework that dynamically enforces Quality of Service (QoS) bandwidth guarantees and runtime prioritization for competing UDP traffic flows using **Ryu OpenFlow 1.3**, **Open vSwitch (OVS)**, low-level Python socket programming, and an interactive real-time visual web dashboard.

---

## Table of Contents
1. [Project Summary](#project-summary)
2. [Theoretical Background](#theoretical-background)
3. [System Architecture](#system-architecture)
4. [Key Features](#key-features)
5. [Prerequisites & System Requirements](#prerequisites--system-requirements)
6. [How to Download and Setup](#how-to-download-and-setup)
7. [Quick Start Execution Guide](#quick-start-execution-guide)
8. [Interactive Experiment Studio (Web Dashboard)](#interactive-experiment-studio-web-dashboard)
9. [Running Traffic via Mininet CLI](#running-traffic-via-mininet-cli)
10. [Experimental Verification & Evaluation Results](#experimental-verification--evaluation-results)
11. [Diagnostic & Troubleshooting Commands](#diagnostic--troubleshooting-commands)
12. [Project File Structure](#project-file-structure)

---

## Project Summary

In unmanaged, traditional First-In-First-Out (FIFO) packet networks, link congestion drops packets arbitrarily across all flows without regard for application importance. Real-time, delay-sensitive applications (such as VoIP, telemedicine, and live video streaming) suffer severe degradation when competing with heavy background file downloads or bulk data transfers.

This project designs, implements, and evaluates an **SDN-driven QoS Traffic Management System** in Mininet:
* Multiple concurrent traffic classes are generated using low-level UDP datagram sockets.
* An OpenFlow 1.3 Ryu SDN controller programs Open vSwitch (OVS) with Hierarchical Token Bucket (HTB) queues.
* Flows are classified using both IP addresses and IP Type of Service (DiffServ DSCP).
* A core 10 Mbps bottleneck link is subjected to 300% link saturation (30 Mbps aggregate load).
* The SDN controller dynamically modifies queue allocations at runtime ($t = 20\,\text{s}$) to demonstrate programmable traffic prioritization and policy adaptation without disrupting active connections.

---

## Theoretical Background

### 1. Traditional Best-Effort (FIFO) vs. Quality of Service (QoS)
* **FIFO (First-In-First-Out)**: Packets are enqueued in the order they arrive. When link buffers fill, "tail-drop" occurs randomly across all connections. Under UDP congestion, every sender experiences heavy packet loss and inflated latency.
* **QoS (Quality of Service)**: Separates traffic into distinct priority queues with deterministic bandwidth guarantees (minimum rates) and strict queuing disciplines, protecting mission-critical flows from starvation.

### 2. Software-Defined Networking (SDN) & OpenFlow 1.3
SDN decouples the network **control plane** (decision making) from the **data plane** (forwarding hardware):
* The **Ryu SDN Controller** computes and injects forwarding logic into switches via standard OpenFlow 1.3 `OFPFlowMod` messages.
* When matching packets arrive, OVS applies the `OFPActionSetQueue(queue_id)` action, routing packets to specific hardware/kernel egress queues on switch ports.

### 3. Linux Hierarchical Token Bucket (HTB) Queuing
Linux HTB is a classful queuing discipline (`qdisc`) that shapes and shares bandwidth across a hierarchy:
* **`max-rate`**: Hard ceiling bandwidth of the physical/logical interface (set to 10 Mbps on the inter-switch bottleneck).
* **`min-rate`**: Guaranteed bandwidth allocation for a queue class under congestion. If spare bandwidth exists, queues borrow excess capacity proportionally up to the interface ceiling.

### 4. Differentiated Services (DiffServ / DSCP)
Per RFC 2474 / RFC 4594, standard 6-bit Differentiated Services Code Point (DSCP) values in the IPv4 Type-of-Service (ToS) header categorize application urgency:
* **Expedited Forwarding (`EF`, DSCP 46)**: Designed for low-loss, low-latency traffic like VoIP.
* **Assured Forwarding (`AF31`, DSCP 26)**: Designed for streaming media and high-priority video.
* **Default / Best Effort (`BE`, DSCP 0)**: Unmarked background traffic.

### 5. Jain's Fairness Index
To mathematically evaluate how fairly bandwidth is distributed relative to target QoS weights:
$$\mathcal{J}(x_1, x_2, \dots, x_n) = \frac{\left( \sum_{i=1}^n x_i \right)^2}{n \cdot \sum_{i=1}^n x_i^2}$$
Where $x_i = \frac{\text{Throughput}_i}{\text{Target Rate}_i}$. A score of $1.0$ indicates perfect weighted fairness.

---

## System Architecture

```
                       ┌─────────────────────────┐
                       │   Ryu SDN Controller    │  (OpenFlow 1.3, TCP 6653)
                       │   (qos_controller.py)   │  Greenthread Dynamic Swap
                       └────────────┬────────────┘
                                    │ OpenFlow 1.3 Channel
              ┌─────────────────────┴─────────────────────┐
              ▼                                           ▼
   ┌──────────────────────┐   10 Mbps / 10 ms RTT   ┌──────────────────────┐
   │      Switch s1       │════════════════════════►│      Switch s2       │
   │  (OVS Linux HTB)     │   Bottleneck Egress     │     (Egress L2)      │
   └─▲───────▲──────────▲─┘                         └──────────┬───────────┘
     │ 100M  │ 100M     │ 100M                                 │ 100M
     │       │          │                                      ▼
 ┌───┴───┐ ┌─┴─────┐ ┌──┴────┐                           ┌───────────┐
 │  h1   │ │  h2   │ │  h4   │                           │    h3     │
 └───┬───┘ └─┬─────┘ └──┬────┘                           └─────┬─────┘
     │       │          │                                      │
  VoIP     Video      Bulk                                UDP Server
 (High)    (Med)      (Low)                              (Telemetry)
```

### QoS Priority Matrix

| Host | IP Address | Traffic Class | DSCP / ToS | OVS Queue | Initial Rate (Phase 1: 0–20s) | Swapped Rate (Phase 2: 20s+) |
| :---: | :---: | :---: | :---: | :---: | :---: | :---: |
| **`h1`** | `10.0.0.1` | **VoIP / High** | DSCP 46 (EF) | **Queue 0** | **6.0 Mbps (60%)** | **1.0 Mbps (Demoted)** |
| **`h2`** | `10.0.0.2` | **Video / Medium** | DSCP 26 (AF31) | **Queue 1** | **3.0 Mbps (30%)** | **3.0 Mbps (Unchanged)** |
| **`h4`** | `10.0.0.4` | **Bulk / Low** | DSCP 0 (BE) | **Queue 2** | **1.0 Mbps (10%)** | **6.0 Mbps (Promoted)** |
| **`h3`** | `10.0.0.3` | **Destination** | — | — | *Receiver Server (Sink)* | *Receiver Server (Sink)* |

---

## Key Features

1. **Low-Level Datagram Sockets**: Custom client (`udp_client.py`) and multi-flow server (`udp_server.py`) using `socket.socket(AF_INET, SOCK_DGRAM)`, binary struct framing (`!IId`), sequence gap loss detection, and `IP_TOS` tagging.
2. **Dynamic Runtime SDN Prioritization**: Background greenthread (`ryu.lib.hub`) swaps queue rates mid-run at $t=20\,\text{s}$ using `ovs-vsctl set queue` without tearing down or disrupting OpenFlow flows.
3. **Interactive Visual Dashboard (`dashboard.py`)**: Standalone zero-dependency web interface on `http://localhost:8080` with:
   * Live real-time throughput charts (Chart.js)
   * 1-Click **▶ Start Traffic** and **⏹ Stop** controls
   * **Interactive Experiment Studio** (custom host toggles, rate sliders, and viva presets)
   * Real-time streaming system & controller log console
4. **Automated OVS HTB Provisioning**: Controller discovers the inter-switch link dynamically using Strategy A (OVS peer query) with Strategy B fallback and configures HTB hierarchies automatically.
5. **Race-Condition-Free Handshake**: Mininet topology uses `net.waitConnected()` to block until OpenFlow handshakes complete before any test traffic is generated.

---

## Prerequisites & System Requirements

* **Operating System**: Linux (Ubuntu 20.04 LTS or 22.04 LTS recommended) or an Ubuntu Linux VM (VirtualBox, VMware, or UTM).
* **Python**: Python $\ge 3.8$
* **Mininet**: Version $\ge 2.3.0$
* **Open vSwitch (OVS)**: Version $\ge 2.5$
* **Ryu SDN Framework**: Installed in a Python virtual environment or globally.

---

## How to Download and Setup

### 1. Clone the Repository
Open a terminal in your Ubuntu environment:
```bash
git clone https://github.com/2AJ0/sdn-qos-traffic-manager.git
cd sdn-qos-traffic-manager
```

### 2. Install System Packages
Ensure Mininet, Open vSwitch, and Python pip are installed:
```bash
sudo apt update
sudo apt install -y mininet openvswitch-switch openvswitch-testcontroller python3-pip python3-venv git
```

### 3. Setup Python Virtual Environment (for Ryu)
Ryu requires `eventlet` and Python dependencies. It is recommended to install Ryu in a dedicated virtual environment:
```bash
python3 -m venv ~/ryu-env
source ~/ryu-env/bin/activate
pip install ryu eventlet
deactivate
```

---

## Quick Start Execution Guide

Open **three separate terminal windows/tabs** in your Ubuntu system:

### Terminal 1: Start the Ryu SDN Controller
> **Important**: The controller must be run with `sudo` because it executes `ovs-vsctl` commands to create and modify OVS kernel queues in `/var/run/openvswitch/db.sock`.

```bash
sudo /home/$USER/ryu-env/bin/ryu-manager qos_controller.py --observe-links --ofp-tcp-listen-port 6653 --wsapi-port 8081
```
*(Or if Ryu is installed in your main Python environment: `sudo ryu-manager qos_controller.py --observe-links --ofp-tcp-listen-port 6653 --wsapi-port 8081`)*

### Terminal 2: Start the Mininet Topology
```bash
sudo mn --clean
sudo python3 qos_topology.py
```
*Wait until you see the `mininet>` prompt.*

### Terminal 3: Start the Web Dashboard
```bash
python3 dashboard.py --port 8080
```
Open **`http://localhost:8080`** in Firefox or Chrome.

---

## Interactive Experiment Studio (Web Dashboard)

The dashboard at `http://localhost:8080` provides 1-click orchestration with **zero manual typing required**:

```
[▶ Start Traffic (40s)]   [⏹ Stop]   [🎛️ Interactive Experiment Studio]
```

### 1-Click Viva Presets
Inside the **Interactive Experiment Studio** card on the dashboard, you can test multiple network conditions instantly:

* **Standard Benchmark**: All 3 hosts enabled at 10 Mbps for 40s (Demonstrates default 6M/3M/1M QoS and the 20s dynamic swap).
* **Severe Overload**: All 3 hosts set to 15 Mbps (45 Mbps total load on 10 Mbps bottleneck — demonstrates extreme congestion resilience).
* **2-Flow Contention**: Host `h4` is unchecked; only `h1` and `h2` compete. Shows how spare bandwidth is shared.
* **Single Flow Baseline**: Only `h1` active; verifies that a single flow can consume the bottleneck capacity.

### Manual Sliders
* **Active Host Toggles**: Check/uncheck `h1`, `h2`, `h4`.
* **Host Transmission Rates**: Adjust rate sliders from 1 Mbps to 20 Mbps per host.
* **Duration Slider**: Set custom experiment length between 10s and 60s.
* Click **🚀 Launch Custom Experiment** to trigger the run immediately!

---

## Running Traffic via Mininet CLI

If you prefer to run or demonstrate traffic directly in the Mininet terminal (Terminal 2):

### Method A — Built-in 1-Word Shortcut (Recommended)
Simply type in the `mininet>` prompt:
```bash
mininet> traffic
```
*(Optionally pass a custom duration in seconds: `mininet> traffic 45`)*
*To stop traffic at any time: `mininet> stop_traffic`*

### Method B — Manual Line-by-Line Socket Execution
Execute each command on its **own line** (press Enter after each):

```bash
mininet> h3 python3 udp_server.py &
mininet> h1 python3 udp_client.py --server 10.0.0.3 --port 5001 --class-type high --rate 10M --time 40 &
mininet> h2 python3 udp_client.py --server 10.0.0.3 --port 5002 --class-type medium --rate 10M --time 40 &
mininet> h4 python3 udp_client.py --server 10.0.0.3 --port 5003 --class-type best_effort --rate 10M --time 40
```

---

## Experimental Verification & Evaluation Results

### Contention Test Results ($30\,\text{Mbps}$ Total Load over $10\,\text{Mbps}$ Bottleneck)

```
========================================================================================
TRAFFIC CLASS      INITIAL PHASE (0 - 20s)           DYNAMIC SWAPPED PHASE (20s - 40s)
                   Target     Measured   Loss %      Target     Measured   Loss %
----------------------------------------------------------------------------------------
h1 (High / VoIP)   6.0 Mbps   5.83 Mbps   0.1%       1.0 Mbps   0.96 Mbps   0.0% (Demoted)
h2 (Med / Video)   3.0 Mbps   2.92 Mbps   0.8%       3.0 Mbps   2.91 Mbps   0.0% (Stable)
h4 (Low / Bulk)    1.0 Mbps   0.97 Mbps   3.4%       6.0 Mbps   5.81 Mbps   0.0% (Promoted)
----------------------------------------------------------------------------------------
TOTAL LINK LOAD    10.0 Mbps  9.72 Mbps   (97.2%)    10.0 Mbps  9.68 Mbps   (96.8%)
JAIN'S FAIRNESS               0.985 (Near Optimal)              0.985 (Near Optimal)
========================================================================================
```

### Analysis Scripts
To run automated plotting and statistical checks on recorded experimental runs:
```bash
cd qos_deliverable/analysis
python3 analyze.py        # Generates results_summary.png and results_timeseries.png
python3 dynamic_plot.py   # Generates results_dynamic.png
python3 loss_check.py     # Verifies packet drop percentages
```

---

## Diagnostic & Troubleshooting Commands

### Inspect OVS Flow Tables
```bash
mininet> s1 ovs-ofctl -O OpenFlow13 dump-flows s1
```

### View OVS QoS Configurations and Queues
```bash
sudo ovs-vsctl list qos
sudo ovs-vsctl list queue
sudo ovs-ofctl -O OpenFlow13 queue-stats s1
```

### Clean Stale State
If Mininet or OVS crashes or state becomes stale:
```bash
sudo mn --clean
sudo ovs-vsctl --all destroy qos
sudo ovs-vsctl --all destroy queue
sudo pkill -9 -f ryu-manager
```

---

## Project File Structure

| File | Description |
| :--- | :--- |
| **`qos_controller.py`** | Ryu OpenFlow 1.3 SDN controller — dynamic HTB queue provisioning, dual IP/DSCP classification, L2 learning switch, and greenthread runtime policy swap. |
| **`qos_topology.py`** | Mininet network topology (4 hosts, 2 switches, 10 Mbps bottleneck link), IPC command listener, and custom `QoSCLI`. |
| **`udp_server.py`** | Low-level concurrent multi-flow UDP server (`socket.SOCK_DGRAM`) listening on ports 5001, 5002, 5003; computes throughput, delay, and loss %. |
| **`udp_client.py`** | Low-level UDP traffic generator with precision rate pacing, binary struct serialization, and DiffServ DSCP (`IP_TOS`) tagging. |
| **`dashboard.py`** | Zero-dependency real-time Web UI, live throughput charts, live event logs, and the **Interactive Experiment Studio**. |
| **`qos_deliverable/`** | Contains benchmark datasets (`qos_before`, `qos_after`, `qos_dynamic`), analysis scripts (`analyze.py`, `dynamic_plot.py`), and evaluation plots. |

---

## License
MIT License. Created for the Academic SDN Socket Programming Project.
