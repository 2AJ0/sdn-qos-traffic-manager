import os, re
d = os.path.expanduser("~/qos_after2")
DUR = 30
print(f"{'host':<5}{'asked Mbps':>11}{'sent Mbps':>11}{'sent':>8}{'lost':>7}{'total':>8}{'loss %':>8}")
for h in ("h1", "h2", "h4"):
    t = open(f"{d}/cli_{h}.txt").read()
    sent = int(re.search(r"Sent (\d+) datagrams", t).group(1))
    lost, total = map(int, re.search(r"(\d+)/(\d+) \(", t).groups())
    rate = sent * 1470 * 8 / DUR / 1e6
    print(f"{h:<5}{10:>11}{rate:>11.2f}{sent:>8}{lost:>7}{total:>8}{100*lost/total:>8.2f}")
