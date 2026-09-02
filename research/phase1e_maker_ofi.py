"""Phase 1e - revised thesis: maker-only extreme OFI on liquid books.

Predeclared in LEDGER.md §12 BEFORE any forward return was computed.

    python research/phase1e_maker_ofi.py
"""
import json
from pathlib import Path

import numpy as np
import pandas as pd

from phase1 import LAT_BARS, day_bootstrap_ci, independent_mask
from phase1b_ofi import cell_stats
from reconstruct import BAR_MS, _grid, _load, load_universe

DATA = Path(__file__).parent / "data"
RESULTS = Path(__file__).parent / "results"

# Frozen at predeclaration (LEDGER §12). Do not edit after seeing results.
LIQUID = [
    "BTCUSDT", "ETHUSDT", "BTCUSD1", "XAUUSD1", "ETHUSD1", "SOLUSDT",
    "CLUSDT", "CLUSD1", "ASTERUSDT", "XAGUSDT", "SNDKUSD1", "SOLUSD1",
    "SPCXUSD1", "MUUSD1", "SKHYNIXUSD1",
]
TAU = 0.6
RUNNER_24H = 0.40
MIN_FILL_USD = 500.0
LOOKBACK = 24 * 3600 * 1000 // BAR_MS
HORIZONS = [60, 120, 300]
TAKER_EXIT_FEE = 0.0004  # 4bp; diagnostic only


def _hl_grid(bars, start, end):
    close, opn, vq, buyvq, sellvq, has = _grid(bars, start, end)
    K = len(close)
    high = np.full(K, np.nan)
    low = np.full(K, np.nan)
    idx = {start + i * BAR_MS: i for i in range(K)}
    for j, t in enumerate(bars["t"]):
        i = idx.get(t)
        if i is None:
            continue
        high[i] = bars["high"][j]
        low[i] = bars["low"][j]
    return close, opn, high, low, vq, buyvq, sellvq, has


def collect_signals(u):
    start, end = u["start"], u["end"]
    rows = []
    per_sym = {}
    for s in LIQUID:
        bars = _load(s, start, end)
        if bars is None:
            per_sym[s] = {"bars": False}
            continue
        close, opn, high, low, vq, buyvq, sellvq, has = _hl_grid(
            bars, start, end)
        n_sig = n_veto = n_fill = 0
        for k in range(LOOKBACK, len(close) - 2):
            if not has[k] or vq[k] <= 0:
                continue
            ofi = (buyvq[k] - sellvq[k]) / vq[k]
            if abs(ofi) < TAU:
                continue
            p0, p1 = close[k - LOOKBACK], close[k]
            r24 = (p1 / p0 - 1.0) if (p0 > 0 and np.isfinite(p0)) else 0.0
            if r24 >= RUNNER_24H and ofi > 0:
                n_veto += 1
                continue
            n_sig += 1
            side = 1 if ofi > 0 else -1
            fill_k = k + 1
            opp = sellvq[fill_k] if side > 0 else buyvq[fill_k]
            filled = bool(has[fill_k] and opp >= MIN_FILL_USD)
            if filled:
                n_fill += 1
            entry = close[fill_k] if filled and close[fill_k] > 0 else np.nan
            # Adverse: long filled at the fill-bar HIGH, short at the LOW
            # (picked off). The opposite (low/high) would be a best-price
            # fantasy and is not used.
            adv = (high[fill_k] if side > 0 else low[fill_k]) if filled else np.nan
            rec = {"symbol": s, "k": k, "t": start + k * BAR_MS,
                   "ofi": ofi, "r24": r24, "side": side, "filled": filled,
                   "entry": entry, "adv_entry": adv, "fill_k": fill_k}
            for H in HORIZONS:
                xi = fill_k + H // 30
                if filled and xi < len(opn) and has[xi] and opn[xi] > 0 and entry > 0:
                    rec[f"raw_{H}"] = np.log(opn[xi] / entry)
                    rec[f"adv_{H}"] = (np.log(opn[xi] / adv)
                                       if adv > 0 else np.nan)
                else:
                    rec[f"raw_{H}"] = np.nan
                    rec[f"adv_{H}"] = np.nan
            rows.append(rec)
        per_sym[s] = {"bars": True, "signals": n_sig, "vetoes": n_veto,
                      "fills": n_fill}
        print(f"  {s:<14} signals={n_sig:<6} vetoes={n_veto:<5} "
              f"fills={n_fill}", flush=True)
    return rows, per_sym


def series(df, H, mode):
    """mode: unconditional (unfilled=0), filled_only, adv_filled, taker_exit."""
    if mode == "unconditional":
        net = np.zeros(len(df))
        raw = df[f"raw_{H}"].to_numpy()
        filled = df["filled"].to_numpy() & np.isfinite(raw)
        net[filled] = df["side"].to_numpy()[filled] * raw[filled]
        sub = df.copy()
        sub["_net"] = net
    elif mode == "filled_only":
        raw = df[f"raw_{H}"].to_numpy()
        m = df["filled"].to_numpy() & np.isfinite(raw)
        sub = df[m].copy()
        sub["_net"] = sub["side"].to_numpy() * raw[m]
    elif mode == "adv_filled":
        raw = df[f"adv_{H}"].to_numpy()
        m = df["filled"].to_numpy() & np.isfinite(raw)
        sub = df[m].copy()
        sub["_net"] = sub["side"].to_numpy() * raw[m]
    elif mode == "taker_exit":
        raw = df[f"raw_{H}"].to_numpy()
        net = np.zeros(len(df))
        filled = df["filled"].to_numpy() & np.isfinite(raw)
        net[filled] = df["side"].to_numpy()[filled] * raw[filled] - TAKER_EXIT_FEE
        sub = df.copy()
        sub["_net"] = net
    else:
        raise ValueError(mode)
    if len(sub) == 0:
        return np.array([]), np.array([]), sub
    hb = H // 30 + LAT_BARS + 1
    imask = independent_mask(sub, hb)
    days = (sub["t"].to_numpy() // (86400 * 1000))[imask]
    return sub["_net"].to_numpy()[imask], days, sub[imask]


def print_table(title, cells):
    print(f"\n===== {title} =====")
    print(f"{'cell':<16}{'n':>6}{'mean':>9}{'median':>9}{'hit%':>7}"
          f"{'CI_lo':>9}{'CI_hi':>9}   verdict")
    for name, v in cells.items():
        if v["mean_bps"] is None or v["n_indep"] < 20:
            print(f"{name:<16}{v['n_indep']:>6}   insufficient")
            continue
        lo = v["ci95_lo_bps"]
        verdict = "edge>0" if (lo is not None and lo > 0) else "no edge"
        print(f"{name:<16}{v['n_indep']:>6}{v['mean_bps']:>9.2f}"
              f"{v['median_bps']:>9.2f}{v['hit_rate'] * 100:>7.1f}"
              f"{(lo if lo is not None else float('nan')):>9.2f}"
              f"{(v['ci95_hi_bps'] if v['ci95_hi_bps'] is not None else float('nan')):>9.2f}"
              f"   {verdict}")


def main():
    u = load_universe()
    start, end = u["start"], u["end"]
    cut = ((start + (end - start) * 2 // 3) // 30_000) * 30_000
    print("collecting |OFI|>=0.6 signals on frozen liquid universe...")
    rows, per_sym = collect_signals(u)
    df = pd.DataFrame(rows)
    n = len(df)
    nf = int(df["filled"].sum()) if n else 0
    print(f"\nsignals={n}  fills={nf}  fill_rate={nf / n:.1%}" if n else "no signals")

    out = {"universe": LIQUID, "per_symbol": per_sym,
           "n_signals": n, "n_fills": nf,
           "fill_rate": (nf / n) if n else None}

    primary = {}
    for H in HORIZONS:
        net, days, _ = series(df, H, "unconditional")
        primary[f"maker_{H}s"] = cell_stats(net, days)
    out["primary_unfilled_as_zero"] = primary
    print_table("PRIMARY: maker 0-fee, unfilled=0 (selection)", primary)

    filled = {}
    for H in HORIZONS:
        net, days, _ = series(df, H, "filled_only")
        filled[f"filled_{H}s"] = cell_stats(net, days)
    out["filled_only_diagnostic"] = filled
    print_table("diagnostic: filled only (no missed-fill)", filled)

    taker = {}
    for H in HORIZONS:
        net, days, _ = series(df, H, "taker_exit")
        taker[f"taker_exit_{H}s"] = cell_stats(net, days)
    out["taker_exit_diagnostic"] = taker
    print_table("diagnostic: unfilled=0, exit as taker (4bp)", taker)

    adv = {}
    for H in HORIZONS:
        net, days, _ = series(df, H, "adv_filled")
        adv[f"adv_{H}s"] = cell_stats(net, days)
    out["adverse_selection_diagnostic"] = adv
    print_table("diagnostic: adverse-selection entry (low/high of fill bar)", adv)

    print("\n== Walk-forward PRIMARY (IS 14d / OOS 7d) ==")
    wf = {}
    for H in HORIZONS:
        net, days, sub = series(df, H, "unconditional")
        is_m = sub["t"].to_numpy() < cut
        s_is = cell_stats(net[is_m], days[is_m])
        s_oos = cell_stats(net[~is_m], days[~is_m])
        wf[f"maker_{H}s"] = {"IS": s_is, "OOS": s_oos}

        def fmt(s):
            if s["mean_bps"] is None:
                return "n/a"
            return (f"mean={s['mean_bps']:+.2f} med={s['median_bps']:+.2f} "
                    f"hit={s['hit_rate']*100:.0f}% "
                    f"CI=[{s['ci95_lo_bps']:+.2f},{s['ci95_hi_bps']:+.2f}] "
                    f"n={s['n_indep']}")
        print(f"  {H:>3}s  IS : {fmt(s_is)}")
        print(f"       OOS: {fmt(s_oos)}")
    out["walk_forward"] = wf
    out["meta"] = {"tau": TAU, "runner_veto": RUNNER_24H,
                   "min_fill_usd": MIN_FILL_USD, "selection_cells": 3,
                   "cumulative_N": 96}
    RESULTS.mkdir(exist_ok=True)
    (RESULTS / "phase1e_maker_ofi.json").write_text(json.dumps(out, indent=2, default=int))
    print(f"\nsaved -> {RESULTS / 'phase1e_maker_ofi.json'}")


if __name__ == "__main__":
    main()
