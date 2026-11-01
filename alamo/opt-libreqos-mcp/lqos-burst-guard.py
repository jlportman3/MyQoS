#!/usr/bin/env python3
"""Keep per-circuit HTB burst/cburst sized to the circuit's ceiling.

The lqosd bakery creates circuit classes without a burst, so tc gives every one
the 1600b default. At a few tens of Mbit and up that bucket is smaller than one
timer tick of data and HTB under-delivers ~10-15% of the ceiling (measured on
account 100019: wire rate 84% of ceiling at 1600b, 94-98% at 15Kb).

burst = cburst = max(FLOOR, 1.5 * ceil_bytes_per_ms), rounded up to 100 bytes.
Only circuit leaf classes with ceil >= 10 Mbit are touched (htb, leaf qdisc, prio 3); the
20 Gbit parent buckets and anything else are left alone.

Default is a dry run. --apply re-applies only classes that drifted, in one
`tc -batch`, so it is idempotent and cheap to run on a timer. New or rebuilt
classes (new circuit, rate change, lqosd restart) sit at 1600b until the next run.
Never fatal; logs to the journal via logger when applying.
Usage: lqos-burst-guard.py [--apply] [--verbose] [--dev NIC ...]
"""
import re, subprocess, sys, tempfile, os, collections

FLOOR = 15 * 1024          # bytes; tc's 'k' is 1024
FACTOR = 1.5               # x one millisecond of data at ceil
MIN_CEIL = 10e6           # bits/s; below this 1600b already covers a timer tick, leave alone
TOL = 0.05                 # treat within 5% as already correct (tc rounds on display)

UNIT = {"bit": 1, "Kbit": 1e3, "Mbit": 1e6, "Gbit": 1e9, "Tbit": 1e12}
BUNIT = {"b": 1, "Kb": 1024, "Mb": 1024 ** 2, "Gb": 1024 ** 3}
LINE = re.compile(
    r"^class htb (\S+) parent (\S+) leaf (\S+) prio (\d+) rate (\S+) ceil (\S+) "
    r"burst (\S+) cburst (\S+)")


def bits(s):
    m = re.fullmatch(r"([\d.]+)(bit|Kbit|Mbit|Gbit|Tbit)", s)
    return float(m.group(1)) * UNIT[m.group(2)] if m else None


def nbytes(s):
    m = re.fullmatch(r"([\d.]+)(b|Kb|Mb|Gb)", s)
    return float(m.group(1)) * BUNIT[m.group(2)] if m else None


def nics():
    out = []
    try:
        for ln in open("/etc/lqos.conf"):
            if re.match(r"\s*(to_internet|to_network)\s*=", ln):
                out += re.findall(r"enp[a-z0-9]+", ln)
    except OSError:
        pass
    return out


def target(ceil_bits):
    per_ms = ceil_bits / 8 / 1000
    t = max(FLOOR, FACTOR * per_ms)
    return int(-(-t // 100) * 100)


def plan(dev, verbose):
    r = subprocess.run(["tc", "class", "show", "dev", dev], capture_output=True, text=True)
    cmds, seen, skipped = [], 0, collections.Counter()
    for ln in r.stdout.splitlines():
        m = LINE.match(ln)
        if not m:
            continue
        cid, parent, leaf, prio, rate, ceil, burst, cburst = m.groups()
        if prio != "3":
            skipped["not-prio3"] += 1
            continue
        cb, bb, cbb = bits(ceil), nbytes(burst), nbytes(cburst)
        if None in (cb, bb, cbb):
            skipped["unparsed"] += 1
            continue
        seen += 1
        if cb < MIN_CEIL:
            skipped["ceil<10Mbit"] += 1
            continue
        t = target(cb)
        if bb >= t * (1 - TOL) and cbb >= t * (1 - TOL):
            continue
        cmds.append((dev, cid, ceil, int(bb), t,
                     f"class change dev {dev} parent {parent} classid {cid} htb "
                     f"rate {rate} ceil {ceil} prio {prio} burst {t} cburst {t}"))
    return seen, cmds, skipped


def main():
    apply = "--apply" in sys.argv
    verbose = "--verbose" in sys.argv
    devs = [a for i, a in enumerate(sys.argv) if i and sys.argv[i - 1] == "--dev"] or nics()
    if not devs:
        print("no shaping NICs found", file=sys.stderr)
        return 0
    allcmds = []
    for d in devs:
        if not os.path.exists(f"/sys/class/net/{d}"):
            continue
        seen, cmds, skipped = plan(d, verbose)
        print(f"{d}: {seen} circuit classes, {len(cmds)} need burst update"
              + (f", skipped {dict(skipped)}" if skipped else ""))
        allcmds += cmds
        if verbose:
            for c in cmds[:40]:
                print(f"   {c[1]:>8} ceil {c[2]:>10}  burst {c[3]}b -> {c[4]}b")
    if not allcmds:
        return 0
    bands = collections.Counter()
    for c in allcmds:
        bands[c[4]] += 1
    print("target burst (bytes) -> classes:", dict(sorted(bands.items())))
    if not apply:
        print("dry run: nothing changed (use --apply)")
        return 0
    with tempfile.NamedTemporaryFile("w", suffix=".tc", delete=False) as f:
        f.write("\n".join(c[5] for c in allcmds) + "\n")
        path = f.name
    r = subprocess.run(["tc", "-force", "-batch", path], capture_output=True, text=True)
    os.unlink(path)
    msg = f"burst updated on {len(allcmds)} classes" if r.returncode == 0 else \
          f"tc -batch FAILED rc={r.returncode}: {r.stderr.strip()[:300]}"
    print(msg)
    subprocess.run(["logger", "-t", "lqos-burst-guard", msg])
    return 0


if __name__ == "__main__":
    sys.exit(main())
