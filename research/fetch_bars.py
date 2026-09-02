"""Download Aster aggTrades and fold them into compact 30s bars.

aggTrades are millisecond prints (price, qty, m=buyer-is-maker). We stream them
by `fromId` pagination and aggregate into 30s buckets, keeping only what the
detector and the direction test need:

  t       bucket start (ms, aligned to 30s grid)
  open    first trade price in the bucket  (realistic entry ~signal time)
  high    high trade price in the bucket
  low     low trade price in the bucket
  close   last trade price in the bucket   (midquote proxy; no book history)
  vq      quote volume in the bucket        (sum p*q)
  buyvq   quote volume from buy aggressors  (m == False)
  sellvq  quote volume from sell aggressors (m == True)
  n       trade count

Bars are cached to research/data/bars/<symbol>__<start>__<end>.json so reruns
are free. Only non-empty buckets are stored; the loader rebuilds the full 30s
grid with forward-filled prices.
"""
import argparse
import json
import time
from pathlib import Path

import httpx

REST = "https://fapi.asterdex.com/fapi/v1"
BAR_MS = 30_000
BARS_DIR = Path(__file__).parent / "data" / "bars"


def _get(client, params, tries=8):
    """Return the JSON list on success. Raise on persistent failure so that
    pagination is NEVER silently truncated by a transient error / rate limit
    (a 200 with an empty list is a legitimate 'no more data' and is returned)."""
    last = None
    for i in range(tries):
        try:
            r = client.get(f"{REST}/aggTrades", params=params)
            if r.status_code == 200:
                return r.json()
            last = f"HTTP {r.status_code}: {r.text[:120]}"
            if r.status_code in (429, 418, 500, 502, 503, 504):
                time.sleep(min(30, 1.5 * 2 ** i))
                continue
            r.raise_for_status()
        except httpx.HTTPError as e:
            last = str(e)
            time.sleep(min(30, 1.5 * 2 ** i))
    raise RuntimeError(f"aggTrades failed after {tries} tries: {last}")


def fetch_bars(symbol, start_ms, end_ms, client=None, verbose=True):
    """Return dict of parallel lists for non-empty 30s bars in [start, end)."""
    BARS_DIR.mkdir(parents=True, exist_ok=True)
    cache = BARS_DIR / f"{symbol}__{start_ms}__{end_ms}__v2.json"
    if cache.exists():
        return json.loads(cache.read_text())

    own = client is None
    if own:
        client = httpx.Client(timeout=30)

    bars = {}   # bucket_ms -> [open, high, low, close, vq, buyvq, sellvq, n]
    from_id = None
    calls = 0
    got = 0
    try:
        # seed: first page by startTime, then walk forward by id
        page = _get(client, {"symbol": symbol, "startTime": start_ms,
                             "endTime": min(start_ms + 3600_000, end_ms),
                             "limit": 1000})
        calls += 1
        if not page:
            # try a wider seed window in case the symbol is sparse near start
            page = _get(client, {"symbol": symbol, "startTime": start_ms,
                                 "endTime": end_ms, "limit": 1000})
            calls += 1
        while page:
            for t in page:
                T = t["T"]
                if T >= end_ms:
                    page = None
                    break
                if T < start_ms:
                    continue
                p = float(t["p"]); q = float(t["q"]); qv = p * q
                b = (T // BAR_MS) * BAR_MS
                rec = bars.get(b)
                if rec is None:
                    bars[b] = [p, p, p, p, qv, 0.0 if t["m"] else qv,
                               qv if t["m"] else 0.0, 1]
                else:
                    rec[3] = p
                    if p > rec[1]:
                        rec[1] = p
                    if p < rec[2]:
                        rec[2] = p
                    rec[4] += qv
                    if t["m"]:
                        rec[6] += qv
                    else:
                        rec[5] += qv
                    rec[7] += 1
                got += 1
            if page is None:
                break
            last_a = page[-1]["a"]
            from_id = last_a + 1
            page = _get(client, {"symbol": symbol, "fromId": from_id,
                                 "limit": 1000})
            calls += 1
            if calls % 50 == 0 and verbose:
                last_t = time.strftime('%m-%d %H:%M',
                                       time.gmtime(bars and max(bars) / 1000))
                print(f"    {symbol}: {calls} calls, {got} trades, "
                      f"through {last_t}", flush=True)
    finally:
        if own:
            client.close()

    ks = sorted(bars)
    out = {
        "symbol": symbol, "start": start_ms, "end": end_ms,
        "calls": calls, "trades": got,
        "t": ks,
        "open": [bars[k][0] for k in ks],
        "high": [bars[k][1] for k in ks],
        "low": [bars[k][2] for k in ks],
        "close": [bars[k][3] for k in ks],
        "vq": [bars[k][4] for k in ks],
        "buyvq": [bars[k][5] for k in ks],
        "sellvq": [bars[k][6] for k in ks],
        "n": [bars[k][7] for k in ks],
    }
    cache.write_text(json.dumps(out))
    if verbose:
        print(f"  {symbol}: {got} trades in {calls} calls -> "
              f"{len(ks)} non-empty 30s bars", flush=True)
    return out


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("symbol")
    ap.add_argument("--days", type=float, default=7)
    ap.add_argument("--end", type=int, default=0, help="end ms (0=now)")
    a = ap.parse_args()
    end = a.end or int(time.time() * 1000)
    start = end - int(a.days * 86400 * 1000)
    fetch_bars(a.symbol, start, end)
