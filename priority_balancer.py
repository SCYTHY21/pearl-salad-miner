#!/usr/bin/env python3
"""
Priority balancer: keep as many replicas as possible on the LOW-priority group and
the rest on the MEDIUM-priority group, within the replica quota.

Why two groups instead of flipping one group's priority: changing a group's priority
redeploys every instance (lost startup minutes each time). Moving replica counts between
two identical groups only touches the instances that actually change.

Loop (every INTERVAL seconds):
  1. read live availability for the GPU class (available_gpu_low)
  2. read both groups: running / allocating counts
  3. decide targets with hysteresis:
       - grow LOW only when nodes are actually free at low priority
       - shrink LOW when its replicas sit in "allocating" for longer than STUCK_MIN
         (nobody is giving us low nodes), so MEDIUM can use the quota instead
       - never exceed TOTAL replicas across both groups
       - at most one change per tick
  4. PATCH replicas, log every decision to data/balancer.csv

    python priority_balancer.py --dry-run          # print decisions, change nothing
    python priority_balancer.py                    # act, every 300 s
    python priority_balancer.py --interval 180 --total 10

Secrets come from .env (SALAD_API_KEY, SALAD_ORG, SALAD_PROJECT). Never printed.
"""
import argparse
import csv
import datetime as dt
import json
import os
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from monitor import DATA, SALAD_API, get_json, load_env, iso, now_utc  # noqa: E402

FIELDS = ["ts", "avail_low", "avail_medium", "low_running", "low_allocating", "low_replicas",
          "med_running", "med_allocating", "med_replicas", "low_stuck_min", "action", "new_low", "new_med", "note"]


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
        import urllib.request
        req = urllib.request.Request(
            f"{SALAD_API}/organizations/{self.org}/projects/{self.project}/containers/{name}",
            data=json.dumps({"replicas": int(n)}).encode(), method="PATCH",
            headers={**self.h, "Content-Type": "application/merge-patch+json", "Accept": "application/json"})
        with urllib.request.urlopen(req, timeout=30) as r:
            return r.status


def counts(g):
    c = (g or {}).get("current_state", {}).get("instance_status_counts", {})
    return c.get("running_count", 0), c.get("allocating_count", 0) + c.get("creating_count", 0), (g or {}).get("replicas", 0)


def decide(avail_low, low_run, low_alloc, low_rep, med_run, med_alloc, med_rep, low_stuck_min, total, stuck_after):
    """Returns (new_low, new_med, action, note)."""
    cur_total = low_rep + med_rep
    # 1. low replicas stuck allocating with nothing available -> hand them to medium
    if low_alloc > 0 and avail_low == 0 and low_stuck_min >= stuck_after:
        new_low = max(low_run, low_rep - low_alloc)
        return new_low, min(total, total - new_low), "shrink_low", f"{low_alloc} low replicas allocating for {low_stuck_min:.0f} min with 0 available"
    # 2. free low nodes and room to grow (either unused quota or medium replicas we can move)
    if avail_low > 0 and low_alloc == 0:
        grow = min(avail_low, total - low_rep)
        if grow > 0:
            new_low = low_rep + grow
            new_med = max(0, min(med_rep, total - new_low))
            return new_low, new_med, "grow_low", f"{avail_low} low nodes available; moving {grow} replica(s) to low"
    # 3. quota not fully used and low is not stuck -> fill medium
    if cur_total < total and not (low_alloc > 0 and avail_low == 0):
        return low_rep, med_rep + (total - cur_total), "fill_medium", f"{total - cur_total} unused replica(s) -> medium"
    return low_rep, med_rep, "hold", ""


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--low", default="pearl-hash-low")
    ap.add_argument("--medium", default="pearl-hash-1")
    ap.add_argument("--total", type=int, default=10, help="replica quota shared by both groups")
    ap.add_argument("--interval", type=int, default=300)
    ap.add_argument("--stuck-after", type=int, default=10, help="minutes a low replica may sit allocating before giving it to medium")
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
    low_alloc_since = None
    while True:
        t = now_utc()
        gl, e1 = salad.group(args.low)
        gm, e2 = salad.group(args.medium)
        if e1 or e2:
            print(iso(t), "error reading groups:", e1 or e2); time.sleep(args.interval); continue
        res = gm.get("container", {}).get("resources", {})
        av, e3 = salad.availability(res.get("gpu_classes", []), res.get("cpu", 2), res.get("memory", 4096))
        if e3:
            print(iso(t), "availability unknown:", e3, "-> holding"); time.sleep(args.interval); continue
        avail_low, avail_med = av.get("available_gpu_low", 0), av.get("available_gpu_medium", 0)
        low_run, low_alloc, low_rep = counts(gl)
        med_run, med_alloc, med_rep = counts(gm)
        if low_alloc > 0:
            low_alloc_since = low_alloc_since or t
        else:
            low_alloc_since = None
        stuck_min = (t - low_alloc_since).total_seconds() / 60 if low_alloc_since else 0.0
        new_low, new_med, action, note = decide(avail_low, low_run, low_alloc, low_rep, med_run, med_alloc, med_rep, stuck_min, args.total, args.stuck_after)
        changed = (new_low != low_rep) or (new_med != med_rep)
        line = (f"{iso(t)} avail low/med={avail_low}/{avail_med} | LOW run/alloc/rep={low_run}/{low_alloc}/{low_rep} "
                f"| MED run/alloc/rep={med_run}/{med_alloc}/{med_rep} | {action}: {note or '-'}"
                + (f" -> low={new_low} med={new_med}" if changed else ""))
        print(line)
        if changed and not args.dry_run:
            try:
                # shrink first so the quota is never exceeded, then grow
                if new_med < med_rep:
                    salad.set_replicas(args.medium, new_med)
                if new_low < low_rep:
                    salad.set_replicas(args.low, new_low)
                if new_low > low_rep:
                    salad.set_replicas(args.low, new_low)
                if new_med > med_rep:
                    salad.set_replicas(args.medium, new_med)
                if new_low > low_rep:
                    low_alloc_since = t  # the new low replicas start allocating now
            except Exception as e:
                print(iso(t), "PATCH failed:", e); note += f" | PATCH failed: {e}"
        row = dict(ts=iso(t), avail_low=avail_low, avail_medium=avail_med, low_running=low_run, low_allocating=low_alloc, low_replicas=low_rep,
                   med_running=med_run, med_allocating=med_alloc, med_replicas=med_rep, low_stuck_min=round(stuck_min, 1),
                   action=(action if changed else "hold") + (" (dry-run)" if args.dry_run and changed else ""), new_low=new_low, new_med=new_med, note=note)
        p = os.path.join(DATA, "balancer.csv")
        new = not os.path.exists(p)
        with open(p, "a", newline="", encoding="utf-8") as f:
            w = csv.DictWriter(f, fieldnames=FIELDS)
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
