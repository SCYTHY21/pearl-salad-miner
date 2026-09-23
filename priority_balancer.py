#!/usr/bin/env python3
"""
Priority balancer: spread a fixed replica quota over identical container groups that
differ only in priority, always preferring the cheapest tier that has free nodes.

Tiers are given cheapest first; the LAST tier is the fallback that absorbs whatever
the cheaper tiers cannot place. Default:
    pearl-hash-lowest:batch  ->  pearl-hash-low:low  ->  pearl-hash-1:medium (fallback)

Why several groups instead of flipping one group's priority: changing a group's
priority redeploys every instance (lost startup minutes each time). Moving replica
counts between identical groups only touches the instances that actually change.

Each tick (INTERVAL seconds):
  1. read live availability for the GPU class per priority (batch / low / medium)
  2. read every group: running / allocating / replicas
  3. cheapest tier first:
       keep   = running + allocating   (allocating dropped once "stuck": nothing available
                                        at that tier for STUCK_AFTER minutes)
       grow   = min(available_at_tier, remaining) only when nothing is pending there
       target = keep + grow ; remaining -= target
     fallback tier gets the remainder
  4. PATCH replicas (shrinks first, then grows), log every decision to data/balancer.csv

    python priority_balancer.py --dry-run
    python priority_balancer.py --interval 300
    python priority_balancer.py --tiers pearl-hash-low:low,pearl-hash-1:medium --total 10

Secrets come from .env (SALAD_API_KEY, SALAD_ORG, SALAD_PROJECT). Never printed.
"""
import argparse
import csv
import datetime as dt
import json
import os
import sys
import time
import urllib.request

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from monitor import DATA, SALAD_API, get_json, load_env, iso, now_utc  # noqa: E402

AVAIL_KEY = {"batch": "available_gpu_batch", "low": "available_gpu_low", "medium": "available_gpu_medium", "high": "available_gpu_high"}


class Salad:
    def __init__(self, env):
        self.key, self.org, self.project = env["SALAD_API_KEY"], env["SALAD_ORG"], env.get("SALAD_PROJECT", "default")
        self.h = {"Salad-Api-Key": self.key}

    def group(self, name):
        return get_json(f"{SALAD_API}/organizations/{self.org}/projects/{self.project}/containers/{name}", self.h)

    def availability(self, gpu_ids, cpu, memory):
        body = json.dumps({"gpu_classes": gpu_ids, "cpu": cpu, "memory": memory}).encode()
        return get_json(f"{SALAD_API}/organizations/{self.org}/availability/sce-gpu-availability", self.h, body)

    def set_replicas(self, name, n):
        req = urllib.request.Request(
            f"{SALAD_API}/organizations/{self.org}/projects/{self.project}/containers/{name}",
            data=json.dumps({"replicas": int(n)}).encode(), method="PATCH",
            headers={**self.h, "Content-Type": "application/merge-patch+json", "Accept": "application/json"})
        with urllib.request.urlopen(req, timeout=30) as r:
            return r.status


def counts(g):
    c = (g or {}).get("current_state", {}).get("instance_status_counts", {})
    return c.get("running_count", 0), c.get("allocating_count", 0) + c.get("creating_count", 0), int((g or {}).get("replicas") or 0)


def decide(tiers, avail, total, stuck_after):
    """tiers: list of dicts {name, prio, run, alloc, rep, stuck_min}; returns list of targets + notes."""
    remaining = total
    targets, notes = [], []
    for t in tiers[:-1]:
        a = avail.get(AVAIL_KEY[t["prio"]], 0) or 0
        stuck = t["alloc"] > 0 and a == 0 and t["stuck_min"] >= stuck_after
        keep = t["run"] + (0 if stuck else t["alloc"])
        keep = min(keep, remaining)
        grow = min(a, remaining - keep) if (a > 0 and t["alloc"] == 0) else 0
        target = keep + grow
        if stuck:
            notes.append(f"{t['name']}: {t['alloc']} allocating {t['stuck_min']:.0f} min with 0 free -> release")
        if grow:
            notes.append(f"{t['name']}: {a} free at {t['prio']} -> +{grow}")
        targets.append(target)
        remaining -= target
    targets.append(max(0, remaining))
    return targets, notes


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--tiers", default="pearl-hash-lowest:batch,pearl-hash-low:low,pearl-hash-1:medium",
                    help="cheapest first, name:priority pairs; the last one is the fallback")
    ap.add_argument("--total", type=int, default=10, help="replica quota shared by all groups")
    ap.add_argument("--interval", type=int, default=300)
    ap.add_argument("--stuck-after", type=int, default=10, help="minutes a cheap-tier replica may sit allocating before being released")
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--once", action="store_true")
    args = ap.parse_args()
    try:
        sys.stdout.reconfigure(line_buffering=True)
    except Exception:
        pass
    env = load_env()
    for k in ("SALAD_API_KEY", "SALAD_ORG"):
        if not env.get(k):
            sys.exit(f"{k} missing in .env")
    salad = Salad(env)
    os.makedirs(DATA, exist_ok=True)
    tiers = [{"name": n, "prio": p} for n, p in (x.split(":") for x in args.tiers.split(","))]
    alloc_since = {t["name"]: None for t in tiers}
    fields = ["ts", "avail_batch", "avail_low", "avail_medium"] + [f"{k}_{t['name']}" for t in tiers for k in ("run", "alloc", "rep", "target")] + ["changed", "notes"]
    while True:
        t0 = now_utc()
        ok = True
        for t in tiers:
            g, err = salad.group(t["name"])
            if err:
                print(iso(t0), f"error reading {t['name']}: {err}"); ok = False; break
            t["run"], t["alloc"], t["rep"] = counts(g)
            t["group"] = g
            if t["alloc"] > 0:
                alloc_since[t["name"]] = alloc_since[t["name"]] or t0
            else:
                alloc_since[t["name"]] = None
            t["stuck_min"] = (t0 - alloc_since[t["name"]]).total_seconds() / 60 if alloc_since[t["name"]] else 0.0
        if not ok:
            time.sleep(args.interval); continue
        res = tiers[-1]["group"].get("container", {}).get("resources", {})
        av, e3 = salad.availability(res.get("gpu_classes", []), res.get("cpu", 2), res.get("memory", 4096))
        if e3:
            print(iso(t0), "availability unknown:", e3, "-> holding"); time.sleep(args.interval); continue
        targets, notes = decide(tiers, av, args.total, args.stuck_after)
        changes = [(t, tgt) for t, tgt in zip(tiers, targets) if tgt != t["rep"]]
        summary = " | ".join(f"{t['name'].replace('pearl-hash-', '')}[{t['prio']}] run/alloc/rep={t['run']}/{t['alloc']}/{t['rep']}" + (f"->{tgt}" if tgt != t['rep'] else "") for t, tgt in zip(tiers, targets))
        print(f"{iso(t0)} avail batch/low/med={av.get('available_gpu_batch', 0)}/{av.get('available_gpu_low', 0)}/{av.get('available_gpu_medium', 0)} | {summary} | {'; '.join(notes) or 'hold'}")
        if changes and not args.dry_run:
            try:
                for t, tgt in sorted(changes, key=lambda x: x[1] - x[0]["rep"]):  # shrinks (negative delta) first
                    salad.set_replicas(t["name"], tgt)
                    if tgt > t["rep"]:
                        alloc_since[t["name"]] = t0
            except Exception as e:
                print(iso(t0), "PATCH failed:", e); notes.append(f"PATCH failed: {e}")
        row = {"ts": iso(t0), "avail_batch": av.get("available_gpu_batch"), "avail_low": av.get("available_gpu_low"), "avail_medium": av.get("available_gpu_medium"),
               "changed": bool(changes) and not args.dry_run, "notes": "; ".join(notes) + (" (dry-run)" if args.dry_run and changes else "")}
        for t, tgt in zip(tiers, targets):
            row.update({f"run_{t['name']}": t["run"], f"alloc_{t['name']}": t["alloc"], f"rep_{t['name']}": t["rep"], f"target_{t['name']}": tgt})
        p = os.path.join(DATA, "balancer.csv")
        new = not os.path.exists(p)
        if not new:
            with open(p, encoding="utf-8") as f:
                header = f.readline().rstrip("\r\n").split(",")
            if header != fields:  # schema changed: rotate (file is closed by now, Windows needs that)
                os.replace(p, p + "." + dt.datetime.now().strftime("%Y%m%d%H%M%S") + ".bak"); new = True
        with open(p, "a", newline="", encoding="utf-8") as f:
            w = csv.DictWriter(f, fieldnames=fields)
            if new:
                w.writeheader()
            w.writerow(row)
        if args.once:
            break
        try:
            time.sleep(args.interval)
        except KeyboardInterrupt:
            break


if __name__ == "__main__":
    main()
