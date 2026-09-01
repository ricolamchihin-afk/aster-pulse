"""Aster DEX perps - live abnormal-move scanner + dashboard.

    python aster_pulse.py              live + dashboard at http://127.0.0.1:8787
    python aster_pulse.py --no-serve   terminal feed only
    python aster_pulse.py --test       self-check, no network

Detection runs on the MIDQUOTE (from !bookTicker), sampled on the 1 Hz !markPrice@arr@1s
clock so every symbol sits on the same uniform time grid. The baseline is a rolling set
of NON-OVERLAPPING 30s returns scored with median/MAD, and the market move is removed
with a rolling beta against BTC before scoring.

It is an attention flag, not a trade signal. Direction is deliberately never asserted -
see direction() for why.
"""
import asyncio
import http.server
import json
import math
import sys
import threading
import time
import webbrowser
from collections import defaultdict, deque
from pathlib import Path
from statistics import fmean, median
from urllib.parse import urlparse

import httpx
import websockets

REST = "https://fapi.asterdex.com/fapi/v1"
WS = ("wss://fstream.asterdex.com/stream?streams="
      "!markPrice@arr@1s/!bookTicker/!ticker@arr")

HORIZON = 30            # seconds the move is measured over
BASE_OBS = 240          # non-overlapping 30s returns kept as baseline (2 hours)
MIN_BASE = 40           # ~20 min of baseline before a symbol may score at all
VOL_FLOOR = 0.0005      # floor on 30s sigma; stops stale marks producing absurd z
Z_MIN = 4.0             # robust score threshold
MIN_MOVE = 0.005        # absolute move floor
VOL_MULT = 3.0          # quote-volume burst vs baseline, when volume is knowable
MAX_SPREAD_BPS = 20.0   # wider than this is not executable at these horizons
MIN_BOOK_USD = 500.0    # top-of-book notional on the thinner side
THIN_QUOTE = 1_000_000  # 24h quote volume below this is tagged THIN
BETA_CLAMP = 3.0        # rolling beta vs BTC, clamped
COOLDOWN = 60           # seconds between alerts per symbol
OI_REFRESH = 60         # seconds between open-interest polls for board symbols
PORT = 8787
BOARD_MOVE = 0.001
BOARD_ROWS = 25


# ---------------------------------------------------------------- statistics


def mad_sigma(base):
    """(median, robust sigma) of a baseline series. 1.4826*MAD matches sigma for a
    normal, and unlike stdev it is not dragged around by the fat tails crypto has."""
    med = median(base)
    return med, max(1.4826 * median([abs(x - med) for x in base]), VOL_FLOOR)


def beta(sym_base, btc_base):
    """Rolling beta of a symbol's 30s returns against BTC's, over the aligned baseline.

    Baselines are appended on one global 30s boundary, so index i is the same wall-clock
    window for every symbol - that alignment is what makes this a valid regression.
    """
    n = min(len(sym_base), len(btc_base))
    if n < MIN_BASE:
        return 1.0
    x, y = list(btc_base)[-n:], list(sym_base)[-n:]
    mx, my = fmean(x), fmean(y)
    vx = sum((a - mx) ** 2 for a in x)
    if vx <= 0:
        return 1.0
    return max(0.0, min(BETA_CLAMP, sum((a - mx) * (b - my) for a, b in zip(x, y)) / vx))


def residual_stats(sym_base, btc_base, b):
    """median/MAD of the BETA-RESIDUAL baseline - the same quantity the live score is
    measured in, so the score is dimensionally honest."""
    n = min(len(sym_base), len(btc_base))
    x, y = list(btc_base)[-n:], list(sym_base)[-n:]
    return mad_sigma([yi - b * xi for xi, yi in zip(x, y)])


def zscore(r30, btc_r30, b, med, sigma):
    """Idiosyncratic move, and that move in robust sigmas. Treat it as a standardised
    score, never as a Gaussian probability - these returns are fat-tailed."""
    x = r30 - b * btc_r30
    return x, (x - med) / sigma


def burst(dv):
    """Quote-volume rate over the last window vs its baseline rate.

    None when there is no baseline - unknown, not zero. Only ~45 symbols are covered by
    !ticker@arr, so most rows legitimately have no volume confirmation available.
    """
    if len(dv) <= HORIZON:
        return None
    d = [max(0.0, dv[i] - dv[i - 1]) for i in range(1, len(dv))]
    base = median(d[:-HORIZON]) if len(d) > HORIZON else 0.0
    return (sum(d[-HORIZON:]) / HORIZON) / base if base > 0 else None


def tradeable(book, q24, mid):
    """(ok, reason). 'Three times almost-zero volume is still untradeable' - so an
    anomaly only counts as actionable if the book can actually absorb an order."""
    if not book or not mid:
        return False, "no book"
    bid, ask, bsz, asz = book
    if ask <= bid or bid <= 0:
        return False, "crossed"
    spread = (ask - bid) / mid * 1e4
    top = min(bsz * bid, asz * ask)
    if spread > MAX_SPREAD_BPS:
        return False, f"spread {spread:.0f}bp"
    if top < MIN_BOOK_USD:
        return False, f"book ${top:,.0f}"
    if q24 < THIN_QUOTE:
        return False, "thin 24h"
    return True, f"{spread:.1f}bp"


def flagged(move, z, b):
    """Statistical gate only: big in absolute terms, unusual for this symbol, and
    volume-confirmed where volume is knowable."""
    return abs(move) >= MIN_MOVE and abs(z) >= Z_MIN and (b is None or b >= VOL_MULT)


def fires(move, z, b, ok):
    """Radar gate: statistically flagged AND actually executable.

    Flagged-but-untradeable symbols are not dropped - they pin to the watchlist tagged
    'untradeable', because "this moved abnormally and you cannot trade it" is useful to
    see, just not actionable.
    """
    return flagged(move, z, b) and ok


def direction(fired, ok):
    """Always WAIT, with the reason why.

    Continuation-vs-reversal after an anomaly has NOT been tested on Aster data. Emitting
    LONG or SHORT from an untested rule would be inventing a signal, so this returns WAIT
    for everything. It is the single place a validated rule plugs in: run the two
    predeclared hypotheses (momentum / reversal) over 30/60/120/300s horizons on
    executable prices net of costs, walk-forward, and only then return a side here.
    """
    if not fired:
        return "WAIT", "no signal"
    return ("WAIT", "untested") if ok else ("WAIT", "untradeable")


def spark(prices, n=30):
    """Last n prices normalised to ints 0-100, so the wire stays small."""
    p = prices[-n:]
    lo, hi = min(p), max(p)
    return [50] * len(p) if hi <= lo else [round((v - lo) / (hi - lo) * 100) for v in p]


# ---------------------------------------------------------------- dashboard

# ponytail: STATE is rebound wholesale, which is atomic under the GIL, so no lock.
STATE = {"t": 0, "up": 0, "tracked": 0, "ready": 0, "seeded": 0, "based": 0,
         "movers": [], "alerts": []}


class Handler(http.server.BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def log_message(self, *a):
        pass  # the alert feed is the output; access logs would bury it

    def do_GET(self):
        path = urlparse(self.path).path
        if path == "/":
            body = (Path(__file__).parent / "dashboard.html").read_bytes()
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
        elif path == "/events":
            self.send_response(200)
            self.send_header("Content-Type", "text/event-stream")
            self.send_header("Cache-Control", "no-cache")
            self.end_headers()
            sent = None
            try:
                while True:
                    snap = STATE
                    if snap["t"] != sent:
                        sent = snap["t"]
                        self.wfile.write(f"data: {json.dumps(snap)}\n\n".encode())
                        self.wfile.flush()
                    time.sleep(0.4)
            except (BrokenPipeError, ConnectionResetError, OSError):
                pass  # browser tab closed
        else:
            # Without this the browser's /favicon.ico request gets no response at all
            # and sits open on a keep-alive socket.
            self.send_error(404)


class Server(http.server.ThreadingHTTPServer):
    daemon_threads = True

    def handle_error(self, request, addr):
        # A closing browser tab aborts its SSE socket. Only report non-socket faults,
        # otherwise every tab close buries the alert feed in a traceback.
        if not isinstance(sys.exc_info()[1], OSError):
            super().handle_error(request, addr)


def serve(open_browser=True):
    threading.Thread(target=Server(("127.0.0.1", PORT), Handler).serve_forever,
                     daemon=True).start()
    print(f"dashboard  http://127.0.0.1:{PORT}", flush=True)
    if open_browser:
        webbrowser.open(f"http://127.0.0.1:{PORT}")


# ---------------------------------------------------------------- live loop


def oi_poller(want, oi):
    """Open interest has no bulk endpoint on Aster, so poll only what is on the board.

    ponytail: sequential and slow on purpose - a few dozen symbols a minute stays far
    under any rate limit. Parallelise only if the board grows a lot.
    """
    with httpx.Client(timeout=10) as c:
        while True:
            for s in list(want)[:BOARD_ROWS * 2]:
                try:
                    r = c.get(f"{REST}/openInterest", params={"symbol": s}).json()
                    oi[s] = float(r["openInterest"])
                except Exception:
                    oi.pop(s, None)  # delisted, or a symbol the endpoint does not know
            time.sleep(OI_REFRESH)


def make_row(s, mids, r30, x, z, b, bu, q, fr, ft, oi_v, ok, why, fired):
    side, reason = direction(fired, ok)
    return {"s": s, "move": r30, "resid": x, "z": z, "beta": b, "burst": bu, "q": q,
            "fr": fr, "ft": ft, "oi": oi_v, "ok": ok, "why": why,
            "side": side, "reason": reason, "fired": fired,
            "thin": q < THIN_QUOTE, "spark": spark(mids)}


async def main(dashboard=True, open_browser=True):
    px = defaultdict(lambda: deque(maxlen=HORIZON + 1))        # midquote, 1 Hz
    base = defaultdict(lambda: deque(maxlen=BASE_OBS))         # non-overlapping 30s rets
    dvol = defaultdict(lambda: deque(maxlen=HORIZON * 2 + 1))  # 24h qvol samples
    stats = {}                                                 # symbol -> (beta, med, sig)
    book, vol, fund, oi = {}, {}, {}, {}
    last, cur = {}, {}
    alerts = deque(maxlen=60)
    board = set()

    with httpx.Client(timeout=20) as c:
        vol.update({t["symbol"]: float(t["quoteVolume"])
                    for t in c.get(f"{REST}/ticker/24hr").json()})
    if dashboard:
        serve(open_browser)
    threading.Thread(target=oi_poller, args=(board, oi), daemon=True).start()

    t0 = pushed = time.time()
    tick = 0
    print(f"connecting  ({len(vol)} symbols seeded; baseline needs "
          f"{MIN_BASE * HORIZON // 60} min before any symbol can score)", flush=True)

    async for ws in websockets.connect(WS, ping_interval=20):
        try:
            async for msg in ws:
                m = json.loads(msg)
                data, stream, now = m["data"], m["stream"], time.time()

                if stream.startswith("!bookTicker"):
                    book[data["s"]] = (float(data["b"]), float(data["a"]),
                                       float(data["B"]), float(data["A"]))
                    continue
                if stream.startswith("!ticker"):
                    vol.update({t["s"]: float(t["q"]) for t in data})
                    continue

                # ---- markPrice batch: one per listed perp, every second. Our clock.
                tick += 1
                for e in data:
                    s = e["s"]
                    bk = book.get(s)
                    mid = (bk[0] + bk[1]) / 2 if bk and bk[1] > bk[0] > 0 else float(e["p"])
                    px[s].append(mid)
                    dvol[s].append(vol.get(s, 0.0))
                    fund[s] = (float(e["r"]), int(e["T"]))

                if tick % HORIZON == 0:  # global 30s boundary: extend every baseline
                    for s, q in px.items():
                        if len(q) > HORIZON and q[0] > 0:
                            base[s].append(math.log(q[-1] / q[0]))
                    btc = base.get("BTCUSDT", deque())
                    for s, bs in base.items():
                        if len(bs) >= MIN_BASE:
                            b = beta(bs, btc)
                            stats[s] = (b, *residual_stats(bs, btc, b))

                btc_px = px.get("BTCUSDT")
                btc_r30 = (math.log(btc_px[-1] / btc_px[0])
                           if btc_px and len(btc_px) > HORIZON and btc_px[0] > 0 else 0.0)

                for s, q in px.items():
                    st = stats.get(s)
                    if st is None or len(q) <= HORIZON or q[0] <= 0:
                        continue
                    r30 = math.log(q[-1] / q[0])
                    b, med, sigma = st
                    x, z = zscore(r30, btc_r30, b, med, sigma)
                    bu = burst(list(dvol[s]))
                    q24 = vol.get(s, 0.0)
                    ok, why = tradeable(book.get(s), q24, q[-1])
                    flag = flagged(r30, z, bu)
                    hit = flag and ok
                    fr, ft = fund.get(s, (0.0, 0))
                    cur[s] = make_row(s, list(q), r30, x, z, b, bu, q24, fr, ft,
                                      oi.get(s), ok, why, flag)
                    if not hit or now - last.get(s, 0) < COOLDOWN:
                        continue
                    last[s] = now
                    a = dict(cur[s], ts=time.strftime("%H:%M:%S"))
                    alerts.append(a)
                    print(
                        f"{a['ts']}  {s:<14}{r30 * 100:+6.2f}% /{HORIZON}s"
                        f"  idio={x * 100:+6.2f}%  z={z:+6.1f}  b={b:.2f}"
                        f"  {why:<12} fund={fr * 100:+.4f}%  {a['side']}:{a['reason']}",
                        flush=True,
                    )

                if dashboard and now - pushed >= 1:
                    pushed = now
                    waits = [r for r in cur.values() if r["fired"]]
                    rest = sorted((r for r in cur.values()
                                   if not r["fired"] and abs(r["move"]) >= BOARD_MOVE),
                                  key=lambda r: -abs(r["move"]))
                    # Flagged-but-undecided rows pin to the top of the right panel.
                    movers = (sorted(waits, key=lambda r: -abs(r["z"])) + rest)[:BOARD_ROWS]
                    board.clear()
                    board.update(r["s"] for r in movers)
                    globals()["STATE"] = {
                        "t": int(now), "up": int(now - t0), "tracked": len(px),
                        "ready": len(cur), "seeded": len(vol), "based": len(stats),
                        "movers": movers, "alerts": list(alerts)[::-1],
                    }
        except websockets.ConnectionClosed:
            print("reconnecting...", flush=True)


# ---------------------------------------------------------------- self-check


def demo():
    import random
    random.seed(7)

    # --- robust sigma ignores the fat tail that would wreck stdev
    calm = [0.001 * random.gauss(0, 1) for _ in range(200)]
    med, sig = mad_sigma(calm)
    assert 0.0005 <= sig < 0.002, sig
    _, sig_spiked = mad_sigma(calm + [0.5, -0.4, 0.6])
    assert sig_spiked < sig * 1.5, (sig, sig_spiked)   # three outliers barely move it

    # --- the volatility floor stops a stale mark producing an infinite score
    _, floored = mad_sigma([0.0] * 100)
    assert floored == VOL_FLOOR
    _, z = zscore(0.002, 0.0, 1.0, 0.0, floored)
    assert z == 4.0, z

    # --- beta recovers a known market sensitivity, and is clamped
    btc = deque(0.001 * random.gauss(0, 1) for _ in range(MIN_BASE + 20))
    assert abs(beta(deque(2.0 * v for v in btc), btc) - 2.0) < 1e-6
    assert beta(deque(9.0 * v for v in btc), btc) == BETA_CLAMP
    assert beta(deque([0.01] * 5), btc) == 1.0        # too little baseline -> neutral

    # --- a pure market-wide move is NOT idiosyncratic: beta removes it
    x, z = zscore(0.02, 0.02, 1.0, 0.0, 0.001)
    assert abs(x) < 1e-12 and abs(z) < 1e-9, (x, z)

    # --- executability gates
    assert tradeable((100.0, 100.05, 50, 50), 5e6, 100.0)[0]
    assert not tradeable((100.0, 101.0, 50, 50), 5e6, 100.0)[0]   # 100bp spread
    assert not tradeable((100.0, 100.05, 1, 1), 5e6, 100.0)[0]    # $100 book
    assert not tradeable((100.0, 100.05, 50, 50), 5e3, 100.0)[0]  # thin 24h
    assert not tradeable(None, 5e6, 100.0)[0]

    # --- the alert gate needs every condition, executability included
    assert fires(0.02, 9.0, 5.0, True)
    assert not fires(0.001, 9.0, 5.0, True)     # move too small
    assert not fires(0.02, 1.0, 5.0, True)      # not unusual for this symbol
    assert not fires(0.02, 9.0, 1.0, True)      # volume did not confirm
    assert not fires(0.02, 9.0, 5.0, False)     # cannot be executed
    assert fires(0.02, 9.0, None, True)         # unknown volume still allowed through

    # --- untradeable anomalies stay visible on the watchlist instead of vanishing
    assert flagged(0.02, 9.0, 5.0) and not fires(0.02, 9.0, 5.0, False)
    assert direction(flagged(0.02, 9.0, 5.0), False) == ("WAIT", "untradeable")

    # --- volume burst: None means unknown, never zero
    assert burst([0.0] * 61) is None
    assert burst([float(i) for i in range(62)]) == 1.0
    assert burst(list(range(5))) is None         # not enough samples

    # --- direction is never asserted
    assert direction(True, True) == ("WAIT", "untested")
    assert direction(True, False) == ("WAIT", "untradeable")
    assert direction(False, True) == ("WAIT", "no signal")

    # --- sparkline
    assert spark([1, 2, 3, 4]) == [0, 33, 67, 100]
    assert spark([7.0] * 5) == [50] * 5
    assert len(spark(list(range(200)))) == 30

    print("ok  22/22")


if __name__ == "__main__":
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")  # Aster has CJK symbols
    if "--test" in sys.argv:
        demo()
    else:
        asyncio.run(main(dashboard="--no-serve" not in sys.argv,
                         open_browser="--no-open" not in sys.argv))
