#!/usr/bin/env python3
"""
Pearl-on-SaladCloud monitor.

Every INTERVAL seconds it pulls, from public/authenticated APIs:
  * SaladCloud   : container group state, instances (machine, state, GPU class), live GPU availability
  * Kryptex pool : wallet balance (unconfirmed / confirmed / paid), per-worker hashrate and shares
  * CoinGecko    : SafeTrade PRL/USDT last price, 24 h volume, bids within 2 % below price
and appends everything to CSV files under data/, then prints a one-screen summary with the
measured economics (revenue/day from measured hashrate, estimated Salad cost, margin).

    python monitor.py --once            # one snapshot
    python monitor.py                   # loop every 600 s (Ctrl+C to stop)
    python monitor.py --interval 300
    python monitor.py --report          # aggregate what has been collected so far

Only the standard library is used. Secrets come from .env (never printed).
"""
import argparse
import csv
import datetime as dt
import json
import os
import re
import sys
import threading
import time
import urllib.error
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlparse

HERE = os.path.dirname(os.path.abspath(__file__))
DATA = os.path.join(HERE, "data")
UA = "pearl-salad-monitor/1.0 (+https://github.com/SCYTHY21/pearl-salad-miner)"

SALAD_API = "https://api.salad.com/api/public"
KRYPTEX = "https://pool.kryptex.com"
COINGECKO = "https://api.coingecko.com/api/v3/coins/pearl-2/tickers?depth=true"

# Model parameters used only for the "expected" columns; measured values override them.
NET_YIELD_PRL_PER_TH_DAY = 0.0263   # net of 2 % PPS+ fee, from 23 Sep 2026 network stats
GPU_EXPECTED_THS = {"RTX 4090": 260, "RTX 5090": 350, "RTX 3090": 140, "RTX 5080": 210}


# ----------------------------------------------------------------------------- helpers
def load_env(path=os.path.join(HERE, ".env")):
    env = {}
    if os.path.exists(path):
        with open(path, encoding="utf-8-sig") as f:
            for line in f:
                line = line.strip()
                if not line or line.startswith("#") or "=" not in line:
                    continue
                k, v = line.split("=", 1)
                env[k.strip()] = v.strip()
    for k in ("SALAD_API_KEY", "SALAD_ORG", "SALAD_PROJECT", "WALLET", "GROUP"):
        if os.environ.get(k):
            env[k] = os.environ[k]
    return env


def get_json(url, headers=None, data=None, timeout=25):
    req = urllib.request.Request(url, data=data, headers={"User-Agent": UA, "Accept": "application/json", **(headers or {})})
    if data is not None:
        req.add_header("Content-Type", "application/json")
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return json.loads(r.read().decode("utf-8")), None
    except urllib.error.HTTPError as e:
        return None, f"HTTP {e.code} {url.split('?')[0][-60:]}"
    except Exception as e:  # network errors are data too: "unknown", not zero
        return None, f"{type(e).__name__}: {e}"


def now_utc():
    return dt.datetime.now(dt.timezone.utc).replace(microsecond=0)


def iso(t):
    return t.strftime("%Y-%m-%dT%H:%M:%SZ")


def sanitize_worker(machine_id):
    return "s" + re.sub(r"[^A-Za-z0-9]", "", machine_id or "")[:10]


def ths(hps):
    try:
        return round(float(hps) / 1e12, 1)
    except (TypeError, ValueError):
        return None


def append_csv(path, row, fieldnames):
    new = not os.path.exists(path)
    if not new:
        with open(path, encoding="utf-8") as f:
            header = f.readline().rstrip("\r\n").split(",")
        if header != fieldnames:  # schema changed: keep the old file aside, start a fresh one
            os.replace(path, path + "." + dt.datetime.now().strftime("%Y%m%d%H%M%S") + ".bak")
            new = True
    with open(path, "a", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=fieldnames, extrasaction="ignore")
        if new:
            w.writeheader()
        w.writerow(row)


def read_rows(path):
    if not os.path.exists(path):
        return []
    with open(path, encoding="utf-8") as f:
        return list(csv.DictReader(f))


def parse_ts(s):
    return dt.datetime.strptime(s, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=dt.timezone.utc)


MAX_GAP_H = 1.0  # when the monitor was down, assume the last cost rate continued for at most this long


def cum_cost(rows):
    """Cumulative estimated Salad cost per snapshot row: running instances x class price x elapsed time."""
    out, total, prev_t, prev_rate = [], 0.0, None, 0.0
    for r in rows:
        try:
            t = parse_ts(r["ts"])
        except Exception:
            out.append(round(total, 4)); continue
        if prev_t is not None:
            total += prev_rate * min((t - prev_t).total_seconds() / 3600.0, MAX_GAP_H)
        out.append(round(total, 4))
        prev_t = t
        try:
            prev_rate = float(r.get("cost_rate_usd_h") or 0)
        except ValueError:
            prev_rate = 0.0
    return out


# ----------------------------------------------------------------------------- sources
class Salad:
    def __init__(self, env):
        self.key = env.get("SALAD_API_KEY", "")
        self.org = env.get("SALAD_ORG", "")
        self.project = env.get("SALAD_PROJECT", "default")
        self.h = {"Salad-Api-Key": self.key}
        self.classes = {}      # id -> {"name":..., "prices": {priority: price}}

    def ok(self):
        return bool(self.key and self.org)

    def gpu_classes(self):
        d, err = get_json(f"{SALAD_API}/organizations/{self.org}/gpu-classes", self.h)
        if err:
            return err
        items = d.get("items", d) if isinstance(d, dict) else d
        for g in items or []:
            prices = {p.get("priority"): float(p.get("price") or 0) for p in g.get("prices", [])}
            self.classes[g.get("id")] = {"name": g.get("name", ""), "prices": prices}
        return None

    def group(self, name):
        return get_json(f"{SALAD_API}/organizations/{self.org}/projects/{self.project}/containers/{name}", self.h)

    def instances(self, name):
        return get_json(f"{SALAD_API}/organizations/{self.org}/projects/{self.project}/containers/{name}/instances", self.h)

    def availability(self, gpu_ids, cpu=2, memory=4096):
        body = json.dumps({"gpu_classes": gpu_ids, "cpu": cpu, "memory": memory}).encode()
        return get_json(f"{SALAD_API}/organizations/{self.org}/availability/sce-gpu-availability", self.h, body)


class Kryptex:
    def __init__(self, wallet):
        self.wallet = wallet

    def balance(self):
        return get_json(f"{KRYPTEX}/prl/api/v1/miner/balance/{self.wallet}")

    def workers(self):
        return get_json(f"{KRYPTEX}/prl/api/v3/miner/workers/{self.wallet}")

    def payouts(self):
        return get_json(f"{KRYPTEX}/prl/api/v1/miner/payouts/{self.wallet}/stats")

    @staticmethod
    def rate():
        d, err = get_json(f"{KRYPTEX}/api/v1/rates")
        return (d or {}).get("crypto", {}).get("PRL"), err


class PearlHash:
    """pearlhash.xyz: /api/stats (pool) and /api/account/<address> (wallet; 404 until the first share)."""
    BASE = "https://pearlhash.xyz"

    def __init__(self, wallet):
        self.wallet = wallet

    def pool(self):
        return get_json(f"{self.BASE}/api/stats")

    def account(self):
        return get_json(f"{self.BASE}/api/account/{self.wallet}")

    @staticmethod
    def pick(d, *names):
        """first matching key (case-insensitive, nested one level) or None"""
        if not isinstance(d, dict):
            return None
        low = {k.lower(): v for k, v in d.items()}
        for n in names:
            if n.lower() in low:
                return low[n.lower()]
        for v in d.values():
            if isinstance(v, dict):
                r = PearlHash.pick(v, *names)
                if r is not None:
                    return r
        return None


def safetrade_price():
    d, err = get_json(COINGECKO)
    if err:
        return None, err
    for t in d.get("tickers", []):
        if t.get("market", {}).get("name") == "SafeTrade" and t.get("base") == "PRL" and t.get("target") == "USDT":
            return {
                "last": t.get("last"),
                "volume_usd_24h": round(t.get("converted_volume", {}).get("usd", 0)),
                "spread_pct": t.get("bid_ask_spread_percentage"),
                "bids_2pct_usd": round(t.get("cost_to_move_down_usd") or 0),
                "asks_2pct_usd": round(t.get("cost_to_move_up_usd") or 0),
                "last_trade": t.get("last_traded_at"),
            }, None
    return None, "SafeTrade PRL/USDT ticker not in CoinGecko response"


# ----------------------------------------------------------------------------- snapshot
def snapshot(env, salad, kx, state, interval):
    t = now_utc()
    errors = []
    groups = [x.strip() for x in env.get("GROUP", "pearl-test-1").replace(";", ",").split(",") if x.strip()]
    group_name = ",".join(groups)

    # --- Salad (one or more container groups, e.g. a low-priority and a medium-priority twin)
    running = creating = allocating = 0
    replicas = 0
    statuses = []
    inst_rows = []
    class_counts = {}
    cost_rate = 0.0  # USD/h for currently running instances
    avail = {}
    gpu_ids_all = []
    if salad.ok():
        for gname in groups:
            g, err = salad.group(gname)
            if err:
                errors.append(f"salad group {gname}: " + err)
                continue
            cs = g.get("current_state", {})
            statuses.append(f"{gname}={cs.get('status', 'n/a')}")
            replicas += int(g.get("replicas") or 0)
            c = cs.get("instance_status_counts", {})
            running += c.get("running_count", 0); creating += c.get("creating_count", 0); allocating += c.get("allocating_count", 0)
            priority = g.get("priority", "low")
            gpu_ids = g.get("container", {}).get("resources", {}).get("gpu_classes", [])
            gpu_ids_all += [x for x in gpu_ids if x not in gpu_ids_all]
            ins, err2 = salad.instances(gname)
            if err2:
                errors.append(f"salad instances {gname}: " + err2)
                continue
            for i in ins.get("instances", []):
                gid = i.get("gpu_class") or i.get("gpu_class_id") or ""
                cls = salad.classes.get(gid, {})
                name = cls.get("name") or gid or "unknown"
                price = cls.get("prices", {}).get(priority, 0.0)
                st = i.get("state", "")
                inst_rows.append({
                    "ts": iso(t), "group": gname, "priority": priority, "machine_id": i.get("machine_id", ""),
                    "worker": sanitize_worker(i.get("machine_id", "")),
                    "state": st, "ready": i.get("ready"), "started": i.get("started"), "gpu_class": name,
                    "price_usd_h": price, "update_time": i.get("update_time", ""), "raw_keys": ";".join(sorted(i.keys())),
                })
                if st == "running":
                    key = f"{name}@{priority}"
                    class_counts[key] = class_counts.get(key, 0) + 1
                    cost_rate += price
        av, err3 = salad.availability(gpu_ids_all) if gpu_ids_all else ({}, None)
        if err3:
            errors.append("salad availability: " + err3)
        else:
            avail = av or {}
    else:
        errors.append("salad: SALAD_API_KEY / SALAD_ORG missing in .env")
    status = ";".join(statuses) if statuses else "n/a"

    # accumulate estimated cost: running instances × price × elapsed since last tick (gap capped)
    if state.get("last_ts"):
        elapsed_h = min((t - state["last_ts"]).total_seconds() / 3600.0, MAX_GAP_H)
        state["cost_est_usd"] = state.get("cost_est_usd", 0.0) + state.get("last_cost_rate", 0.0) * elapsed_h
    state["last_ts"], state["last_cost_rate"] = t, cost_rate
    state["replicas"] = replicas

    # --- Kryptex
    bal, e1 = kx.balance()
    wk, e2 = kx.workers()
    po, e3 = kx.payouts()
    for e in (e1, e2, e3):
        if e:
            errors.append("kryptex: " + e)
    bal, po = bal or {}, po or {}
    workers = (wk or {}).get("results", [])
    online = [w for w in workers if w.get("status") == "online"]
    sum_30m = sum((float(w.get("avg_hashrate_30m") or 0) for w in workers)) / 1e12
    sum_3h = sum((float(w.get("avg_hashrate_3h") or 0) for w in workers)) / 1e12
    valid = sum(int(w.get("valid") or 0) for w in workers)
    stale = sum(int(w.get("stale") or 0) for w in workers)
    invalid = sum(int(w.get("invalid") or 0) for w in workers)
    worker_rows = []
    for w in workers:
        worker_rows.append({
            "ts": iso(t), "worker": w.get("worker"), "status": w.get("status"),
            "ths_30m": ths(w.get("avg_hashrate_30m")), "ths_3h": ths(w.get("avg_hashrate_3h")), "ths_24h": ths(w.get("avg_hashrate_24h")),
            "valid": w.get("valid"), "stale": w.get("stale"), "invalid": w.get("invalid"),
            "last_share": w.get("last_share"), "opened_at": w.get("opened_at"),
        })

    # --- PearlHash (raw JSON kept verbatim; the summary uses best-effort keys until the format is known)
    ph = PearlHash(kx.wallet)
    ph_acc, e5 = ph.account()
    ph_pool, e6 = ph.pool()
    if e5 and "404" not in e5:
        errors.append("pearlhash account: " + e5)
    if e6:
        errors.append("pearlhash pool: " + e6)
    if ph_acc is not None:
        with open(os.path.join(DATA, "pearlhash_account.jsonl"), "a", encoding="utf-8") as f:
            f.write(json.dumps({"ts": iso(t), "account": ph_acc}) + "\n")
    # /api/account shape (seen 2026-09-23): connected_workers[], estimated_hashrate_{15s,60s,5m} (H/s),
    # balance_transactions[{amount, reason, timestamp, coin_type}] with epoch credits (+) and payouts (-)
    acc = ph_acc or {}
    ph_hash = acc.get("estimated_hashrate_5m")
    ph_worker_list = acc.get("connected_workers") or []
    ph_workers = len(ph_worker_list)
    txs = acc.get("balance_transactions") or []
    ph_credits = sum(float(x.get("amount") or 0) for x in txs if float(x.get("amount") or 0) > 0)
    ph_paid = -sum(float(x.get("amount") or 0) for x in txs if float(x.get("amount") or 0) < 0)
    ph_balance = round(ph_credits - ph_paid, 6) if txs else None
    ph_paid = round(ph_paid, 6) if txs else None
    ph_pool_hash = (ph_pool or {}).get("hashrate")
    for w in ph_worker_list:
        with open(os.path.join(DATA, "pearlhash_workers.jsonl"), "a", encoding="utf-8") as f:
            f.write(json.dumps({"ts": iso(t), "worker": w}) + "\n")

    # --- price
    px, e4 = safetrade_price()
    if e4:
        errors.append("coingecko: " + e4)
    px = px or {}
    kx_rate, _ = Kryptex.rate()
    price = px.get("last") or kx_rate or 0.0

    # --- economics (measured where possible)
    prl_day_measured = sum_30m * NET_YIELD_PRL_PER_TH_DAY
    rev_day = prl_day_measured * float(price or 0)
    cost_day = cost_rate * 24.0
    row = {
        "ts": iso(t), "group": group_name, "status": status, "replicas": replicas,
        "running": running, "creating": creating, "allocating": allocating,
        "classes_running": ";".join(f"{k}={v}" for k, v in sorted(class_counts.items())),
        "avail_low": avail.get("available_gpu_low"), "avail_medium": avail.get("available_gpu_medium"), "avail_high": avail.get("available_gpu_high"),
        "workers_total": len(workers), "workers_online": len(online),
        "ths_30m_total": round(sum_30m, 1), "ths_3h_total": round(sum_3h, 1),
        "ths_per_online_worker": round(sum_30m / len(online), 1) if online else None,
        "shares_valid": valid, "shares_stale": stale, "shares_invalid": invalid,
        "stale_pct": round(100.0 * stale / (valid + stale), 2) if (valid + stale) else None,
        "prl_unconfirmed": bal.get("unconfirmed"), "prl_confirmed": bal.get("confirmed"), "prl_total": bal.get("total"),
        "prl_paid": po.get("paid"), "prl_unpaid": po.get("unpaid"), "reward_week": (po.get("reward") or {}).get("week"),
        "ph_hashrate_ths": ths(ph_hash) if ph_hash is not None else None, "ph_balance": ph_balance, "ph_paid": ph_paid,
        "ph_workers": ph_workers, "ph_pool_ehs": round(float(ph_pool_hash) / 1e18, 2) if ph_pool_hash else None,
        "price_safetrade_usdt": px.get("last"), "price_kryptex_usd": kx_rate,
        "volume_24h_usd": px.get("volume_usd_24h"), "bids_2pct_usd": px.get("bids_2pct_usd"), "spread_pct": px.get("spread_pct"),
        "cost_rate_usd_h": round(cost_rate, 4), "cost_est_cum_usd": round(state.get("cost_est_usd", 0.0), 4),
        "prl_day_from_hashrate": round(prl_day_measured, 3), "rev_day_usd": round(rev_day, 2), "cost_day_usd": round(cost_day, 2),
        "margin_day_usd": round(rev_day - cost_day, 2),
        "errors": " | ".join(errors),
    }
    return row, inst_rows, worker_rows


SNAP_FIELDS = ["ts", "group", "status", "replicas", "running", "creating", "allocating", "classes_running",
               "avail_low", "avail_medium", "avail_high", "workers_total", "workers_online",
               "ths_30m_total", "ths_3h_total", "ths_per_online_worker", "shares_valid", "shares_stale", "shares_invalid", "stale_pct",
               "prl_unconfirmed", "prl_confirmed", "prl_total", "prl_paid", "prl_unpaid", "reward_week",
               "ph_hashrate_ths", "ph_balance", "ph_paid", "ph_workers", "ph_pool_ehs",
               "price_safetrade_usdt", "price_kryptex_usd", "volume_24h_usd", "bids_2pct_usd", "spread_pct",
               "cost_rate_usd_h", "cost_est_cum_usd", "prl_day_from_hashrate", "rev_day_usd", "cost_day_usd", "margin_day_usd", "errors"]
INST_FIELDS = ["ts", "group", "priority", "machine_id", "worker", "state", "ready", "started", "gpu_class", "price_usd_h", "update_time", "raw_keys"]
WORK_FIELDS = ["ts", "worker", "status", "ths_30m", "ths_3h", "ths_24h", "valid", "stale", "invalid", "last_share", "opened_at"]


def print_summary(row, inst_rows, worker_rows):
    print(f"\n=== {row['ts']}  group={row['group']}  status={row['status']}  "
          f"running={row['running']} creating={row['creating']} allocating={row['allocating']}  "
          f"[{row['classes_running'] or '-'}]  avail low/med/high={row['avail_low']}/{row['avail_medium']}/{row['avail_high']}")
    print(f"pool   : workers {row['workers_online']}/{row['workers_total']} online | {row['ths_30m_total']} TH/s (30m), "
          f"{row['ths_per_online_worker'] or 0} TH/s per online worker | shares valid/stale/invalid {row['shares_valid']}/{row['shares_stale']}/{row['shares_invalid']} "
          f"(stale {row['stale_pct'] or 0}%)")
    print(f"kryptex: unconfirmed {row['prl_unconfirmed']} | confirmed {row['prl_confirmed']} | paid {row['prl_paid']} PRL")
    print(f"pearlhash: hashrate {row['ph_hashrate_ths']} TH/s | balance {row['ph_balance']} | paid {row['ph_paid']} | workers {row['ph_workers']} | pool {row['ph_pool_ehs']} EH/s")
    print(f"price  : SafeTrade {row['price_safetrade_usdt']} USDT (Kryptex {row['price_kryptex_usd']}) | vol24h {row['volume_24h_usd']} USD | bids within 2%: {row['bids_2pct_usd']} USD")
    print(f"money  : cost now {row['cost_rate_usd_h']} USD/h ({row['cost_day_usd']} USD/day) | est. cost so far {row['cost_est_cum_usd']} USD | "
          f"PRL/day from hashrate {row['prl_day_from_hashrate']} -> {row['rev_day_usd']} USD/day | margin/day {row['margin_day_usd']} USD")
    for w in sorted(worker_rows, key=lambda x: -(x['ths_30m'] or 0)):
        print(f"  worker {w['worker']:14} {w['status']:8} {w['ths_30m'] or 0:>7} TH/s(30m) {w['ths_3h'] or 0:>7} TH/s(3h)  valid={w['valid']} stale={w['stale']} invalid={w['invalid']}")
    for i in inst_rows:
        print(f"  salad  {i['machine_id'][:14]:14} {i['state']:12} {i['gpu_class']:18} {i['price_usd_h']} USD/h  worker={i['worker']}")
    if row["errors"]:
        print("errors : " + row["errors"])


def report():
    p = os.path.join(DATA, "snapshots.csv")
    if not os.path.exists(p):
        print("no data yet"); return
    rows = read_rows(p)
    if not rows:
        print("no data yet"); return
    cum = cum_cost(rows)
    for r, c in zip(rows, cum):
        r["cost_est_cum_usd"] = c
    f = lambda r, k: float(r[k]) if r.get(k) not in (None, "", "None") else None
    first, last = rows[0], rows[-1]
    t0 = dt.datetime.strptime(first["ts"], "%Y-%m-%dT%H:%M:%SZ")
    t1 = dt.datetime.strptime(last["ts"], "%Y-%m-%dT%H:%M:%SZ")
    hours = max((t1 - t0).total_seconds() / 3600.0, 1e-9)
    run_avg = sum(float(r["running"] or 0) for r in rows) / len(rows)
    ths_avg = sum(f(r, "ths_30m_total") or 0 for r in rows) / len(rows)
    print(f"window        : {first['ts']} -> {last['ts']}  ({hours:.2f} h, {len(rows)} snapshots)")
    print(f"running avg   : {run_avg:.2f} instances | hashrate avg {ths_avg:.1f} TH/s | per instance {ths_avg / run_avg if run_avg else 0:.1f} TH/s")
    print(f"cost estimate : {f(last, 'cost_est_cum_usd')} USD so far ({(f(last, 'cost_est_cum_usd') or 0) / hours:.3f} USD/h)")
    print(f"PRL           : unconfirmed {last['prl_unconfirmed']} | confirmed {last['prl_confirmed']} | paid {last['prl_paid']} | total credited {f(last, 'prl_total')}")
    tot = f(last, "prl_total") or 0
    px = f(last, "price_safetrade_usdt") or 0
    cost = f(last, "cost_est_cum_usd") or 0
    print(f"realised so far: {tot:.3f} PRL x {px} = {tot * px:.2f} USD vs cost {cost:.2f} USD -> {tot * px - cost:+.2f} USD  "
          f"(per 24 h at this pace: {(tot * px - cost) / hours * 24:+.2f} USD)")
    wp = os.path.join(DATA, "workers.csv")
    if os.path.exists(wp):
        agg = {}
        for r in csv.DictReader(open(wp, encoding="utf-8")):
            a = agg.setdefault(r["worker"], {"n": 0, "ths": 0.0, "valid": 0, "stale": 0})
            a["n"] += 1; a["ths"] += float(r["ths_30m"] or 0)
            a["valid"] = max(a["valid"], int(r["valid"] or 0)); a["stale"] = max(a["stale"], int(r["stale"] or 0))
        print("per worker    : (avg 30m hashrate over all snapshots; shares are cumulative maxima)")
        for w, a in sorted(agg.items(), key=lambda kv: -kv[1]["ths"] / kv[1]["n"]):
            print(f"  {w:14} {a['ths'] / a['n']:7.1f} TH/s  valid={a['valid']} stale={a['stale']}")


# ----------------------------------------------------------------------------- dashboard server
SERVER_CTX = {"group": "", "interval": 0, "state": {}}


def history_payload(since_s=0):
    rows = read_rows(os.path.join(DATA, "snapshots.csv"))
    cum = cum_cost(rows)
    if since_s and rows:
        cutoff = now_utc() - dt.timedelta(seconds=since_s)
        keep = [i for i, r in enumerate(rows) if parse_ts(r["ts"]) >= cutoff]
        rows = [rows[i] for i in keep]; cum = [cum[i] for i in keep]
    last = rows[-1] if rows else {}
    nodes = []
    if last:
        ts = last["ts"]
        inst = [r for r in read_rows(os.path.join(DATA, "instances.csv")) if r.get("ts") == ts]
        kx_w = {r["worker"]: r for r in read_rows(os.path.join(DATA, "workers.csv")) if r.get("ts") == ts}
        ph_w = {}
        p = os.path.join(DATA, "pearlhash_workers.jsonl")
        if os.path.exists(p):
            with open(p, encoding="utf-8") as f:
                for line in f:
                    try:
                        j = json.loads(line)
                    except ValueError:
                        continue
                    if j.get("ts") == ts and isinstance(j.get("worker"), dict):
                        w = j["worker"]
                        name = w.get("name") or w.get("worker") or w.get("id") or ""
                        ph_w[name] = w
        seen = set()
        for i in inst:
            w = i.get("worker", "")
            pw = ph_w.get(w, {})
            kw = kx_w.get(w, {})
            hs = pw.get("estimated_hashrate_5m") or pw.get("hashrate") or pw.get("hashrate_5m")
            nodes.append({
                "worker": w, "machine_id": i.get("machine_id"), "state": i.get("state"),
                "gpu_class": (i.get("gpu_class") or "") + (f" · {i['priority']}" if i.get("priority") else ""),
                "price_usd_h": float(i["price_usd_h"]) if i.get("price_usd_h") not in (None, "") else None,
                "ths": ths(hs) if hs else (float(kw["ths_30m"]) if kw.get("ths_30m") else None),
                "last_share": pw.get("last_share") or pw.get("lastShare") or kw.get("last_share"),
            })
            seen.add(w)
        for name, w in ph_w.items():  # pool workers not matched to a Salad instance
            if name in seen:
                continue
            hs = w.get("estimated_hashrate_5m") or w.get("hashrate")
            nodes.append({"worker": name, "state": "pool only", "gpu_class": None, "price_usd_h": None, "ths": ths(hs) if hs else None, "last_share": w.get("last_share")})
    return {
        "snapshots": rows, "cost_cum": cum,
        "latest": {"snapshot": last, "group": SERVER_CTX["group"], "interval": SERVER_CTX["interval"],
                   "replicas": SERVER_CTX["state"].get("replicas") or (last.get("replicas") if last else None), "nodes": nodes},
    }


class Handler(BaseHTTPRequestHandler):
    def log_message(self, *a):  # keep the console for the monitor summary
        pass

    def _send(self, code, body, ctype):
        data = body if isinstance(body, bytes) else body.encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(data)

    def do_GET(self):
        u = urlparse(self.path)
        try:
            if u.path in ("/", "/index.html"):
                with open(os.path.join(HERE, "dashboard.html"), "rb") as f:
                    self._send(200, f.read(), "text/html; charset=utf-8")
            elif u.path == "/api/history":
                since = int(parse_qs(u.query).get("since", ["0"])[0] or 0)
                self._send(200, json.dumps(history_payload(since)), "application/json")
            elif u.path == "/api/latest":
                self._send(200, json.dumps(history_payload()["latest"]), "application/json")
            else:
                self._send(404, "not found", "text/plain")
        except Exception as e:  # never kill the server thread on a bad request
            self._send(500, f"error: {e}", "text/plain")


def start_server(port):
    srv = ThreadingHTTPServer(("127.0.0.1", port), Handler)
    th = threading.Thread(target=srv.serve_forever, daemon=True)
    th.start()
    print(f"dashboard: http://localhost:{port}/")
    return srv


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--once", action="store_true", help="single snapshot and exit")
    ap.add_argument("--interval", type=int, default=600, help="seconds between snapshots (default 600)")
    ap.add_argument("--report", action="store_true", help="aggregate collected CSVs and exit")
    ap.add_argument("--serve", type=int, metavar="PORT", help="also serve the dashboard on http://localhost:PORT/")
    args = ap.parse_args()
    if args.report:
        report(); return
    try:
        sys.stdout.reconfigure(line_buffering=True)  # summaries reach the log file immediately
    except Exception:
        pass

    env = load_env()
    wallet = env.get("WALLET")
    if not wallet:
        try:
            wallet = json.load(open(os.path.join(HERE, "container-group.example.json")))["container"]["environment_variables"]["WALLET"]
        except Exception:
            sys.exit("WALLET not found: add WALLET=prl1p... to .env")
    os.makedirs(DATA, exist_ok=True)
    salad = Salad(env)
    if salad.ok():
        err = salad.gpu_classes()
        if err:
            print("warning: could not load GPU class prices:", err)
    kx = Kryptex(wallet)
    state = {}
    prev = read_rows(os.path.join(DATA, "snapshots.csv"))  # seed the cost accumulator from history
    if prev:
        try:
            state["cost_est_usd"] = cum_cost(prev)[-1]
            state["last_ts"] = parse_ts(prev[-1]["ts"])
            state["last_cost_rate"] = float(prev[-1].get("cost_rate_usd_h") or 0)
        except Exception:
            state = {}
    SERVER_CTX.update({"group": env.get("GROUP", "pearl-test-1"), "interval": args.interval, "state": state})
    if args.serve:
        start_server(args.serve)
    while True:
        row, inst_rows, worker_rows = snapshot(env, salad, kx, state, args.interval)
        append_csv(os.path.join(DATA, "snapshots.csv"), row, SNAP_FIELDS)
        for r in inst_rows:
            append_csv(os.path.join(DATA, "instances.csv"), r, INST_FIELDS)
        for r in worker_rows:
            append_csv(os.path.join(DATA, "workers.csv"), r, WORK_FIELDS)
        print_summary(row, inst_rows, worker_rows)
        if args.once:
            break
        try:
            time.sleep(args.interval)
        except KeyboardInterrupt:
            print("\nstopped"); break


if __name__ == "__main__":
    main()
