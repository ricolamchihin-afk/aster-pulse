"""Phase 1 - DIRECTION.

Take the reconstructed detector events and test the two PREDECLARED hypotheses
(momentum / reversal) at horizons 30/60/120/300s, on executable trade prices,
net of measured costs. Entry is the NEXT bar's open after the signal (never the
signal's own price); exit is `horizon` seconds after entry.

Outputs mean AND median net return, hit rate, full-distribution percentiles, the
independent-event count, and a day-cluster bootstrap 95% CI on mean net return,
under several cost scenarios (fees-only, measured p50 spread, measured p90
spread). The null H0: net expectancy <= 0.

    python research/phase1.py
"""
import json
from pathlib import Path

import numpy as np
import pandas as pd

from reconstruct import _grid, _load, load_universe, BAR_MS
from spread_model import load_spreads

DATA = Path(__file__).parent / "data"
RESULTS = Path(__file__).parent / "results"
HORIZONS = [30, 60, 120, 300]
FEE_RT = 0.0008           # 0.04% taker * 2 sides
SLIP = 0.0000             # extra impact; varied in sensitivity separately
LAT_BARS = 1              # entry at next 30s bar open (>= real ms latency)


def load_open_grids(universe, start, end):
    grids = {}
    for row in universe["universe"]:
        s = row["symbol"]
        bars = _load(s, start, end)
        if bars is None:
            continue
        close, opn, vq, buyvq, sellvq, has_trade = _grid(bars, start, end)
        grids[s] = (opn, has_trade)
    return grids


def forward_returns(events, grids, start, end):
    """Attach raw forward log-returns for each horizon (before costs)."""
    rows = []
    for _, e in events.iterrows():
        s = e["symbol"]
        g = grids.get(s)
        if g is None:
            continue
        opn, has = g
        k = int(e["k"])
        entry_i = k + LAT_BARS
        if entry_i >= len(opn) or not has[entry_i] or not (opn[entry_i] > 0):
            continue
        entry = opn[entry_i]
        rec = {"symbol": s, "k": k, "t": int(e["t"]), "move": e["move"],
               "z": e["z"], "beta": e["beta"], "burst": e["burst"],
               "ofi": e["ofi"], "sign": 1 if e["move"] > 0 else -1,
               "entry": entry}
        ok = True
        for H in HORIZONS:
            xi = entry_i + H // 30
            if xi >= len(opn) or not has[xi] or not (opn[xi] > 0):
                rec[f"raw_{H}"] = np.nan
            else:
                rec[f"raw_{H}"] = np.log(opn[xi] / entry)
        rows.append(rec)
    return pd.DataFrame(rows)


def independent_mask(df, horizon_bars):
    """Keep events whose forward windows do not overlap, per symbol
    (greedy, chronological), so observations are not double-counted."""
    keep = np.zeros(len(df), dtype=bool)
    last_k = {}
    for i, (_, e) in enumerate(df.sort_values(["symbol", "k"]).iterrows()):
        s, k = e["symbol"], int(e["k"])
        if s not in last_k or k - last_k[s] >= horizon_bars:
            keep_idx = e.name
            keep[df.index.get_loc(keep_idx)] = True
            last_k[s] = k
    return keep


def day_bootstrap_ci(net, days, n_boot=5000, seed=0):
    """95% CI on the mean, resampling whole days (event clusters)."""
    net = np.asarray(net)
    days = np.asarray(days)
    uniq = np.unique(days)
    if len(uniq) < 2 or len(net) < 5:
        return (np.nan, np.nan)
    rng = np.random.default_rng(seed)
    by = {d: net[days == d] for d in uniq}
    means = np.empty(n_boot)
    for b in range(n_boot):
        pick = rng.choice(uniq, size=len(uniq), replace=True)
        vals = np.concatenate([by[d] for d in pick])
        means[b] = vals.mean()
    return (float(np.percentile(means, 2.5)),
            float(np.percentile(means, 97.5)))


def cost_for(symbol, spreads, scenario):
    fee = FEE_RT
    sp = spreads.get(symbol, {})
    if scenario == "fees_only":
        spread = 0.0
    elif scenario == "measured_p50":
        spread = (sp.get("p50") or 0.0) / 1e4
    elif scenario == "measured_p90":
        spread = (sp.get("p90") or 0.0) / 1e4
    else:
        raise ValueError(scenario)
    return fee + spread


def evaluate(fwd, spreads, scenario, label):
    out = {}
    days = (fwd["t"].to_numpy() // (86400 * 1000))
    costs = np.array([cost_for(s, spreads, scenario) for s in fwd["symbol"]])
    for hyp in ["momentum", "reversal"]:
        dir_sign = fwd["sign"].to_numpy() * (1 if hyp == "momentum" else -1)
        for H in HORIZONS:
            raw = fwd[f"raw_{H}"].to_numpy()
            m = np.isfinite(raw)
            net = dir_sign[m] * raw[m] - costs[m]
            d = days[m]
            hb = H // 30 + LAT_BARS
            sub = fwd[m].copy()
            sub["_net"] = net
            imask = independent_mask(sub, hb)
            net_i = net[imask]
            d_i = d[imask]
            lo, hi = day_bootstrap_ci(net_i, d_i)
            out[f"{hyp}_{H}s"] = {
                "n_raw": int(m.sum()),
                "n_indep": int(imask.sum()),
                "mean_bps": float(np.mean(net) * 1e4) if len(net) else None,
                "median_bps": float(np.median(net) * 1e4) if len(net) else None,
                "mean_indep_bps": float(np.mean(net_i) * 1e4) if len(net_i) else None,
                "hit_rate": float(np.mean(net_i > 0)) if len(net_i) else None,
                "p10_bps": float(np.percentile(net_i, 10) * 1e4) if len(net_i) else None,
                "p90_bps": float(np.percentile(net_i, 90) * 1e4) if len(net_i) else None,
                "ci95_lo_bps": lo * 1e4 if np.isfinite(lo) else None,
                "ci95_hi_bps": hi * 1e4 if np.isfinite(hi) else None,
            }
    return {"scenario": label, "cells": out}


def print_table(res):
    print(f"\n===== cost scenario: {res['scenario']} =====")
    hdr = (f"{'cell':<16}{'n_ind':>6}{'mean':>9}{'median':>9}"
           f"{'hit%':>7}{'CI95_lo':>9}{'CI95_hi':>9}   verdict")
    print(hdr)
    for cell, v in res["cells"].items():
        if v["mean_indep_bps"] is None:
            print(f"{cell:<16}{v['n_indep']:>6}   (insufficient)")
            continue
        lo = v["ci95_lo_bps"]
        verdict = "edge>0" if (lo is not None and lo > 0) else "no edge"
        print(f"{cell:<16}{v['n_indep']:>6}{v['mean_indep_bps']:>9.1f}"
              f"{v['median_bps']:>9.1f}{v['hit_rate'] * 100:>7.1f}"
              f"{(lo if lo is not None else float('nan')):>9.1f}"
              f"{(v['ci95_hi_bps'] if v['ci95_hi_bps'] is not None else float('nan')):>9.1f}"
              f"   {verdict}")


def main():
    u = load_universe()
    start, end = u["start"], u["end"]
    events = pd.read_parquet(DATA / "events.parquet")
    print(f"events loaded: {len(events)}")
    grids = load_open_grids(u, start, end)
    fwd = forward_returns(events, grids, start, end)
    print(f"events with usable entry price: {len(fwd)}")
    fwd.to_parquet(DATA / "forward.parquet")
    spreads = load_spreads()

    RESULTS.mkdir(exist_ok=True)
    allres = {}
    for scenario, label in [("fees_only", "fees only (8bp, optimistic)"),
                            ("measured_p50", "fees + measured p50 spread"),
                            ("measured_p90", "fees + measured p90 spread")]:
        res = evaluate(fwd, spreads, scenario, label)
        allres[scenario] = res
        print_table(res)
    (RESULTS / "phase1.json").write_text(json.dumps(allres, indent=2))
    print(f"\nsaved -> {RESULTS / 'phase1.json'}")


if __name__ == "__main__":
    main()
