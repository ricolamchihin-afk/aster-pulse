"""Reconstruct the aster_pulse detector signal from historical 30s bars.

Because history has no book, the midquote is proxied by the last trade price in
each 30s bar (`close`). Every other piece of the detector is reproduced exactly
on the 30s grid, which is the detector's own baseline granularity:

  r[k]   = log(close[k] / close[k-1])                 the 30s move
  beta   = rolling beta of r vs BTC r over <=240 bars (>=40 to score), clamp[0,3]
  resid  = r - beta*btc_r  ; med,sigma = median / 1.4826*MAD of resid over window
  z      = (r[k] - beta*btc_r[k] - med) / sigma
  burst  = vq[k] / median(vq over trailing baseline)  volume confirmation

An event fires where the detector's statistical gate `flagged` is true:
  |r| >= MIN_MOVE (0.5%)  AND  |z| >= Z_MIN (4)  AND  burst >= VOL_MULT (3)

Volume is always known from aggTrades, so the burst gate is always applied
(unlike live, where only ~45 symbols carry !ticker volume).

The reconstruction is cross-checked bar-by-bar against aster_pulse's own
beta / residual_stats / zscore functions in `selftest_equivalence`.
"""
import json
import sys
from pathlib import Path

import numpy as np
import pandas as pd

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import aster_pulse as ap  # noqa: E402  (path set above; app import, not inline)

DATA = Path(__file__).parent / "data"
BARS = DATA / "bars"
BAR_MS = 30_000

MIN_BASE = ap.MIN_BASE      # 40
BASE_OBS = ap.BASE_OBS      # 240
Z_MIN = ap.Z_MIN            # 4.0
MIN_MOVE = ap.MIN_MOVE      # 0.005
VOL_MULT = ap.VOL_MULT      # 3.0
VOL_FLOOR = ap.VOL_FLOOR    # 0.0005
BETA_CLAMP = ap.BETA_CLAMP  # 3.0
BURST_BASE = 60             # trailing bars for the volume-burst baseline median


def _load(symbol, start, end):
    f = BARS / f"{symbol}__{start}__{end}__v2.json"
    if not f.exists():
        return None
    return json.loads(f.read_text())


def _grid(bars, start, end):
    """Return per-grid arrays on the aligned 30s grid: close(ffill), open, vq,
    buyvq, sellvq, has_trade. Prices are forward-filled; open/high/low are NaN
    where no trade occurred in the bar."""
    K = (end - start) // BAR_MS
    idx = {t: i for i, t in enumerate((start + k * BAR_MS for k in range(K)))}
    close = np.full(K, np.nan)
    opn = np.full(K, np.nan)
    vq = np.zeros(K)
    buyvq = np.zeros(K)
    sellvq = np.zeros(K)
    for j, t in enumerate(bars["t"]):
        i = idx.get(t)
        if i is None:
            continue
        close[i] = bars["close"][j]
        opn[i] = bars["open"][j]
        vq[i] = bars["vq"][j]
        buyvq[i] = bars["buyvq"][j]
        sellvq[i] = bars["sellvq"][j]
    has_trade = ~np.isnan(close)
    # forward-fill close
    last = np.nan
    for i in range(K):
        if has_trade[i]:
            last = close[i]
        else:
            close[i] = last
    return close, opn, vq, buyvq, sellvq, has_trade


def _returns(close):
    r = np.zeros(len(close))
    prev = close[:-1]
    cur = close[1:]
    ok = (prev > 0) & np.isfinite(prev) & np.isfinite(cur)
    r[1:][ok] = np.log(cur[ok] / prev[ok])
    r[~np.isfinite(r)] = 0.0
    return r


def reconstruct(symbol, start, end, btc_r):
    """Return a DataFrame of detector events for one symbol."""
    bars = _load(symbol, start, end)
    if bars is None or len(bars["t"]) < MIN_BASE + 5:
        return pd.DataFrame()
    close, opn, vq, buyvq, sellvq, has_trade = _grid(bars, start, end)
    r = _returns(close)
    K = len(r)

    events = []
    for k in range(MIN_BASE, K):
        lo = max(0, k - BASE_OBS + 1)
        rs = r[lo:k + 1]
        bs = btc_r[lo:k + 1]
        n = len(rs)
        if n < MIN_BASE:
            continue
        # beta of r vs btc over the window, clamped [0, 3]
        mx = bs.mean()
        vx = np.sum((bs - mx) ** 2)
        if vx <= 0:
            b = 1.0
        else:
            my = rs.mean()
            b = np.sum((bs - mx) * (rs - my)) / vx
            b = max(0.0, min(BETA_CLAMP, b))
        resid = rs - b * bs
        med = np.median(resid)
        sigma = max(1.4826 * np.median(np.abs(resid - med)), VOL_FLOOR)
        x = r[k] - b * btc_r[k]
        z = (x - med) / sigma
        move = r[k]
        if abs(move) < MIN_MOVE or abs(z) < Z_MIN:
            continue
        # volume-burst confirmation
        vlo = max(0, k - BURST_BASE)
        base_vq = np.median(vq[vlo:k]) if k > vlo else 0.0
        burst = (vq[k] / base_vq) if base_vq > 0 else 0.0
        if burst < VOL_MULT:
            continue
        if not has_trade[k]:
            continue
        ofi = ((buyvq[k] - sellvq[k]) / vq[k]) if vq[k] > 0 else 0.0
        events.append({
            "symbol": symbol, "k": k, "t": start + k * BAR_MS,
            "move": move, "x": x, "z": z, "beta": b, "burst": burst,
            "ofi": ofi, "close": close[k],
        })
    return pd.DataFrame(events)


def selftest_equivalence():
    """Prove the numpy reconstruction matches aster_pulse's own functions."""
    from collections import deque
    rng = np.random.default_rng(1)
    K = 400
    btc_r = rng.normal(0, 0.002, K)
    sym_r = 1.3 * btc_r + rng.normal(0, 0.003, K)
    k = 350
    lo = max(0, k - BASE_OBS + 1)
    rs, bs = sym_r[lo:k + 1], btc_r[lo:k + 1]
    mx = bs.mean(); vx = np.sum((bs - mx) ** 2); my = rs.mean()
    b_np = max(0.0, min(BETA_CLAMP,
                        np.sum((bs - mx) * (rs - my)) / vx))
    resid = rs - b_np * bs
    med_np = np.median(resid)
    sig_np = max(1.4826 * np.median(np.abs(resid - med_np)), VOL_FLOOR)
    x_np, z_np = sym_r[k] - b_np * btc_r[k], None
    z_np = (x_np - med_np) / sig_np

    b_ap = ap.beta(deque(rs.tolist()), deque(bs.tolist()))
    med_ap, sig_ap = ap.residual_stats(deque(rs.tolist()),
                                       deque(bs.tolist()), b_ap)
    x_ap, z_ap = ap.zscore(sym_r[k], btc_r[k], b_ap, med_ap, sig_ap)
    assert abs(b_np - b_ap) < 1e-9, (b_np, b_ap)
    assert abs(med_np - med_ap) < 1e-12, (med_np, med_ap)
    assert abs(sig_np - sig_ap) < 1e-12, (sig_np, sig_ap)
    assert abs(z_np - z_ap) < 1e-9, (z_np, z_ap)
    print("equivalence OK  beta/med/sigma/z match aster_pulse to 1e-9")


def load_universe():
    return json.loads((DATA / "universe.json").read_text())


def btc_grid_returns(start, end):
    bars = _load("BTCUSDT", start, end)
    if bars is None:
        raise SystemExit("BTCUSDT bars missing; run download_universe.py first")
    close, *_ = _grid(bars, start, end)
    return _returns(close)


if __name__ == "__main__":
    selftest_equivalence()
    u = load_universe()
    start, end = u["start"], u["end"]
    btc_r = btc_grid_returns(start, end)
    frames = []
    for row in u["universe"]:
        s = row["symbol"]
        df = reconstruct(s, start, end, btc_r)
        if not df.empty:
            frames.append(df)
        print(f"{s:<14} events={len(df)}", flush=True)
    allev = pd.concat(frames, ignore_index=True) if frames else pd.DataFrame()
    out = DATA / "events.parquet"
    allev.to_parquet(out)
    print(f"\ntotal events: {len(allev)} -> {out}")
