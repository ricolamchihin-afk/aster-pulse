"""Phase 1c - longer holding periods for the locked follow_flow rule.

Predeclared in LEDGER.md §10 BEFORE any 15m/1h/4h return was computed:
  rule     = follow_flow (tau=0)
  horizons = 900s / 3600s / 14400s  (15m / 1h / 4h)
  costs    = fees_only, measured p50, measured p90, PLUS funding prints
             that fall in (entry, exit]
  fade_flow is a one-line sanity check, not a selection candidate.

    python research/phase1c_longer_holds.py
"""
import json
import time
from pathlib import Path

import httpx
import numpy as np
import pandas as pd

from phase1 import (LAT_BARS, cost_for, day_bootstrap_ci, independent_mask,
                    load_open_grids)
from phase1b_ofi import RULES, cell_stats, rule_direction
from reconstruct import load_universe
from spread_model import load_spreads

DATA = Path(__file__).parent / "data"
RESULTS = Path(__file__).parent / "results"
REST = "https://fapi.asterdex.com/fapi/v1"
HORIZONS = [900, 3600, 14400]  # 15m, 1h, 4h
SCENARIOS = [("fees_only", "fees only (8bp)"),
             ("measured_p50", "fees + measured p50 spread"),
             ("measured_p90", "fees + measured p90 spread")]


def fetch_funding(symbols, start_ms, end_ms):
    """Historical funding prints per symbol. Aster is typically 8-hourly."""
    out = {}
    with httpx.Client(timeout=20) as c:
        for s in symbols:
            rows = []
            cursor = start_ms
            for _ in range(20):
                r = c.get(f"{REST}/fundingRate", params={
                    "symbol": s, "startTime": cursor, "endTime": end_ms,
                    "limit": 1000,
                })
                if r.status_code != 200:
                    break
                page = r.json()
                if not page:
                    break
                for p in page:
                    rows.append((int(p["fundingTime"]), float(p["fundingRate"])))
                nxt = int(page[-1]["fundingTime"]) + 1
                if nxt <= cursor or len(page) < 1000:
                    break
                cursor = nxt
                time.sleep(0.05)
            out[s] = rows
    return out


def attach_long_forwards(fwd, grids, funding):
    """Add raw_{H} and fund_{H} columns. fund is the signed funding a LONG pays
    over (entry, exit]; a SHORT receives the opposite."""
    raw = {H: [] for H in HORIZONS}
    fund = {H: [] for H in HORIZONS}
    for _, e in fwd.iterrows():
        s = e["symbol"]
        g = grids.get(s)
        k = int(e["k"])
        entry_i = k + LAT_BARS
        if g is None or entry_i >= len(g[0]):
            for H in HORIZONS:
                raw[H].append(np.nan)
                fund[H].append(0.0)
            continue
        opn, has = g
        entry = e["entry"]
        entry_t = int(e["t"]) + LAT_BARS * 30_000
        prints = funding.get(s, [])
        for H in HORIZONS:
            xi = entry_i + H // 30
            if xi >= len(opn) or not has[xi] or not (opn[xi] > 0):
                raw[H].append(np.nan)
                fund[H].append(0.0)
                continue
            raw[H].append(np.log(opn[xi] / entry))
            exit_t = entry_t + H * 1000
            paid = 0.0
            for ft, fr in prints:
                if entry_t < ft <= exit_t:
                    paid += fr
            fund[H].append(paid)
    out = fwd.copy()
    for H in HORIZONS:
        out[f"raw_{H}"] = raw[H]
        out[f"fund_{H}"] = fund[H]
    return out


def net_series(fwd, rule, H, scenario, spreads):
    d = rule_direction(fwd, rule, 0.0)
    r = fwd[f"raw_{H}"].to_numpy()
    f = fwd[f"fund_{H}"].to_numpy()
    take = (d != 0) & np.isfinite(r)
    costs = np.array([cost_for(s, spreads, scenario) for s in fwd["symbol"]])
    # long pays funding; short receives it → subtract d * funding
    net_all = d[take] * r[take] - costs[take] - d[take] * f[take]
    sub = fwd[take].copy()
    if len(sub) == 0:
        return np.array([]), np.array([]), sub
    sub["_net"] = net_all
    hb = H // 30 + LAT_BARS
    imask = independent_mask(sub, hb)
    days = (sub["t"].to_numpy() // (86400 * 1000))[imask]
    return net_all[imask], days, sub[imask]


def print_table(title, cells):
    print(f"\n===== {title} =====")
    print(f"{'cell':<28}{'n':>6}{'mean':>9}{'median':>9}{'hit%':>7}"
          f"{'CI_lo':>9}{'CI_hi':>9}   verdict")
    for name, v in cells.items():
        if v["mean_bps"] is None:
            print(f"{name:<28}{v['n_indep']:>6}   (insufficient)")
            continue
        lo = v["ci95_lo_bps"]
        verdict = "edge>0" if (lo is not None and lo > 0) else "no edge"
        print(f"{name:<28}{v['n_indep']:>6}{v['mean_bps']:>9.1f}"
              f"{v['median_bps']:>9.1f}{v['hit_rate'] * 100:>7.1f}"
              f"{(lo if lo is not None else float('nan')):>9.1f}"
              f"{(v['ci95_hi_bps'] if v['ci95_hi_bps'] is not None else float('nan')):>9.1f}"
              f"   {verdict}")


def main():
    u = load_universe()
    start, end = u["start"], u["end"]
    cut = ((start + (end - start) * 2 // 3) // 30_000) * 30_000
    fwd = pd.read_parquet(DATA / "forward.parquet")
    print(f"events with usable entry: {len(fwd)}")
    grids = load_open_grids(u, start, end)
    symbols = [r["symbol"] for r in u["universe"]]
    print(f"fetching funding for {len(symbols)} symbols...", flush=True)
    funding = fetch_funding(symbols, start, end)
    n_prints = sum(len(v) for v in funding.values())
    print(f"funding prints: {n_prints}", flush=True)
    fwd = attach_long_forwards(fwd, grids, funding)
    spreads = load_spreads()

    allres = {"primary": {}, "walk_forward": {}, "fade_sanity": {}}
    for scenario, label in SCENARIOS:
        cells = {}
        for H in HORIZONS:
            net, days, _ = net_series(fwd, "follow_flow", H, scenario, spreads)
            cells[f"follow_flow_{H}s"] = cell_stats(net, days)
        allres["primary"][scenario] = {"label": label, "cells": cells}
        print_table(f"follow_flow  {label}", cells)

    # fade_flow sanity, fees-only only (not for selection)
    fade = {}
    for H in HORIZONS:
        net, days, _ = net_series(fwd, "fade_flow", H, "fees_only", spreads)
        fade[f"fade_flow_{H}s"] = cell_stats(net, days)
    allres["fade_sanity"] = fade
    print_table("fade_flow sanity (fees-only, not a candidate)", fade)

    print(f"\n== Walk-forward follow_flow (IS first 14d / OOS last 7d) ==")
    wf = {}
    for scenario, _ in SCENARIOS:
        wf[scenario] = {}
        for H in HORIZONS:
            net, days, sub = net_series(fwd, "follow_flow", H, scenario, spreads)
            is_m = sub["t"].to_numpy() < cut
            s_is = cell_stats(net[is_m], days[is_m])
            s_oos = cell_stats(net[~is_m], days[~is_m])
            wf[scenario][f"follow_flow_{H}s"] = {"IS": s_is, "OOS": s_oos}
            def fmt(s):
                if s["mean_bps"] is None:
                    return "n/a"
                return (f"mean={s['mean_bps']:+.1f} med={s['median_bps']:+.1f} "
                        f"hit={s['hit_rate']*100:.0f}% "
                        f"CI=[{s['ci95_lo_bps']:+.1f},{s['ci95_hi_bps']:+.1f}] "
                        f"n={s['n_indep']}")
            print(f"  {scenario:<14} {H:>5}s  IS : {fmt(s_is)}")
            print(f"  {scenario:<14} {H:>5}s  OOS: {fmt(s_oos)}")
    allres["walk_forward"] = wf
    allres["meta"] = {
        "horizons_s": HORIZONS, "rule": "follow_flow", "tau": 0,
        "selection_cells": 9, "cumulative_N": 81,
        "funding_prints": n_prints,
        "note": "300s is a known baseline, not a new trial",
    }
    RESULTS.mkdir(exist_ok=True)
    (RESULTS / "phase1c_longer_holds.json").write_text(
        json.dumps(allres, indent=2))
    print(f"\nsaved -> {RESULTS / 'phase1c_longer_holds.json'}")


if __name__ == "__main__":
    # RULES imported only so a typo in fade/follow fails loudly at import
    assert "follow_flow" in RULES
    main()
