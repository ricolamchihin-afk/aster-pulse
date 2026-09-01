"""Phase 1b - OFI-CONDITIONED DIRECTION.

Reuses the exact same reconstructed detector events / forward returns as Phase 1
(`research/data/forward.parquet`, entry = next-bar open, never the signal price),
but supplies the trade DIRECTION from order-flow imbalance instead of the
anomaly's own sign. Four predeclared, mutually-exclusive rules (see LEDGER.md §5):

  follow_flow             ofi>0 -> LONG,  ofi<0 -> SHORT
  fade_flow               ofi>0 -> SHORT, ofi<0 -> LONG
  ofi_confirmed_momentum  take sign(move) only when sign(move)==sign(ofi)
  ofi_divergence_reversal fade sign(move) only when sign(move)!=sign(ofi)

Primary OFI magnitude threshold tau=0. Horizons 30/60/120/300s. Costs:
fees_only (8bp), measured p50 spread, measured p90 spread. Null H0: net
expectancy <= 0. For each rule x horizon x cost scenario this reports the
independent-event mean and median net (bps), hit rate, distribution
percentiles, and a day-cluster bootstrap 95% CI on the mean.

    python research/phase1b_ofi.py
"""
import json
from pathlib import Path

import numpy as np
import pandas as pd

from phase1 import (HORIZONS, LAT_BARS, cost_for, day_bootstrap_ci,
                    independent_mask)
from spread_model import load_spreads
from reconstruct import load_universe

DATA = Path(__file__).parent / "data"
RESULTS = Path(__file__).parent / "results"

RULES = ["follow_flow", "fade_flow", "ofi_confirmed_momentum",
         "ofi_divergence_reversal"]
SCENARIOS = [("fees_only", "fees only (8bp, optimistic)"),
             ("measured_p50", "fees + measured p50 spread"),
             ("measured_p90", "fees + measured p90 spread")]


def sign0(x):
    """Signed indicator with an explicit zero (no trade)."""
    return np.where(x > 0, 1, np.where(x < 0, -1, 0)).astype(int)


def rule_direction(fwd, rule, tau):
    """Return an int array in {-1, 0, +1}: the traded direction per event under
    `rule` and OFI-magnitude threshold `tau`. 0 means NO trade for this event."""
    ofi = fwd["ofi"].to_numpy()
    move_sign = fwd["sign"].to_numpy().astype(int)  # sign(move), from phase1
    ofi_sign = sign0(ofi)
    active = np.abs(ofi) >= tau  # flow-magnitude filter

    if rule == "follow_flow":
        d = ofi_sign.copy()
    elif rule == "fade_flow":
        d = -ofi_sign
    elif rule == "ofi_confirmed_momentum":
        d = np.where(move_sign == ofi_sign, move_sign, 0)
    elif rule == "ofi_divergence_reversal":
        d = np.where(move_sign != ofi_sign, -move_sign, 0)
    else:
        raise ValueError(rule)
    d = d.astype(int)
    d[~active] = 0
    return d


def net_series(fwd, rule, H, scenario, spreads, tau=0.0):
    """Independent-event net returns (log, not bps), the day index, and the
    trades sub-frame, for one rule x horizon x cost cell. Only events with a
    defined direction (d != 0) and a finite forward return are traded; the
    non-overlap independence mask is applied to the taken trades."""
    d = rule_direction(fwd, rule, tau)
    raw = fwd[f"raw_{H}"].to_numpy()
    take = (d != 0) & np.isfinite(raw)
    costs = np.array([cost_for(s, spreads, scenario) for s in fwd["symbol"]])
    sub = fwd[take].copy()
    if len(sub) == 0:
        return np.array([]), np.array([]), sub
    net_all = d[take] * raw[take] - costs[take]
    sub["_net"] = net_all
    hb = H // 30 + LAT_BARS
    imask = independent_mask(sub, hb)
    net_i = net_all[imask]
    days = (sub["t"].to_numpy() // (86400 * 1000))[imask]
    return net_i, days, sub[imask]


def cell_stats(net, days):
    if len(net) == 0:
        return {"n_indep": 0, "mean_bps": None, "median_bps": None,
                "hit_rate": None, "p10_bps": None, "p25_bps": None,
                "p75_bps": None, "p90_bps": None, "ci95_lo_bps": None,
                "ci95_hi_bps": None}
    lo, hi = day_bootstrap_ci(net, days)
    return {
        "n_indep": int(len(net)),
        "mean_bps": float(np.mean(net) * 1e4),
        "median_bps": float(np.median(net) * 1e4),
        "hit_rate": float(np.mean(net > 0)),
        "p10_bps": float(np.percentile(net, 10) * 1e4),
        "p25_bps": float(np.percentile(net, 25) * 1e4),
        "p75_bps": float(np.percentile(net, 75) * 1e4),
        "p90_bps": float(np.percentile(net, 90) * 1e4),
        "ci95_lo_bps": (lo * 1e4) if np.isfinite(lo) else None,
        "ci95_hi_bps": (hi * 1e4) if np.isfinite(hi) else None,
    }


def evaluate(fwd, spreads, scenario, label, tau=0.0):
    out = {}
    for rule in RULES:
        for H in HORIZONS:
            net, days, _ = net_series(fwd, rule, H, scenario, spreads, tau)
            out[f"{rule}_{H}s"] = cell_stats(net, days)
    return {"scenario": label, "tau": tau, "cells": out}


def print_table(res):
    print(f"\n===== cost scenario: {res['scenario']}  (tau={res['tau']}) =====")
    print(f"{'cell':<28}{'n':>6}{'mean':>9}{'median':>9}{'hit%':>7}"
          f"{'CI_lo':>9}{'CI_hi':>9}   verdict")
    for cell, v in res["cells"].items():
        if v["mean_bps"] is None:
            print(f"{cell:<28}{v['n_indep']:>6}   (insufficient)")
            continue
        lo = v["ci95_lo_bps"]
        verdict = "edge>0" if (lo is not None and lo > 0) else "no edge"
        print(f"{cell:<28}{v['n_indep']:>6}{v['mean_bps']:>9.1f}"
              f"{v['median_bps']:>9.1f}{v['hit_rate'] * 100:>7.1f}"
              f"{(lo if lo is not None else float('nan')):>9.1f}"
              f"{(v['ci95_hi_bps'] if v['ci95_hi_bps'] is not None else float('nan')):>9.1f}"
              f"   {verdict}")


def main():
    fwd = pd.read_parquet(DATA / "forward.parquet")
    spreads = load_spreads()
    print(f"events with usable entry: {len(fwd)}")
    RESULTS.mkdir(exist_ok=True)

    allres = {"primary_tau0": {}}
    for scenario, label in SCENARIOS:
        res = evaluate(fwd, spreads, scenario, label, tau=0.0)
        allres["primary_tau0"][scenario] = res
        print_table(res)

    # tau robustness (diagnostic only; not for selection)
    allres["tau_robustness"] = {}
    for tau in (0.3, 0.6):
        allres["tau_robustness"][f"tau_{tau}"] = {}
        for scenario, label in SCENARIOS:
            res = evaluate(fwd, spreads, scenario, label, tau=tau)
            allres["tau_robustness"][f"tau_{tau}"][scenario] = res

    (RESULTS / "phase1b_ofi.json").write_text(json.dumps(allres, indent=2))
    print(f"\nsaved -> {RESULTS / 'phase1b_ofi.json'}")


if __name__ == "__main__":
    main()
