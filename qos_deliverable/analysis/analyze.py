#!/usr/bin/env python3
import csv, os, re, statistics as st
import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt

HOSTS = {"h1": ("h1 High (6 Mbps)", 6.0),
         "h2": ("h2 Medium (3 Mbps)", 3.0),
         "h4": ("h4 Best-effort (1 Mbps)", 1.0)}
RUNS = [("before", "Before: queues missing (no QoS)", "~/qos_before"),
        ("after", "After: QoS working", "~/qos_after")]
T0, T1 = 3, 28          # steady-state window (seconds)
PINGS_SENT = 30

def load_iperf(path):
    rows = []
    with open(path) as f:
        for r in csv.reader(f):
            if len(r) < 14:
                continue
            try:
                a, b = (float(x) for x in r[6].split("-"))
                if b - a > 1.5:          # skip the final summary line
                    continue
                rows.append((a, float(r[8]) / 1e6, int(r[10]), int(r[11])))
            except ValueError:
                continue
    return rows                           # (t, Mbps, lost, total)

def load_ping(path):
    pts = []
    with open(path) as f:
        for line in f:
            m = re.search(r"icmp_seq=(\d+).*time=([\d.]+)", line)
            if m:
                pts.append((int(m.group(1)), float(m.group(2))))
    return pts                            # (seq ~ seconds, RTT ms)

def jain(xs):
    return sum(xs) ** 2 / (len(xs) * sum(x * x for x in xs))

data = {}
for key, _, folder in RUNS:
    d = os.path.expanduser(folder)
    data[key] = {h: {"ip": load_iperf(f"{d}/srv_{h}.csv"),
                     "ping": load_ping(f"{d}/ping_{h}.txt")} for h in HOSTS}

# ---- summary table ----
summary = {}
print(f"\nSteady state (t = {T0}-{T1} s)")
for key, title, _ in RUNS:
    print(f"\n== {title} ==")
    print(f"{'host':<5}{'target':>8}{'Mbps':>8}{'srv loss%':>11}{'RTT avg':>9}{'RTT max':>9}{'ping loss%':>12}")
    thr_norm = []
    summary[key] = {}
    for h, (label, target) in HOSTS.items():
        w = [x for x in data[key][h]["ip"] if T0 <= x[0] <= T1]
        thr = st.mean(x[1] for x in w) if w else 0.0
        lost, tot = sum(x[2] for x in w), sum(x[3] for x in w)
        loss = 100 * lost / tot if tot else 0.0
        rtts = [ms for s, ms in data[key][h]["ping"] if s >= T0]
        rtt_avg = st.mean(rtts) if rtts else 0.0
        rtt_max = max(rtts) if rtts else 0.0
        ploss = 100 * (1 - len(data[key][h]["ping"]) / PINGS_SENT)
        summary[key][h] = thr
        thr_norm.append(thr / target)
        print(f"{h:<5}{target:>8.1f}{thr:>8.2f}{loss:>11.1f}{rtt_avg:>9.0f}{rtt_max:>9.0f}{ploss:>12.1f}")
    print(f"Jain fairness index (throughput / target share): {jain(thr_norm):.3f}")

# ---- time-series figure ----
fig, ax = plt.subplots(2, 2, figsize=(13, 8), sharey="row")
for col, (key, title, _) in enumerate(RUNS):
    for h, (label, _) in HOSTS.items():
        r = data[key][h]["ip"]
        ax[0][col].plot([x[0] for x in r], [x[1] for x in r], label=label)
        p = data[key][h]["ping"]
        ax[1][col].plot([x[0] for x in p], [x[1] for x in p], marker="o", ms=3, label=label)
    ax[0][col].set_title(title)
    ax[0][col].set_ylabel("Throughput (Mbps)")
    ax[1][col].set_ylabel("RTT (ms)")
    ax[1][col].set_xlabel("Time (s)")
    for row in (0, 1):
        ax[row][col].grid(alpha=0.3)
        ax[row][col].legend()
fig.tight_layout()
fig.savefig("results_timeseries.png", dpi=150)

# ---- summary bar chart ----
fig2, a = plt.subplots(figsize=(8, 5))
hs = list(HOSTS)
xs = range(len(hs))
w = 0.27
a.bar([x - w for x in xs], [HOSTS[h][1] for h in hs], w, label="Target")
a.bar(list(xs), [summary["before"][h] for h in hs], w, label="Before (no QoS)")
a.bar([x + w for x in xs], [summary["after"][h] for h in hs], w, label="After (QoS)")
a.set_xticks(list(xs))
a.set_xticklabels([HOSTS[h][0] for h in hs])
a.set_ylabel("Mean throughput (Mbps)")
a.set_title("Throughput per traffic class under congestion")
a.legend()
a.grid(axis="y", alpha=0.3)
fig2.tight_layout()
fig2.savefig("results_summary.png", dpi=150)
print("\nSaved results_timeseries.png and results_summary.png")
