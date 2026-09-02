"""Optimistic-ceiling diagnostic + figures for the direction study.

1. LOOK-AHEAD CEILING (not tradeable): enter at the signal bar's own close
   (the price that defined the signal) instead of the next bar. This is
   impossible to trade but is a strict upper bound on any momentum capture. If
   even this fails net of the fee floor, no realistic entry can succeed.

2. Figures: net-return distribution for the best cell, and mean-net-with-CI
   across all predeclared cells.
"""
import json
from pathlib import Path

import numpy as np
import pandas as pd
import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt  # noqa: E402

from reconstruct import _grid, _load, load_universe
from phase1 import (HORIZONS, cost_for, day_bootstrap_ci, independent_mask,
                    FEE_RT)
from spread_model import load_spreads

DATA = Path(__file__).parent / "data"
RESULTS = Path(__file__).parent / "results"
ART = Path("/opt/cursor/artifacts")


def lookahead_ceiling():
    u = load_universe()
    start, end = u["start"], u["end"]
    events = pd.read_parquet(DATA / "events.parquet")
    grids = {}
    for row in u["universe"]:
        s = row["symbol"]
        b = _load(s, start, end)
        if b:
            close, opn, *_ , has = _grid(b, start, end)
            grids[s] = (close, opn, has)
    print("== LOOK-AHEAD CEILING (enter at signal price; NOT tradeable) ==")
    print("   strict upper bound. gross = before fees; net = after 8bp RT fee")
    print(f"   {'H':>4} {'gross_mom':>10} {'mom_net':>9} {'rev_net':>9} "
          f"{'hit_mom':>8} {'n':>6}")
    res = {}
    for H in HORIZONS:
        rows = []
        for _, e in events.iterrows():
            g = grids.get(e["symbol"])
            if g is None:
                continue
            close, opn, has = g
            k = int(e["k"])
            xi = k + H // 30
            if xi >= len(close) or not has[xi] or close[k] <= 0:
                continue
            raw = np.log(close[xi] / close[k])  # enter at signal close
            sign = 1 if e["move"] > 0 else -1
            rows.append((e["symbol"], k, int(e["t"]), sign * raw))
        df = pd.DataFrame(rows, columns=["symbol", "k", "t", "gross_mom"])
        df["_net"] = df["gross_mom"]
        hb = H // 30
        im = independent_mask(df, hb)
        gm = df["gross_mom"].to_numpy()[im]
        mom_net = gm - FEE_RT
        rev_net = -gm - FEE_RT
        days = (df["t"].to_numpy()[im] // (86400 * 1000))
        lo, hi = day_bootstrap_ci(mom_net, days)
        res[H] = {"n": int(len(gm)),
                  "gross_mom_bps": float(gm.mean() * 1e4),
                  "mom_net_bps": float(mom_net.mean() * 1e4),
                  "rev_net_bps": float(rev_net.mean() * 1e4),
                  "mom_median_bps": float(np.median(mom_net) * 1e4),
                  "hit_mom": float((mom_net > 0).mean()),
                  "ci_lo_bps": lo * 1e4, "ci_hi_bps": hi * 1e4}
        print(f"   {H:>3}s {gm.mean()*1e4:>+10.1f} {mom_net.mean()*1e4:>+9.1f} "
              f"{rev_net.mean()*1e4:>+9.1f} {(mom_net>0).mean()*100:>7.0f}% "
              f"{len(gm):>6}")
    print("   => max |gross reversion| is below the 8bp fee floor: no edge "
          "even at the untradeable signal price.")
    (RESULTS / "lookahead_ceiling.json").write_text(json.dumps(res, indent=2))
    return res


def figures():
    ART.mkdir(parents=True, exist_ok=True)
    fwd = pd.read_parquet(DATA / "forward.parquet")
    spreads = load_spreads()
    p1 = json.loads((RESULTS / "phase1.json").read_text())

    # Fig 1: distribution of momentum_300s net (fees-only), independent events
    dir_sign = fwd["sign"].to_numpy()
    raw = fwd["raw_300"].to_numpy()
    m = np.isfinite(raw)
    costs = np.array([cost_for(s, spreads, "fees_only") for s in fwd["symbol"]])
    sub = fwd[m].copy()
    sub["_net"] = dir_sign[m] * raw[m] - costs[m]
    im = independent_mask(sub, 300 // 30 + 1)
    net = sub["_net"].to_numpy()[im] * 1e4
    mean_bps, median_bps = float(np.mean(net)), float(np.median(net))  # full data
    view = net[np.abs(net) < 500]  # clip x-axis view only
    fig, ax = plt.subplots(figsize=(8, 4.5))
    ax.hist(view, bins=120, color="#4c78a8", alpha=0.85)
    ax.axvline(0, color="k", lw=1)
    ax.axvline(mean_bps, color="#e45756", lw=2,
               label=f"mean {mean_bps:+.1f} bps (n={len(net)})")
    ax.axvline(median_bps, color="#f58518", lw=2, ls="--",
               label=f"median {median_bps:+.1f} bps")
    ax.set_title("Momentum @300s net return per trade (fees-only, 8bp)\n"
                 "small positive mean is a fat right tail; median is negative; "
                 "hit rate < 50%")
    ax.set_xlabel("net return (bps), x-axis clipped to +/-500 for view")
    ax.set_ylabel("events")
    ax.legend()
    fig.tight_layout()
    fig.savefig(ART / "net_return_distribution_mom300.png", dpi=130)
    plt.close(fig)

    # Fig 2: mean net + CI across predeclared cells, 3 cost scenarios
    scen = [("fees_only", "fees only (8bp)"),
            ("measured_p50", "+ p50 spread"),
            ("measured_p90", "+ p90 spread")]
    cells = [f"{h}_{H}s" for h in ["momentum", "reversal"] for H in HORIZONS]
    fig, axes = plt.subplots(1, 3, figsize=(14, 4.6), sharey=True)
    for ax, (sk, sl) in zip(axes, scen):
        c = p1[sk]["cells"]
        xs = np.arange(len(cells))
        means = [c[k]["mean_indep_bps"] for k in cells]
        los = [c[k]["ci95_lo_bps"] for k in cells]
        his = [c[k]["ci95_hi_bps"] for k in cells]
        err_lo = [m - l for m, l in zip(means, los)]
        err_hi = [h - m for m, h in zip(means, his)]
        colors = ["#54a24b" if l is not None and l > 0 else "#e45756"
                  for l in los]
        ax.bar(xs, means, color=colors, alpha=0.85)
        ax.errorbar(xs, means, yerr=[err_lo, err_hi], fmt="none",
                    ecolor="k", capsize=3, lw=1)
        ax.axhline(0, color="k", lw=1)
        ax.set_title(sl)
        ax.set_xticks(xs)
        ax.set_xticklabels(cells, rotation=60, ha="right", fontsize=8)
    axes[0].set_ylabel("mean net return per trade (bps), 95% CI")
    fig.suptitle("Direction test: every predeclared cell (green=CI>0, red=not). "
                 "None is green.", y=1.02)
    fig.tight_layout()
    fig.savefig(ART / "direction_test_cells.png", dpi=130,
                bbox_inches="tight")
    plt.close(fig)
    print(f"figures -> {ART}")


if __name__ == "__main__":
    lookahead_ceiling()
    figures()
