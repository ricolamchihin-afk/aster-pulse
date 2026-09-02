"""Phase 1d - fade names that have already printed +60% over 24 hours.

This is NOT the 30s detector. Event = trailing 24h simple return first crosses
+60% from below. Direction = SHORT. Horizons 1h / 4h / 12h / 24h.

If the +60% sample is too small (n < 20 independent events), stop and report
that. +20% / +40% crossings are counted only as sample-size diagnostics.

    python research/phase1d_24h_fade.py
"""
import json
from pathlib import Path

import numpy as np

from phase1 import (LAT_BARS, cost_for, day_bootstrap_ci, independent_mask,
                    load_open_grids)
from phase1b_ofi import cell_stats
from phase1c_longer_holds import fetch_funding
from reconstruct import BAR_MS, _grid, _load, load_universe
from spread_model import load_spreads

DATA = Path(__file__).parent / "data"
RESULTS = Path(__file__).parent / "results"
LOOKBACK = 24 * 3600 * 1000 // BAR_MS  # 2880 bars
THRESH_PRIMARY = 0.60
DIAG_THRESH = (0.20, 0.40, 0.60)
HORIZONS = [3600, 14400, 43200, 86400]  # 1h, 4h, 12h, 24h
MIN_N = 20
SCENARIOS = [("fees_only", "fees only (8bp)"),
             ("measured_p50", "fees + measured p50 spread"),
             ("measured_p90", "fees + measured p90 spread")]


def crossings(close, has, thresh):
    """Bar indices where trailing 24h simple return first crosses `thresh`
    from below. Requires a real trade on the signal bar."""
    out = []
    in_exc = False
    for i in range(LOOKBACK, len(close)):
        p0, p1 = close[i - LOOKBACK], close[i]
        if not (p0 > 0 and p1 > 0 and np.isfinite(p0) and np.isfinite(p1)):
            continue
        r = p1 / p0 - 1.0
        if (not in_exc) and r >= thresh and has[i]:
            out.append((i, r, p1))
            in_exc = True
        elif in_exc and r < thresh:
            in_exc = False
    return out


def build_events(u, grids, thresh):
    rows = []
    start, end = u["start"], u["end"]
    for row in u["universe"]:
        s = row["symbol"]
        g = grids.get(s)
        if g is None:
            continue
        opn, has = g
        bars = _load(s, start, end)
        if bars is None:
            continue
        close, *_rest = _grid(bars, start, end)
        for i, r24, px in crossings(close, has, thresh):
            rows.append({"symbol": s, "k": i, "t": start + i * BAR_MS,
                         "r24": r24, "close": px})
    return rows


def attach_forwards(events, grids, funding):
    recs = []
    for e in events:
        s, k = e["symbol"], int(e["k"])
        g = grids.get(s)
        if g is None:
            continue
        opn, has = g
        entry_i = k + LAT_BARS
        if entry_i >= len(opn) or not has[entry_i] or not (opn[entry_i] > 0):
            continue
        entry = opn[entry_i]
        entry_t = int(e["t"]) + LAT_BARS * 30_000
        rec = dict(e, entry=entry)
        prints = funding.get(s, [])
        for H in HORIZONS:
            xi = entry_i + H // 30
            if xi >= len(opn) or not has[xi] or not (opn[xi] > 0):
                rec[f"raw_{H}"] = np.nan
                rec[f"fund_{H}"] = 0.0
            else:
                rec[f"raw_{H}"] = np.log(opn[xi] / entry)
                exit_t = entry_t + H * 1000
                paid = sum(fr for ft, fr in prints if entry_t < ft <= exit_t)
                rec[f"fund_{H}"] = paid
        recs.append(rec)
    return recs


def net_series(recs, H, scenario, spreads):
    # SHORT: net = -raw - cost - (-funding) = -raw - cost + funding
    # long pays funding; short receives it
    import pandas as pd
    df = pd.DataFrame(recs)
    raw = df[f"raw_{H}"].to_numpy()
    fund = df[f"fund_{H}"].to_numpy()
    take = np.isfinite(raw)
    costs = np.array([cost_for(s, spreads, scenario) for s in df["symbol"]])
    net = -raw[take] - costs[take] + fund[take]
    sub = df[take].copy()
    sub["_net"] = net
    hb = H // 30 + LAT_BARS
    imask = independent_mask(sub, hb)
    days = (sub["t"].to_numpy() // (86400 * 1000))[imask]
    return net[imask], days, sub[imask]


def print_table(title, cells):
    print(f"\n===== {title} =====")
    print(f"{'cell':<22}{'n':>6}{'mean':>9}{'median':>9}{'hit%':>7}"
          f"{'CI_lo':>9}{'CI_hi':>9}   verdict")
    for name, v in cells.items():
        if v["mean_bps"] is None or v["n_indep"] < MIN_N:
            print(f"{name:<22}{v['n_indep']:>6}   insufficient / stop")
            continue
        lo = v["ci95_lo_bps"]
        verdict = "edge>0" if (lo is not None and lo > 0) else "no edge"
        print(f"{name:<22}{v['n_indep']:>6}{v['mean_bps']:>9.1f}"
              f"{v['median_bps']:>9.1f}{v['hit_rate'] * 100:>7.1f}"
              f"{(lo if lo is not None else float('nan')):>9.1f}"
              f"{(v['ci95_hi_bps'] if v['ci95_hi_bps'] is not None else float('nan')):>9.1f}"
              f"   {verdict}")


def main():
    u = load_universe()
    start, end = u["start"], u["end"]
    grids = load_open_grids(u, start, end)
    print("== sample size (crossings, before forwards) ==")
    counts = {}
    for thr in DIAG_THRESH:
        ev = build_events(u, grids, thr)
        by = {}
        for e in ev:
            by.setdefault(e["symbol"], 0)
            by[e["symbol"]] += 1
        counts[thr] = {"n_crossings": len(ev), "by_symbol": by}
        print(f"  +{thr*100:.0f}% 24h crossings: {len(ev)}  "
              f"symbols={len(by)}  {sorted(by.items(), key=lambda x:-x[1])[:8]}")

    n60 = counts[THRESH_PRIMARY]["n_crossings"]
    out = {"sample": counts, "primary_threshold": THRESH_PRIMARY}
    if n60 < MIN_N:
        print(f"\nSTOP: only {n60} +60% 24h crossings in 21d / 33 names "
              f"(need n>={MIN_N}). The +60% fade thesis cannot be evaluated "
              f"on this sample. Not substituting a lower threshold.")
        out["status"] = "INSUFFICIENT_SAMPLE"
        RESULTS.mkdir(exist_ok=True)
        (RESULTS / "phase1d_24h_fade.json").write_text(json.dumps(out, indent=2))
        return

    print(f"\nfetching funding...", flush=True)
    funding = fetch_funding([r["symbol"] for r in u["universe"]], start, end)
    events = build_events(u, grids, THRESH_PRIMARY)
    recs = attach_forwards(events, grids, funding)
    print(f"+60% events with entry: {len(recs)}")
    spreads = load_spreads()
    primary = {}
    for scenario, label in SCENARIOS:
        cells = {}
        for H in HORIZONS:
            net, days, _ = net_series(recs, H, scenario, spreads)
            cells[f"short_{H}s"] = cell_stats(net, days)
        primary[scenario] = {"label": label, "cells": cells}
        print_table(f"SHORT after +60% 24h  {label}", cells)
    out["status"] = "EVALUATED"
    out["primary"] = primary
    RESULTS.mkdir(exist_ok=True)
    (RESULTS / "phase1d_24h_fade.json").write_text(json.dumps(out, indent=2))
    print(f"\nsaved -> {RESULTS / 'phase1d_24h_fade.json'}")


if __name__ == "__main__":
    main()
