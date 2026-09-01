"""Live spread + signal recorder for Aster perps.

Historical aggTrades give trades but NOT the book, so there is no historical
bid/ask, spread, or midquote. This recorder connects to the live WebSocket and
persists, per symbol, the distribution of:

    spread_bps = (ask - bid) / mid * 1e4
    top_usd    = min(bid_size * bid, ask_size * ask)

sampled on the 1 Hz !markPrice clock (the same clock the detector samples on).
These live measurements are a Phase 1 cost input that cannot be recovered from
history. Output is append-only JSONL so a restart never loses prior data.

    python research/recorder.py                # run until Ctrl-C
    python research/recorder.py --seconds 3600 # run for one hour

This module does NOT import or modify aster_pulse.py; the detector's self-check
is unaffected.
"""
import argparse
import asyncio
import json
import time
from collections import defaultdict
from pathlib import Path

import websockets

WS = ("wss://fstream.asterdex.com/stream?streams="
      "!markPrice@arr@1s/!bookTicker")
OUT = Path(__file__).parent / "data" / "spread_samples.jsonl"
FLUSH_EVERY = 30  # seconds between snapshots written to disk


def summarize(samples):
    """Compact per-symbol summary: count, and spread/top percentiles."""
    n = len(samples)
    sp = sorted(s[0] for s in samples)
    tp = sorted(s[1] for s in samples)

    def pct(a, q):
        if not a:
            return None
        i = min(len(a) - 1, max(0, int(q * (len(a) - 1))))
        return a[i]

    return {
        "n": n,
        "spread_bps": {"p10": pct(sp, .10), "p50": pct(sp, .50),
                       "p90": pct(sp, .90), "p99": pct(sp, .99)},
        "top_usd": {"p10": pct(tp, .10), "p50": pct(tp, .50),
                    "p90": pct(tp, .90)},
    }


async def run(seconds):
    book = {}
    samples = defaultdict(list)  # symbol -> [(spread_bps, top_usd), ...]
    t0 = last_flush = time.time()
    OUT.parent.mkdir(parents=True, exist_ok=True)
    print(f"recording spreads -> {OUT}", flush=True)

    async for ws in websockets.connect(WS, ping_interval=20):
        try:
            async for msg in ws:
                m = json.loads(msg)
                data, stream, now = m["data"], m["stream"], time.time()

                if stream.startswith("!bookTicker"):
                    s = data["s"]
                    b, a = float(data["b"]), float(data["a"])
                    bsz, asz = float(data["B"]), float(data["A"])
                    if a > b > 0:
                        mid = (a + b) / 2
                        spread = (a - b) / mid * 1e4
                        top = min(bsz * b, asz * a)
                        book[s] = (spread, top)
                    continue

                # markPrice batch = 1 Hz sampling clock; snapshot current book
                for e in data:
                    bk = book.get(e["s"])
                    if bk:
                        samples[e["s"]].append(bk)

                if now - last_flush >= FLUSH_EVERY:
                    last_flush = now
                    snap = {"t": int(now), "elapsed": int(now - t0),
                            "symbols": {s: summarize(v)
                                        for s, v in samples.items() if v}}
                    with OUT.open("a") as f:
                        f.write(json.dumps(snap) + "\n")
                    print(f"[{int(now - t0)}s] flushed {len(snap['symbols'])} "
                          f"symbols", flush=True)

                if seconds and now - t0 >= seconds:
                    print("done", flush=True)
                    return
        except websockets.ConnectionClosed:
            print("reconnecting...", flush=True)


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--seconds", type=int, default=0)
    a = ap.parse_args()
    try:
        asyncio.run(run(a.seconds))
    except KeyboardInterrupt:
        pass
