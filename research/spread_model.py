"""Turn the live recorder's !bookTicker samples into a per-symbol spread cost.

Historical aggTrades have no book, so the spread that a taker pays is modelled
from live measurement. We read the most recent recorder snapshot and expose the
per-symbol median (p50) and p90 spread in basis points, plus top-of-book depth.
"""
import json
from pathlib import Path

SAMPLES = Path(__file__).parent / "data" / "spread_samples.jsonl"


def load_spreads():
    """Return {symbol: {"p50": bps, "p90": bps, "top_p50": usd, "n": int}}
    from the last (most-accumulated) recorder snapshot."""
    if not SAMPLES.exists():
        return {}
    last = None
    with SAMPLES.open() as f:
        for line in f:
            line = line.strip()
            if line:
                last = line
    if not last:
        return {}
    snap = json.loads(last)
    out = {}
    for s, d in snap["symbols"].items():
        out[s] = {
            "p50": d["spread_bps"]["p50"],
            "p90": d["spread_bps"]["p90"],
            "p99": d["spread_bps"]["p99"],
            "top_p50": d["top_usd"]["p50"],
            "n": d["n"],
        }
    return out


def summary():
    sp = load_spreads()
    if not sp:
        print("no spread samples yet")
        return
    import statistics as st
    p50s = [v["p50"] for v in sp.values() if v["p50"] is not None]
    print(f"symbols with spread data: {len(sp)}")
    print(f"median across symbols of p50 spread: {st.median(p50s):.2f} bps")
    print(f"cross-symbol p50 spread range: {min(p50s):.2f} .. {max(p50s):.2f} bps")
    print("\nsample (liquid names):")
    for s in ["BTCUSDT", "ETHUSDT", "SOLUSDT", "XRPUSDT", "DOGEUSDT",
              "ASTERUSDT", "BNBUSDT"]:
        if s in sp:
            v = sp[s]
            print(f"  {s:<12} p50={v['p50']:.2f}bp p90={v['p90']:.2f}bp "
                  f"top_p50=${v['top_p50']:,.0f} n={v['n']}")


if __name__ == "__main__":
    summary()
