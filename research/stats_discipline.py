"""Search-discipline statistics: correct for the number of trials.

- deflated_sharpe: Bailey & Lopez de Prado's Deflated Sharpe Ratio. Given the
  observed per-trade Sharpe, the number of independent trials N, the variance of
  Sharpe across those trials, and the return skew/kurtosis, it returns the
  probability that the true Sharpe exceeds the deflation benchmark expected from
  N trials of a zero-edge strategy. Pass = DSR > 0.95.

- whites_reality_check: bootstrap p-value for "the best of N strategies beats
  zero", resampling by day/cluster. A high p-value means the best cell is
  consistent with luck across the search.
"""
import numpy as np
from scipy.stats import norm

EULER = 0.5772156649015329


def sharpe(returns):
    r = np.asarray(returns, float)
    if len(r) < 2 or r.std(ddof=1) == 0:
        return 0.0
    return r.mean() / r.std(ddof=1)


def expected_max_sharpe(var_sr_across_trials, n_trials):
    """E[max SR] across n_trials zero-edge strategies (Bailey/LdP)."""
    if n_trials < 2 or var_sr_across_trials <= 0:
        return 0.0
    s = np.sqrt(var_sr_across_trials)
    a = norm.ppf(1 - 1.0 / n_trials)
    b = norm.ppf(1 - 1.0 / (n_trials * np.e))
    return s * ((1 - EULER) * a + EULER * b)


def deflated_sharpe(returns, n_trials, var_sr_across_trials):
    """Return (DSR probability, observed SR, benchmark SR0)."""
    r = np.asarray(returns, float)
    T = len(r)
    if T < 3:
        return (np.nan, np.nan, np.nan)
    sr = sharpe(r)
    sr0 = expected_max_sharpe(var_sr_across_trials, n_trials)
    # standardized moments
    sd = r.std(ddof=1)
    skew = ((r - r.mean()) ** 3).mean() / sd ** 3 if sd > 0 else 0.0
    kurt = ((r - r.mean()) ** 4).mean() / sd ** 4 if sd > 0 else 3.0
    denom = np.sqrt(max(1e-12, 1 - skew * sr + (kurt - 1) / 4 * sr ** 2))
    dsr = norm.cdf((sr - sr0) * np.sqrt(T - 1) / denom)
    return (float(dsr), float(sr), float(sr0))


def whites_reality_check(cells_returns, cells_days, n_boot=5000, seed=0):
    """cells_* are dicts {cell_name: array}. Bootstrap by day the max mean
    across cells under the null (recentre each cell to zero mean). Return the
    p-value that the observed best mean is beaten by chance."""
    rng = np.random.default_rng(seed)
    names = list(cells_returns)
    if not names:
        return np.nan
    obs_best = max(np.mean(cells_returns[c]) for c in names
                   if len(cells_returns[c]) > 0)
    # union of days across cells
    all_days = np.unique(np.concatenate([cells_days[c] for c in names
                                         if len(cells_days[c]) > 0]))
    count = 0
    for _ in range(n_boot):
        pick = rng.choice(all_days, size=len(all_days), replace=True)
        best = -np.inf
        for c in names:
            r = np.asarray(cells_returns[c], float)
            d = np.asarray(cells_days[c])
            if len(r) == 0:
                continue
            mu = r.mean()
            vals = np.concatenate([r[d == day] for day in pick if np.any(d == day)])
            if len(vals) == 0:
                continue
            stat = vals.mean() - mu  # recentred to null (zero edge)
            if stat > best:
                best = stat
        if best >= obs_best:
            count += 1
    return (count + 1) / (n_boot + 1)


def walk_forward_split(start_ms, end_ms, is_frac=2 / 3):
    """Chronological split: in-sample [start, cut), out-of-sample [cut, end)."""
    cut = start_ms + int((end_ms - start_ms) * is_frac)
    cut = (cut // 30_000) * 30_000
    return start_ms, cut, end_ms


if __name__ == "__main__":
    rng = np.random.default_rng(0)
    # sanity: a true-zero-edge return stream should NOT pass DSR after N trials
    r = rng.normal(0, 0.01, 500)
    dsr, sr, sr0 = deflated_sharpe(r, n_trials=50, var_sr_across_trials=0.02)
    print(f"zero-edge sanity: SR={sr:.3f} SR0={sr0:.3f} DSR={dsr:.3f} (expect low)")
