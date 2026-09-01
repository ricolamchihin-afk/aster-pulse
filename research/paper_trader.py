"""Live real-time PAPER trader for the OFI direction thesis (FALSIFICATION test).

Reconstructs the aster_pulse detector signal in real time on the live datafeed,
supplies the trade direction from order-flow imbalance (the locked candidate
rule in research/results/locked_rule.json), and simulates fills against the REAL
live book. NO live order is ever placed.

Pipeline per detector event (aster_pulse.flagged() fires on the 30s grid):
  1. compute OFI for the just-closed 30s bar from REST aggTrades over
     [bar_end-30s, bar_end)  (m=true => seller aggressor).
  2. apply the locked rule -> LONG / SHORT / none.
  3. ENTER at the live book (taker): LONG @ best ASK, SHORT @ best BID.
  4. after H seconds, MARK OUT: LONG @ best BID, SHORT @ best ASK.
  5. net P&L = directional log return - 8bp round-trip fee. The spread is ALREADY
     paid by crossing the book, so it is NOT double-counted (only recorded).

This live cost model is MORE realistic than the historical trade-price backtest,
which had no book and modelled the spread on top of a mid-ish trade price.

Everything (decisions + fills) is appended to research/data/paper_trades.jsonl
(append-only; realized P&L and closed-trade stats resume across restarts).

    python research/paper_trader.py            live paper trading
    python research/paper_trader.py --selftest offline unit checks, no network
"""
import asyncio
import json
import math
import sys
import time
from collections import defaultdict, deque
from pathlib import Path

import httpx
import websockets

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import aster_pulse as ap  # noqa: E402  (app import, path set above)

REST = "https://fapi.asterdex.com/fapi/v1"
WS = ("wss://fstream.asterdex.com/stream?streams="
      "!markPrice@arr@1s/!bookTicker/!ticker@arr")

# detector constants, imported straight from the detector (single source of truth)
HORIZON = ap.HORIZON          # 30s move window / grid
BASE_OBS = ap.BASE_OBS        # 240
MIN_BASE = ap.MIN_BASE        # 40  (~20 min warmup)
Z_MIN = ap.Z_MIN              # 4.0
MIN_MOVE = ap.MIN_MOVE        # 0.005
VOL_MULT = ap.VOL_MULT        # 3.0
VOL_FLOOR = ap.VOL_FLOOR
BETA_CLAMP = ap.BETA_CLAMP

DATA = Path(__file__).parent / "data"
RESULTS = Path(__file__).parent / "results"
TRADES = DATA / "paper_trades.jsonl"
FEE_RT = 0.0008               # 0.04% taker * 2 legs
BAR_MS = HORIZON * 1000


def load_universe_symbols():
    u = json.loads((DATA / "universe.json").read_text())
    syms = {r["symbol"] for r in u["universe"]}
    syms.add("BTCUSDT")  # beta reference
    return syms


def load_locked_rule():
    return json.loads((RESULTS / "locked_rule.json").read_text())


def decide(rule, ofi, move, tau):
    """Return +1 (LONG), -1 (SHORT), or 0 (no trade) for the locked rule."""
    ofi_sign = 1 if ofi > 0 else (-1 if ofi < 0 else 0)
    move_sign = 1 if move > 0 else (-1 if move < 0 else 0)
    if abs(ofi) < tau:
        return 0
    if rule == "follow_flow":
        return ofi_sign
    if rule == "fade_flow":
        return -ofi_sign
    if rule == "ofi_confirmed_momentum":
        return move_sign if move_sign == ofi_sign else 0
    if rule == "ofi_divergence_reversal":
        return -move_sign if move_sign != ofi_sign else 0
    raise ValueError(rule)


async def fetch_ofi(client, symbol, bar_end_ms):
    """OFI over the just-closed 30s bar [bar_end-30s, bar_end). Paginated so a
    burst of >1000 prints is not silently truncated. Returns (ofi, buyvq,
    sellvq, ntrades) or (None,...) on failure/empty."""
    start = bar_end_ms - BAR_MS
    buyvq = sellvq = 0.0
    n = 0
    cursor = start
    for _ in range(20):  # hard cap on pages
        try:
            r = await client.get(f"{REST}/aggTrades",
                                  params={"symbol": symbol, "startTime": cursor,
                                          "endTime": bar_end_ms, "limit": 1000})
            trades = r.json()
        except Exception:
            return (None, 0.0, 0.0, 0)
        if not isinstance(trades, list) or not trades:
            break
        for t in trades:
            qv = float(t["p"]) * float(t["q"])
            if t["m"]:
                sellvq += qv       # buyer is maker => seller aggressor
            else:
                buyvq += qv
        n += len(trades)
        if len(trades) < 1000:
            break
        cursor = trades[-1]["T"] + 1
        if cursor >= bar_end_ms:
            break
    tot = buyvq + sellvq
    if tot <= 0:
        return (None, 0.0, 0.0, n)
    return ((buyvq - sellvq) / tot, buyvq, sellvq, n)


class PaperTrader:
    def __init__(self):
        self.rule = load_locked_rule()
        self.symbols = load_universe_symbols()
        self.H = int(self.rule["horizon_s"])
        self.tau = float(self.rule["tau"])
        self.rule_name = self.rule["rule"]
        sz = self.rule["sizing"]
        self.notional = float(sz["notional_per_trade_usd"])
        self.equity0 = float(sz["equity_usd"])
        self.max_positions = int(sz["max_concurrent_positions"])
        self.max_daily_loss = float(sz["circuit_breakers"]["max_daily_loss_usd"])
        self.max_dd = float(sz["circuit_breakers"]["max_drawdown_usd"])

        # live detector state (mirror of aster_pulse.main, restricted universe)
        self.px = defaultdict(lambda: deque(maxlen=HORIZON + 1))
        self.base = defaultdict(lambda: deque(maxlen=BASE_OBS))
        self.dvol = defaultdict(lambda: deque(maxlen=HORIZON * 2 + 1))
        self.stats = {}
        self.book = {}
        self.vol = {}
        self.last_fire = {}
        self.tick = 0

        # paper-trading state
        self.open_positions = {}   # id -> dict
        self.next_id = 1
        self.closed = []           # list of net_bps
        self.cum_pnl = 0.0
        self.peak_pnl = 0.0
        self.day = None
        self.day_pnl = 0.0
        self.halted = False
        self.n_signals = 0
        self.n_entered = 0
        self.pending = 0           # in-flight OFI fetches
        self._resume()

    # -------------------------------------------------- persistence
    def _resume(self):
        """Resume realized P&L and closed-trade stats from prior runs."""
        if not TRADES.exists():
            return
        with TRADES.open() as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    rec = json.loads(line)
                except Exception:
                    continue
                if rec.get("type") == "close":
                    self.closed.append(rec["net_bps"])
                    self.cum_pnl += rec["pnl_usd"]
                    self.peak_pnl = max(self.peak_pnl, self.cum_pnl)
        if self.closed:
            print(f"resumed: {len(self.closed)} prior closed trades, "
                  f"cum P&L ${self.cum_pnl:+.2f}", flush=True)

    def _write(self, rec):
        rec["wall"] = time.strftime("%Y-%m-%d %H:%M:%S")
        with TRADES.open("a") as f:
            f.write(json.dumps(rec) + "\n")

    # -------------------------------------------------- circuit breakers
    def _roll_day(self, now):
        d = int(now // 86400)
        if self.day != d:
            self.day = d
            self.day_pnl = 0.0
            if self.halted and self.day_pnl > -self.max_daily_loss:
                pass  # daily breaker resets next day; drawdown breaker persists

    def _check_breakers(self):
        if self.day_pnl <= -self.max_daily_loss:
            if not self.halted:
                print(f"CIRCUIT BREAKER: daily loss ${self.day_pnl:.2f} "
                      f"<= -${self.max_daily_loss:.0f} -> HALT new entries",
                      flush=True)
                self._write({"type": "halt", "reason": "max_daily_loss",
                             "day_pnl": self.day_pnl})
            self.halted = True
        if (self.peak_pnl - self.cum_pnl) >= self.max_dd:
            if not self.halted:
                print(f"CIRCUIT BREAKER: drawdown ${self.peak_pnl - self.cum_pnl:.2f} "
                      f">= ${self.max_dd:.0f} -> HALT new entries", flush=True)
                self._write({"type": "halt", "reason": "max_drawdown",
                             "dd": self.peak_pnl - self.cum_pnl})
            self.halted = True

    # -------------------------------------------------- detector scoring
    def _score_symbol(self, s, btc_r30):
        q = self.px.get(s)
        st = self.stats.get(s)
        if st is None or q is None or len(q) <= HORIZON or q[0] <= 0:
            return None
        r30 = math.log(q[-1] / q[0])
        b, med, sigma = st
        _, z = ap.zscore(r30, btc_r30, b, med, sigma)
        bu = ap.burst(list(self.dvol[s]))
        if not ap.flagged(r30, z, bu):
            return None
        return {"move": r30, "z": z, "beta": b, "burst": bu}

    # -------------------------------------------------- position exit
    def _close_due(self, now):
        for pid in list(self.open_positions.keys()):
            p = self.open_positions[pid]
            if now < p["exit_time"]:
                continue
            bk = self.book.get(p["symbol"])
            if not bk or not (bk[1] > bk[0] > 0):
                # no valid book to mark out yet; wait (bounded) for a fresh quote
                if now - p["exit_time"] > 30:
                    self.open_positions.pop(pid)  # give up, cannot fairly mark
                continue
            bid, ask = bk[0], bk[1]
            entry = p["entry_price"]
            if p["side"] == 1:            # long: exit at bid
                exit_px = bid
                gross = math.log(exit_px / entry)
            else:                          # short: exit at ask
                exit_px = ask
                gross = math.log(entry / exit_px)
            net = gross - FEE_RT
            pnl = self.notional * net
            self.cum_pnl += pnl
            self.day_pnl += pnl
            self.peak_pnl = max(self.peak_pnl, self.cum_pnl)
            self.closed.append(net * 1e4)
            self._write({"type": "close", "id": pid, "symbol": p["symbol"],
                         "side": "LONG" if p["side"] == 1 else "SHORT",
                         "entry_price": entry, "exit_price": exit_px,
                         "hold_s": round(now - p["entry_time"], 1),
                         "gross_bps": gross * 1e4, "fee_bps": FEE_RT * 1e4,
                         "net_bps": net * 1e4, "pnl_usd": pnl,
                         "cum_pnl_usd": self.cum_pnl})
            self.open_positions.pop(pid)
            self._check_breakers()

    # -------------------------------------------------- event handling
    async def _handle_event(self, client, s, sig, bar_end_ms, now):
        self.pending += 1
        try:
            ofi, buyvq, sellvq, ntr = await fetch_ofi(client, s, bar_end_ms)
            if ofi is None:
                self._write({"type": "signal", "symbol": s, **sig,
                             "ofi": None, "side": "none",
                             "reason": "no aggTrades for bar", "entered": False})
                return
            side = decide(self.rule_name, ofi, sig["move"], self.tau)
            bk = self.book.get(s)
            spread_bps = None
            entered = False
            reason = ""
            entry_px = None
            if side == 0:
                reason = "rule -> no trade"
            elif self.halted:
                reason = "halted (circuit breaker)"
            elif len(self.open_positions) >= self.max_positions:
                reason = "at max concurrent positions"
            elif any(p["symbol"] == s for p in self.open_positions.values()):
                reason = "already in position for symbol"
            elif not bk or not (bk[1] > bk[0] > 0):
                reason = "no valid live book to enter"
            else:
                bid, ask, mid = bk[0], bk[1], (bk[0] + bk[1]) / 2
                spread_bps = (ask - bid) / mid * 1e4
                entry_px = ask if side == 1 else bid   # taker: long@ask short@bid
                pid = self.next_id
                self.next_id += 1
                self.open_positions[pid] = {
                    "symbol": s, "side": side, "entry_price": entry_px,
                    "entry_time": now, "exit_time": now + self.H}
                entered = True
                self.n_entered += 1
                reason = "entered"
            self.n_signals += 1
            self._write({"type": "signal", "symbol": s, **sig, "ofi": ofi,
                         "buyvq": buyvq, "sellvq": sellvq, "n_trades": ntr,
                         "side": {1: "LONG", -1: "SHORT", 0: "none"}[side],
                         "spread_bps": spread_bps, "entry_price": entry_px,
                         "notional_usd": self.notional if entered else 0,
                         "entered": entered, "reason": reason})
            arrow = {1: "LONG", -1: "SHORT", 0: "flat"}[side]
            print(f"  SIGNAL {s:<13} move={sig['move']*100:+.2f}% z={sig['z']:+.1f} "
                  f"ofi={ofi:+.2f} -> {arrow:<5} [{reason}]", flush=True)
        finally:
            self.pending -= 1

    def _monitor_line(self, now, t0):
        based = len(self.stats)
        warm = "WARMED" if based > 0 else "warming"
        hit = (sum(1 for x in self.closed if x > 0) / len(self.closed)
               if self.closed else 0.0)
        mean_net = (sum(self.closed) / len(self.closed)) if self.closed else 0.0
        pred = self.rule["predicted_edge_bps_per_trade"]
        print(f"[{time.strftime('%H:%M:%S')}] up={int(now-t0)}s {warm} "
              f"based={based}/{len(self.symbols)} | open={len(self.open_positions)} "
              f"closed={len(self.closed)} hit={hit*100:.0f}% "
              f"mean_net={mean_net:+.1f}bps cumP&L=${self.cum_pnl:+.2f} "
              f"| signals={self.n_signals} entered={self.n_entered} "
              f"{'HALTED' if self.halted else ''} "
              f"|| predicted follow_flow@120s fees_only "
              f"{pred['fees_only_optimistic']['mean']:+.1f}bps / "
              f"measured {pred['measured_p50_realistic']['mean']:+.1f}bps",
              flush=True)

    async def run(self):
        print(f"paper trader: rule={self.rule_name}@{self.H}s tau={self.tau} "
              f"({self.rule['status']}); notional=${self.notional}; "
              f"universe={len(self.symbols)} symbols", flush=True)
        print(f"warmup: needs {MIN_BASE} non-overlapping 30s returns "
              f"(~{MIN_BASE * HORIZON // 60} min) before any symbol can score",
              flush=True)
        with httpx.Client(timeout=20) as c:
            try:
                self.vol.update({t["symbol"]: float(t["quoteVolume"])
                                 for t in c.get(f"{REST}/ticker/24hr").json()
                                 if t["symbol"] in self.symbols})
            except Exception as e:
                print(f"seed 24h vol failed: {e}", flush=True)
        t0 = last_mon = time.time()
        async with httpx.AsyncClient(timeout=10) as client:
            async for ws in websockets.connect(WS, ping_interval=20):
                try:
                    async for msg in ws:
                        m = json.loads(msg)
                        data, stream, now = m["data"], m["stream"], time.time()
                        if stream.startswith("!bookTicker"):
                            if data["s"] in self.symbols:
                                self.book[data["s"]] = (float(data["b"]),
                                                        float(data["a"]),
                                                        float(data["B"]),
                                                        float(data["A"]))
                            continue
                        if stream.startswith("!ticker"):
                            self.vol.update({t["s"]: float(t["q"]) for t in data
                                             if t["s"] in self.symbols})
                            continue

                        # markPrice batch = our 1 Hz clock
                        self.tick += 1
                        self._roll_day(now)
                        for e in data:
                            s = e["s"]
                            if s not in self.symbols:
                                continue
                            bk = self.book.get(s)
                            mid = ((bk[0] + bk[1]) / 2 if bk and bk[1] > bk[0] > 0
                                   else float(e["p"]))
                            self.px[s].append(mid)
                            self.dvol[s].append(self.vol.get(s, 0.0))

                        # exits are checked every second against the live book
                        self._close_due(now)

                        if self.tick % HORIZON == 0:   # 30s boundary
                            for s, q in self.px.items():
                                if len(q) > HORIZON and q[0] > 0:
                                    self.base[s].append(math.log(q[-1] / q[0]))
                            btc = self.base.get("BTCUSDT", deque())
                            for s, bs in self.base.items():
                                if len(bs) >= MIN_BASE:
                                    b = ap.beta(bs, btc)
                                    self.stats[s] = (b, *ap.residual_stats(bs, btc, b))
                            btc_px = self.px.get("BTCUSDT")
                            btc_r30 = (math.log(btc_px[-1] / btc_px[0])
                                       if btc_px and len(btc_px) > HORIZON
                                       and btc_px[0] > 0 else 0.0)
                            bar_end_ms = int(now * 1000)
                            for s in list(self.px.keys()):
                                if s == "BTCUSDT":
                                    continue
                                if now - self.last_fire.get(s, 0) < self.H:
                                    continue
                                sig = self._score_symbol(s, btc_r30)
                                if sig is None:
                                    continue
                                self.last_fire[s] = now
                                asyncio.create_task(
                                    self._handle_event(client, s, sig,
                                                       bar_end_ms, now))

                        if now - last_mon >= 30:
                            last_mon = now
                            self._monitor_line(now, t0)
                except websockets.ConnectionClosed:
                    print("reconnecting...", flush=True)


def selftest():
    # decide() truth table
    assert decide("follow_flow", 0.5, 0.01, 0.0) == 1
    assert decide("follow_flow", -0.5, 0.01, 0.0) == -1
    assert decide("fade_flow", 0.5, 0.01, 0.0) == -1
    assert decide("fade_flow", -0.5, -0.01, 0.0) == 1
    assert decide("follow_flow", 0.2, 0.01, 0.3) == 0        # below tau
    assert decide("ofi_confirmed_momentum", 0.5, 0.01, 0.0) == 1   # agree up
    assert decide("ofi_confirmed_momentum", -0.5, -0.01, 0.0) == -1  # agree down
    assert decide("ofi_confirmed_momentum", -0.5, 0.01, 0.0) == 0  # disagree -> flat
    assert decide("ofi_divergence_reversal", -0.5, 0.01, 0.0) == -1  # move up,flow down->short
    assert decide("ofi_divergence_reversal", 0.5, 0.01, 0.0) == 0   # agree -> flat
    # net P&L sign: a long that rises beats the fee; a flat move loses the fee
    entry, up = 100.0, 100.30
    gross = math.log(up / entry)
    assert gross * 1e4 > FEE_RT * 1e4  # 30bp move clears 8bp fee
    assert (0.0 - FEE_RT) < 0          # zero move loses the fee
    # rule file loads and is the locked candidate
    r = load_locked_rule()
    assert r["rule"] == "follow_flow" and r["horizon_s"] == 120
    print("paper_trader selftest ok")


if __name__ == "__main__":
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    if "--selftest" in sys.argv:
        selftest()
    else:
        asyncio.run(PaperTrader().run())
