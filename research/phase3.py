"""Phase 3 + search discipline: stress the (already negative) Phase 1 result.

Runs on the predeclared momentum/reversal cells and asks:
  - Walk-forward: does anything hold in-sample vs the untouched out-of-sample?
  - Outliers: does any positive mean survive trimming the largest few events?
  - Liquidity tiers: is there a pocket (very liquid / mid / low) with edge?
  - Time-of-day: any hour-of-day dependence worth noting?
  - Search correction: Deflated Sharpe and White's Reality Check on the trial
    set, using the true number of cells searched.

Reads research/data/forward.parquet (from phase1.py).
"""
import json
from pathlib import Path

import numpy as np
import pandas as pd

from phase1 import (HORIZONS, cost_for, day_bootstrap_ci, independent_mask,
                    LAT_BARS)
from spread_model import load_spreads
from reconstruct import load_universe
from stats_discipline import deflated_sharpe, whites_reality_check, sharpe

DATA = Path(__file__).parent / "data"
RESULTS = Path(__file__).parent / "results"


def net_series(fwd, hyp, H, scenario, spreads):
    dir_sign = fwd["sign"].to_numpy() * (1 if hyp == "momentum" else -1)
    raw = fwd[f"raw_{H}"].to_numpy()
    m = np.isfinite(raw)
    costs = np.array([cost_for(s, spreads, scenario) for s in fwd["symbol"]])
    net = dir_sign[m] * raw[m] - costs[m]
    sub = fwd[m].copy()
    sub["_net"] = net
    hb = H // 30 + LAT_BARS
    imask = independent_mask(sub, hb)
    days = (sub["t"].to_numpy() // (86400 * 1000))
    return net[imask], days[imask], sub[imask]


def summary(net, days):
    if len(net) < 5:
        return None
    lo, hi = day_bootstrap_ci(net, days)
    return {"n": int(len(net)), "mean_bps": float(net.mean() * 1e4),
            "median_bps": float(np.median(net) * 1e4),
            "hit": float((net > 0).mean()),
            "ci_lo_bps": lo * 1e4, "ci_hi_bps": hi * 1e4,
            "sharpe": float(sharpe(net))}


def main():
    fwd = pd.read_parquet(DATA / "forward.parquet")
    spreads = load_spreads()
    u = load_universe()
    qvol = {r["symbol"]: r["qvol24h"] for r in u["universe"]}
    start, end = u["start"], u["end"]
    cut = start + (end - start) * 2 // 3
    cut = (cut // 30_000) * 30_000
    out = {}

    # ---- walk-forward IS/OOS on momentum (fees-only, the most favorable) ----
    print("== Walk-forward (fees_only), momentum ==")
    print(f"IS = first {(cut-start)/86400000:.0f}d, "
          f"OOS = last {(end-cut)/86400000:.0f}d (touched once)")
    wf = {}
    for H in HORIZONS:
        net, days, sub = net_series(fwd, "momentum", H, "fees_only", spreads)
        is_m = sub["t"].to_numpy() < cut
        s_is = summary(net[is_m], days[is_m])
        s_oos = summary(net[~is_m], days[~is_m])
        wf[f"momentum_{H}s"] = {"IS": s_is, "OOS": s_oos}
        f = lambda s: (f"mean={s['mean_bps']:+.1f} med={s['median_bps']:+.1f} "
                       f"hit={s['hit']*100:.0f}% CI=[{s['ci_lo_bps']:+.1f},"
                       f"{s['ci_hi_bps']:+.1f}] n={s['n']}") if s else "n/a"
        print(f"  {H:>3}s  IS : {f(s_is)}")
        print(f"       OOS: {f(s_oos)}")
    out["walk_forward"] = wf

    # ---- outlier sensitivity: momentum_300s fees-only, trim top winners ----
    print("\n== Outlier sensitivity (momentum_300s, fees_only) ==")
    net, days, sub = net_series(fwd, "momentum", 300, "fees_only", spreads)
    order = np.argsort(net)
    outl = {"full": summary(net, days)}
    for k in [1, 3, 5, 10]:
        keep = np.ones(len(net), bool)
        keep[order[-k:]] = False  # drop k biggest winners
        outl[f"drop_top{k}"] = summary(net[keep], days[keep])
    for name, s in outl.items():
        if s:
            print(f"  {name:<10} mean={s['mean_bps']:+.1f}bps "
                  f"median={s['median_bps']:+.1f} CI=[{s['ci_lo_bps']:+.1f},"
                  f"{s['ci_hi_bps']:+.1f}] n={s['n']}")
    out["outlier_momentum_300s"] = outl

    # ---- liquidity tiers (momentum, fees_only) ----
    print("\n== Liquidity tiers (momentum, fees_only) ==")
    tiers = {"T1_>=20M": lambda q: q >= 20e6,
             "T2_5-20M": lambda q: 5e6 <= q < 20e6,
             "T3_2-5M": lambda q: 2e6 <= q < 5e6}
    tierres = {}
    for H in [60, 300]:
        net, days, sub = net_series(fwd, "momentum", H, "fees_only", spreads)
        q = sub["symbol"].map(qvol).to_numpy()
        for tname, fn in tiers.items():
            mask = np.array([fn(x) for x in q])
            s = summary(net[mask], days[mask])
            tierres[f"{tname}_{H}s"] = s
            if s:
                print(f"  {tname:<10} {H:>3}s mean={s['mean_bps']:+.1f}bps "
                      f"hit={s['hit']*100:.0f}% CI=[{s['ci_lo_bps']:+.1f},"
                      f"{s['ci_hi_bps']:+.1f}] n={s['n']}")
    out["tiers"] = tierres

    # ---- time-of-day (momentum_300s fees_only) ----
    net, days, sub = net_series(fwd, "momentum", 300, "fees_only", spreads)
    hours = ((sub["t"].to_numpy() // (3600 * 1000)) % 24)
    tod = {}
    for h in range(0, 24, 6):
        mask = (hours >= h) & (hours < h + 6)
        s = summary(net[mask], days[mask])
        tod[f"{h:02d}-{h+6:02d}UTC"] = s
    out["time_of_day_mom300"] = tod

    # ---- search correction: DSR + White's on the 24 predeclared cells ----
    print("\n== Search correction (DSR / White's) ==")
    cells_ret, cells_days = {}, {}
    srs = []
    for scenario in ["fees_only", "measured_p50", "measured_p90"]:
        for hyp in ["momentum", "reversal"]:
            for H in HORIZONS:
                net, days, sub = net_series(fwd, hyp, H, scenario, spreads)
                key = f"{hyp}_{H}s_{scenario}"
                cells_ret[key] = net
                cells_days[key] = days
                srs.append(sharpe(net))
    n_trials = len(srs)
    var_sr = float(np.var(srs, ddof=1))
    # best cell by mean
    best = max(cells_ret, key=lambda k: cells_ret[k].mean()
               if len(cells_ret[k]) else -1e9)
    dsr, sr, sr0 = deflated_sharpe(cells_ret[best], n_trials, var_sr)
    wp = whites_reality_check(cells_ret, cells_days, n_boot=3000)
    print(f"  trials N={n_trials}, var(SR across trials)={var_sr:.4f}")
    print(f"  best cell by mean: {best}  (mean="
          f"{cells_ret[best].mean()*1e4:+.1f}bps, per-trade SR={sr:+.3f})")
    print(f"  deflated benchmark SR0={sr0:.3f}  ->  DSR={dsr:.3f} "
          f"(pass needs >0.95)")
    print(f"  White's Reality Check p-value={wp:.3f} "
          f"(high = best cell consistent with luck)")
    out["search_correction"] = {"n_trials": n_trials, "var_sr": var_sr,
                                 "best_cell": best, "best_sr": sr,
                                 "sr0": sr0, "dsr": dsr, "whites_p": wp}

    RESULTS.mkdir(exist_ok=True)
    (RESULTS / "phase3.json").write_text(json.dumps(out, indent=2, default=str))
    print(f"\nsaved -> {RESULTS / 'phase3.json'}")


if __name__ == "__main__":
    main()
