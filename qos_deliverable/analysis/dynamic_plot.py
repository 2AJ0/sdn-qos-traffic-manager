import csv, os, re, statistics as st
from datetime import datetime
import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt

d = os.path.expanduser("~/qos_dynamic")
fmt = "%Y%m%d%H%M%S"
flip = datetime.strptime(open(f"{d}/flip_time.txt").read().strip(), fmt)
H = {"h1": "h1 (6 -> 1 Mbps)", "h2": "h2 (3 Mbps, unchanged)", "h4": "h4 (1 -> 6 Mbps)"}

def iperf(h):
    out = []
    for r in csv.reader(open(f"{d}/srv_{h}.csv")):
        if len(r) < 14:
            continue
        a, b = (float(x) for x in r[6].split("-"))
        if b - a > 1.5:
            continue
        t = (datetime.strptime(r[0], fmt) - flip).total_seconds()
        out.append((t, float(r[8]) / 1e6))
    return out

def ping(h):
    out = []
    for line in open(f"{d}/ping_{h}.txt"):
        m = re.search(r"icmp_seq=(\d+).*time=([\d.]+)", line)
        if m:
            out.append((int(m.group(1)) - 20, float(m.group(2))))
    return out

fig, ax = plt.subplots(2, 1, figsize=(10, 8), sharex=True)
print(f"{'host':<5}{'before Mbps':>12}{'after Mbps':>12}{'RTT before':>12}{'RTT after':>11}")
for h, label in H.items():
    s = iperf(h)
    p = ping(h)
    ax[0].plot([x[0] for x in s], [x[1] for x in s], label=label)
    ax[1].plot([x[0] for x in p], [x[1] for x in p], marker="o", ms=3, label=label)
    b = st.mean(v for t, v in s if -17 <= t <= -2)
    a = st.mean(v for t, v in s if 5 <= t <= 23)
    rb = st.mean(v for t, v in p if -17 <= t <= -2)
    ra = st.mean(v for t, v in p if 5 <= t <= 23)
    print(f"{h:<5}{b:>12.2f}{a:>12.2f}{rb:>12.0f}{ra:>11.0f}")
for a_ in ax:
    a_.axvline(0, color="red", linestyle="--", label="policy change")
    a_.grid(alpha=0.3)
    a_.legend()
ax[0].set_ylabel("Throughput (Mbps)")
ax[0].set_title("Dynamic policy change: h1 and h4 queue rates swapped mid-run")
ax[1].set_ylabel("RTT (ms)")
ax[1].set_xlabel("Seconds since policy change")
fig.tight_layout()
fig.savefig("results_dynamic.png", dpi=150)
print("Saved results_dynamic.png")
