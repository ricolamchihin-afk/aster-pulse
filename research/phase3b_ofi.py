"""Phase 3b - robustness + search correction for the OFI direction study.

Stresses the Phase 1b OFI cells the same way phase3.py stressed the original
momentum/reversal cells:
  - Walk-forward: 14d in-sample / 7d UNTOUCHED out-of-sample (touched once).
  - Outlier trim of the best cell (does a +mean survive dropping top winners?).
  - Liquidity tiers.
  - OFI-decile monotonicity of GROSS forward return (the core of the thesis).
  - tau thresholds (read from phase1b_ofi.json).
  - Search correction: Deflated Sharpe + White's Reality Check on the CUMULATIVE
    trial count (24 prior momentum/reversal cells + 48 new OFI cells = 72).

Also selects the most-promising predeclared rule as CANDIDATE and writes
research/results/locked_rule.json.

    python research/phase3b_ofi.py
"""
import json
from pathlib import Path

import numpy as np
import pandas as pd
import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt  # noqa: E402

from phase1 import HORIZONS, LAT_BARS, cost_for, day_bootstrap_ci, independent_mask
from phase1b_ofi import RULES, net_series as ofi_net_series
from spread_model import load_spreads
from reconstruct import load_universe
from stats_discipline import deflated_sharpe, whites_reality_check, sharpe

DATA = Path(__file__).parent / "data"
RESULTS = Path(__file__).parent / "results"
ART = Path("/opt/cursor/artifacts")


def summary(net, days):
    if len(net) < 5:
        return None
    lo, hi = day_bootstrap_ci(net, days)
    return {"n": int(len(net)), "mean_bps": float(net.mean() * 1e4),
            "median_bps": float(np.median(net) * 1e4),
            "hit": float((net > 0).mean()),
            "ci_lo_bps": lo * 1e4, "ci_hi_bps": hi * 1e4,
            "sharpe": float(sharpe(net))}


def prior_net_series(fwd, hyp, H, scenario, spreads):
    """Reproduce a Phase-1 momentum/reversal cell's independent net series
    (for the cumulative search correction)."""
    dir_sign = fwd["sign"].to_numpy() * (1 if hyp == "momentum" else -1)
    raw = fwd[f"raw_{H}"].to_numpy()
    m = np.isfinite(raw)
    costs = np.array([cost_for(s, spreads, scenario) for s in fwd["symbol"]])
    net = dir_sign[m] * raw[m] - costs[m]
    sub = fwd[m].copy()
    sub["_net"] = net
    hb = H // 30 + LAT_BARS
    imask = independent_mask(sub, hb)
    days = (sub["t"].to_numpy() // (86400 * 1000))[imask]
    return net[imask], days


def ofi_decile_monotonicity(fwd):
    """The CORE of the thesis: does GROSS forward return rise monotonically with
    OFI? Bucket events into OFI deciles and plot mean gross forward log-return
    (bps) per decile per horizon. If the thesis held, follow-flow would show a
    clean upward slope (buy-flow -> up, sell-flow -> down)."""
    ofi = fwd["ofi"].to_numpy()
    edges = np.quantile(ofi, np.linspace(0, 1, 11))
    edges[0] -= 1e-9
    dec = np.clip(np.digitize(ofi, edges[1:-1]), 0, 9)
    res = {}
    for H in HORIZONS:
        raw = fwd[f"raw_{H}"].to_numpy()
        means, ofis, ns = [], [], []
        for d in range(10):
            mask = (dec == d) & np.isfinite(raw)
            means.append(float(np.mean(raw[mask]) * 1e4) if mask.sum() else np.nan)
            ofis.append(float(np.mean(ofi[mask])) if mask.sum() else np.nan)
            ns.append(int(mask.sum()))
        res[f"H{H}"] = {"decile_ofi": ofis, "gross_fwd_bps": means, "n": ns}
    # Spearman rank correlation of decile mean OFI vs decile mean gross return
    from scipy.stats import spearmanr
    for H in HORIZONS:
        o = np.array(res[f"H{H}"]["decile_ofi"])
        g = np.array(res[f"H{H}"]["gross_fwd_bps"])
        ok = np.isfinite(o) & np.isfinite(g)
        rho, p = spearmanr(o[ok], g[ok])
        res[f"H{H}"]["spearman_rho"] = float(rho)
        res[f"H{H}"]["spearman_p"] = float(p)
    return res


def decile_figure(dec):
    ART.mkdir(parents=True, exist_ok=True)
    fig, ax = plt.subplots(figsize=(8.5, 5))
    colors = ["#4c78a8", "#54a24b", "#e45756", "#b279a2"]
    for (H, c) in zip(HORIZONS, colors):
        d = dec[f"H{H}"]
        ax.plot(d["decile_ofi"], d["gross_fwd_bps"], "-o", color=c,
                label=f"{H}s  (Spearman rho={d['spearman_rho']:+.2f}, "
                      f"p={d['spearman_p']:.2f})")
    ax.axhline(0, color="k", lw=1)
    ax.set_xlabel("mean signed OFI in decile  (sell-flow  <-  0  ->  buy-flow)")
    ax.set_ylabel("mean GROSS forward log-return (bps), before costs")
    ax.set_title("OFI-decile monotonicity (core thesis test)\n"
                 "if flow predicted direction, this would slope up and clear the "
                 "8bp fee band")
    ax.axhspan(-8, 8, color="grey", alpha=0.12, label="+/-8bp fee band")
    ax.legend(fontsize=8)
    ax.grid(alpha=0.25)
    fig.tight_layout()
    out = ART / "ofi_decile_monotonicity.png"
    fig.savefig(out, dpi=130)
    plt.close(fig)
    print(f"figure -> {out}")


def rule_cells_figure(p1b):
    """Bar chart of mean net + 95% CI for all 16 OFI cells, 3 cost scenarios."""
    ART.mkdir(parents=True, exist_ok=True)
    scen = [("fees_only", "fees only (8bp)"),
            ("measured_p50", "+ p50 spread"),
            ("measured_p90", "+ p90 spread")]
    cells = [f"{r}_{H}s" for r in RULES for H in HORIZONS]
    short = [c.replace("follow_flow", "follow").replace("fade_flow", "fade")
             .replace("ofi_confirmed_momentum", "confmom")
             .replace("ofi_divergence_reversal", "divrev") for c in cells]
    fig, axes = plt.subplots(1, 3, figsize=(16, 5), sharey=True)
    for ax, (sk, sl) in zip(axes, scen):
        c = p1b["primary_tau0"][sk]["cells"]
        xs = np.arange(len(cells))
        means = [c[k]["mean_bps"] for k in cells]
        los = [c[k]["ci95_lo_bps"] for k in cells]
        his = [c[k]["ci95_hi_bps"] for k in cells]
        err_lo = [m - l for m, l in zip(means, los)]
        err_hi = [h - m for m, h in zip(means, his)]
        colors = ["#54a24b" if (l is not None and l > 0) else "#e45756"
                  for l in los]
        ax.bar(xs, means, color=colors, alpha=0.85)
        ax.errorbar(xs, means, yerr=[err_lo, err_hi], fmt="none",
                    ecolor="k", capsize=3, lw=1)
        ax.axhline(0, color="k", lw=1)
        ax.set_title(sl)
        ax.set_xticks(xs)
        ax.set_xticklabels(short, rotation=60, ha="right", fontsize=8)
    axes[0].set_ylabel("mean net return per trade (bps), 95% CI")
    fig.suptitle("OFI direction test: all 16 cells x 3 cost scenarios "
                 "(green=CI>0, red=not). None is green.", y=1.02)
    fig.tight_layout()
    out = ART / "ofi_direction_test_cells.png"
    fig.savefig(out, dpi=130, bbox_inches="tight")
    plt.close(fig)
    print(f"figure -> {out}")


def main():
    fwd = pd.read_parquet(DATA / "forward.parquet")
    spreads = load_spreads()
    u = load_universe()
    qvol = {r["symbol"]: r["qvol24h"] for r in u["universe"]}
    start, end = u["start"], u["end"]
    cut = start + 14 * 86400 * 1000  # 14d IS / 7d OOS
    cut = (cut // 30_000) * 30_000
    out = {}
    p1b = json.loads((RESULTS / "phase1b_ofi.json").read_text())

    # ---- candidate selection: highest fees-only IS mean net at its best horizon
    print("== Candidate selection (highest in-sample mean net, fees-only) ==")
    cand = {"rule": None, "H": None, "is_mean_bps": -1e9}
    is_table = {}
    for rule in RULES:
        for H in HORIZONS:
            net, days, sub = ofi_net_series(fwd, rule, H, "fees_only", spreads)
            is_m = sub["t"].to_numpy() < cut
            s_is = summary(net[is_m], days[is_m])
            is_table[f"{rule}_{H}s"] = s_is
            if s_is and s_is["mean_bps"] > cand["is_mean_bps"]:
                cand = {"rule": rule, "H": H, "is_mean_bps": s_is["mean_bps"]}
    for k, s in is_table.items():
        if s:
            print(f"  {k:<28} IS mean={s['mean_bps']:+.1f}bps CI="
                  f"[{s['ci_lo_bps']:+.1f},{s['ci_hi_bps']:+.1f}] n={s['n']}")
    print(f"  -> candidate: {cand['rule']} @ {cand['H']}s "
          f"(IS mean {cand['is_mean_bps']:+.1f}bps)")
    out["candidate_is_selection"] = {"table": is_table, "chosen": cand}

    # ---- walk-forward: candidate rule, all horizons, fees-only + measured p50
    print("\n== Walk-forward IS(14d)/untouched-OOS(7d), candidate rule ==")
    wf = {}
    for scenario in ("fees_only", "measured_p50"):
        wf[scenario] = {}
        for H in HORIZONS:
            net, days, sub = ofi_net_series(fwd, cand["rule"], H, scenario, spreads)
            is_m = sub["t"].to_numpy() < cut
            s_is = summary(net[is_m], days[is_m])
            s_oos = summary(net[~is_m], days[~is_m])
            wf[scenario][f"{cand['rule']}_{H}s"] = {"IS": s_is, "OOS": s_oos}
            f = lambda s: (f"mean={s['mean_bps']:+.1f} med={s['median_bps']:+.1f} "
                           f"hit={s['hit']*100:.0f}% CI=[{s['ci_lo_bps']:+.1f},"
                           f"{s['ci_hi_bps']:+.1f}] n={s['n']}") if s else "n/a"
            print(f"  [{scenario}] {H:>3}s IS : {f(s_is)}")
            print(f"                   OOS: {f(s_oos)}")
    out["walk_forward"] = wf

    # ---- outlier sensitivity on the best fees-only cell (candidate rule/H)
    print("\n== Outlier sensitivity (candidate best cell, fees_only) ==")
    net, days, sub = ofi_net_series(fwd, cand["rule"], cand["H"], "fees_only", spreads)
    order = np.argsort(net)
    outl = {"full": summary(net, days)}
    for k in [1, 3, 5, 10]:
        keep = np.ones(len(net), bool)
        keep[order[-k:]] = False
        outl[f"drop_top{k}"] = summary(net[keep], days[keep])
    for name, s in outl.items():
        if s:
            print(f"  {name:<10} mean={s['mean_bps']:+.1f}bps median="
                  f"{s['median_bps']:+.1f} CI=[{s['ci_lo_bps']:+.1f},"
                  f"{s['ci_hi_bps']:+.1f}] n={s['n']}")
    out["outlier_candidate"] = outl

    # ---- liquidity tiers (candidate rule, fees_only) ----
    print("\n== Liquidity tiers (candidate rule, fees_only) ==")
    tiers = {"T1_>=20M": lambda q: q >= 20e6,
             "T2_5-20M": lambda q: 5e6 <= q < 20e6,
             "T3_2-5M": lambda q: 2e6 <= q < 5e6}
    tierres = {}
    for H in [60, 300]:
        net, days, sub = ofi_net_series(fwd, cand["rule"], H, "fees_only", spreads)
        q = sub["symbol"].map(qvol).to_numpy()
        for tname, fn in tiers.items():
            mask = np.array([fn(x) for x in q])
            s = summary(net[mask], days[mask])
            tierres[f"{tname}_{H}s"] = s
            if s:
                print(f"  {tname:<10} {H:>3}s mean={s['mean_bps']:+.1f}bps hit="
                      f"{s['hit']*100:.0f}% CI=[{s['ci_lo_bps']:+.1f},"
                      f"{s['ci_hi_bps']:+.1f}] n={s['n']}")
    out["tiers"] = tierres

    # ---- OFI-decile monotonicity (core thesis) ----
    print("\n== OFI-decile monotonicity of GROSS forward return ==")
    dec = ofi_decile_monotonicity(fwd)
    for H in HORIZONS:
        d = dec[f"H{H}"]
        print(f"  {H:>3}s Spearman(OFI-decile, gross fwd)="
              f"{d['spearman_rho']:+.2f} p={d['spearman_p']:.3f}  "
              f"gross by decile(bps): "
              f"{[round(x,1) for x in d['gross_fwd_bps']]}")
    out["ofi_decile"] = dec
    decile_figure(dec)
    rule_cells_figure(p1b)

    # ---- search correction: DSR + White's on cumulative N=72 ----
    print("\n== Search correction (DSR / White's) on CUMULATIVE N=72 ==")
    cells_ret, cells_days = {}, {}
    srs = []
    # 24 prior momentum/reversal cells
    for scenario in ["fees_only", "measured_p50", "measured_p90"]:
        for hyp in ["momentum", "reversal"]:
            for H in HORIZONS:
                net, days = prior_net_series(fwd, hyp, H, scenario, spreads)
                key = f"PRIOR_{hyp}_{H}s_{scenario}"
                cells_ret[key] = net
                cells_days[key] = days
                srs.append(sharpe(net))
    # 48 new OFI cells
    for scenario in ["fees_only", "measured_p50", "measured_p90"]:
        for rule in RULES:
            for H in HORIZONS:
                net, days, _ = ofi_net_series(fwd, rule, H, scenario, spreads)
                key = f"OFI_{rule}_{H}s_{scenario}"
                cells_ret[key] = net
                cells_days[key] = days
                srs.append(sharpe(net))
    n_trials = len(srs)
    var_sr = float(np.var(srs, ddof=1))
    best = max((k for k in cells_ret if len(cells_ret[k]) > 0),
               key=lambda k: cells_ret[k].mean())
    dsr, sr, sr0 = deflated_sharpe(cells_ret[best], n_trials, var_sr)
    wp = whites_reality_check(cells_ret, cells_days, n_boot=3000)
    print(f"  cumulative trials N={n_trials}  var(SR across trials)={var_sr:.4f}")
    print(f"  best cell by mean: {best} (mean="
          f"{cells_ret[best].mean()*1e4:+.1f}bps, per-trade SR={sr:+.3f})")
    print(f"  deflated benchmark SR0={sr0:.3f} -> DSR={dsr:.3f} (pass needs >0.95)")
    print(f"  White's Reality Check p={wp:.3f} (high = best cell consistent with luck)")
    out["search_correction"] = {"n_trials": n_trials, "var_sr": var_sr,
                                 "best_cell": best,
                                 "best_mean_bps": float(cells_ret[best].mean() * 1e4),
                                 "best_sr": sr, "sr0": sr0, "dsr": dsr,
                                 "whites_p": wp}

    RESULTS.mkdir(exist_ok=True)
    (RESULTS / "phase3b_ofi.json").write_text(json.dumps(out, indent=2, default=str))
    print(f"\nsaved -> {RESULTS / 'phase3b_ofi.json'}")
    return out, cand


if __name__ == "__main__":
    main()
