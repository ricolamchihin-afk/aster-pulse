"""Select the liquid universe and download 30s bars for the study window.

Universe = perps with 24h quote volume >= $2M (the detector's tradeable tier;
the illiquid tail is untradeable for a real $10k book). BTCUSDT is always
included because it is the beta reference. Bars are cached by fetch_bars, so
this is safe to re-run.
"""
import json
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path

import httpx

from fetch_bars import fetch_bars

REST = "https://fapi.asterdex.com/fapi/v1"
DATA = Path(__file__).parent / "data"
MIN_QVOL = 2_000_000
DAYS = 21
WORKERS = 6


def main():
    c = httpx.Client(timeout=30)
    tk = [t for t in c.get(f"{REST}/ticker/24hr").json()
          if float(t["quoteVolume"]) > 0]
    tk.sort(key=lambda t: -float(t["quoteVolume"]))
    universe = [(t["symbol"], float(t["quoteVolume"]))
                for t in tk if float(t["quoteVolume"]) >= MIN_QVOL]
    syms = [s for s, _ in universe]
    if "BTCUSDT" not in syms:
        syms.insert(0, "BTCUSDT")

    end = (int(time.time() * 1000) // 30_000) * 30_000
    start = end - DAYS * 86400 * 1000
    meta = {"generated": end, "start": start, "end": end, "days": DAYS,
            "min_qvol": MIN_QVOL,
            "universe": [{"symbol": s, "qvol24h": q} for s, q in universe]}
    (DATA / "universe.json").write_text(json.dumps(meta, indent=2))
    print(f"universe: {len(syms)} symbols, window {DAYS}d "
          f"[{start}..{end}]", flush=True)

    def job(sym):
        cl = httpx.Client(timeout=30)
        try:
            b = fetch_bars(sym, start, end, client=cl, verbose=False)
            return sym, b["trades"], b["calls"], len(b["t"])
        finally:
            cl.close()

    done = 0
    with ThreadPoolExecutor(max_workers=WORKERS) as ex:
        futs = {ex.submit(job, s): s for s in syms}
        for f in as_completed(futs):
            sym = futs[f]
            try:
                s, tr, ca, nb = f.result()
                done += 1
                print(f"[{done}/{len(syms)}] {s:<14} {tr:>9,} trades  "
                      f"{ca:>5} calls  {nb:>6} bars", flush=True)
            except Exception as e:
                print(f"FAILED {sym}: {e}", flush=True)
    print("download complete", flush=True)


if __name__ == "__main__":
    main()
