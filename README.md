# Aster Pulse

Live abnormal-move scanner for [Aster DEX](https://asterdex.com) perpetuals, with a
local dashboard. Read-only: no API keys, no orders, nothing to authenticate.

**It is an attention flag, not a trade signal.** It has never been backtested and it
does not tell you which way to trade. See [Direction](#direction-is-never-asserted).

```bash
python aster_pulse.py              # live + dashboard at http://127.0.0.1:8787
python aster_pulse.py --no-serve   # terminal feed only
python aster_pulse.py --test       # self-check, no network
```

Windows: `.\run.ps1` creates the venv, installs deps, and launches in one step.

---

## What the venue actually looks like

Measured, not assumed:

| Source | Symbols | Note |
|---|---|---|
| `GET /fapi/v1/exchangeInfo` | 573 | 553 `TRADING`, 15 `SETTLING`, 5 `PENDING_TRADING` |
| `GET /fapi/v1/ticker/24hr` | 568 | all carry non-zero `quoteVolume` |
| `!markPrice@arr@1s` (ws) | 721 unique in 20s | every listed perp, every second |
| `!ticker@arr` (ws) | **45 unique in 25s** | only symbols that actually ticked |

Three findings that drove the design:

1. **`!ticker@arr` is not an all-market feed in practice.** It pushes only symbols whose
   ticker changed that second — 45 of ~570 over 25 seconds. The illiquid tail, where
   abnormal moves are most likely, was invisible to a scanner built on it.
2. **`!markPrice@arr@1s` is the real all-perps firehose**, and it gives a *uniform time
   grid*: on the ticker stream "30 samples" meant 30 seconds on BTC and several minutes
   on a dead name, so scores were silently incomparable across symbols.
3. **Aster lists perps with CJK symbol names** (`我踏马来了USDT`, `牛来USDT`, `黑马USDT`).
   Printing them crashes the default Windows cp950 console, which would kill the live
   loop mid-run. `stdout` is reconfigured to UTF-8 at startup.

Open interest has **no bulk endpoint** — `/fapi/v1/openInterest` requires a symbol — so
OI is polled only for symbols currently on the board.

## How detection works

Per symbol, at 1 Hz:

```
mid     = (bid + ask) / 2                    from !bookTicker
r30     = log(mid_t / mid_{t-30s})           the move
beta    = cov(base_sym, base_btc) / var(base_btc)      rolling, clamped to [0, 3]
x       = r30 - beta * r30_btc               idiosyncratic move
z       = (x - median(resid)) / (1.4826 * MAD(resid))
burst   = mean(dvol[-30:]) / median(dvol[:-30])        None if no baseline
```

- **Midquote, not last trade or mark.** Last-trade prices manufacture false short-horizon
  moves through bid–ask bounce.
- **Median/MAD, not mean/stdev.** Crypto returns are fat-tailed; three outliers barely
  move a MAD estimate but wreck a standard deviation. Read `z` as a standardised score,
  never as a Gaussian probability.
- **The baseline is non-overlapping.** Adjacent 30-second returns share 29 of their 30
  seconds, so an overlapping baseline is not 60 independent observations — it is two.
  Baselines are extended on one global 30s boundary, which also makes index `i` the same
  wall-clock window for every symbol, which is what makes the beta regression valid.
- **Beta-adjusted.** Without it, one BTC-driven market jump flags most symbols at once.
- **Volatility floor** (`VOL_FLOOR`) on sigma. Mark prices go stale; a symbol sitting at
  one price then ticking once otherwise scores `z = +694` on a 0.001% move.

### Gates

| Gate | Threshold | Why |
|---|---|---|
| `MIN_MOVE` | \|move\| ≥ 0.5% | Absolute floor |
| `Z_MIN` | \|z\| ≥ 4 | Unusual *for this symbol* |
| `VOL_MULT` | burst ≥ 3×, or unknown | Volume confirms, where volume is knowable |
| `tradeable` | spread ≤ 20bp, top-of-book ≥ $500, 24h ≥ $1M | Three times almost-zero volume is still untradeable |

Plus a 60s per-symbol cooldown. Statistically flagged but untradeable symbols are not
dropped — they pin to the watchlist tagged `untradeable`.

A symbol needs `MIN_BASE` = 40 non-overlapping 30s returns — **about 20 minutes** —
before it can be scored at all.

## Direction is never asserted

Every row reads `WAIT`. This is deliberate, not unfinished.

Continuation-vs-reversal after an anomaly has **not been tested** on Aster data. A
detector says *something unusual happened*; it does not say which way to trade it.
Emitting LONG or SHORT from an untested rule would be inventing a signal.

`direction()` is the single place a validated rule plugs in. To earn one:

1. Predeclare both hypotheses — momentum (positive anomaly → long) and reversal
   (positive anomaly → short).
2. Measure forward returns at 30 / 60 / 120 / 300s from **executable** prices:
   `R_long = log(bid_exit / ask_entry) − costs`, entering at the next available quote
   after the signal plus real processing delay, never at the signal's own midprice.
3. Costs must include fees, spread, book impact, measured latency, slippage and funding
   when crossed. Aster's 0.04% taker fee per side is an 0.08% round trip *before*
   spread and slippage.
4. Chronological walk-forward only — never shuffled cross-validation. Keep simultaneous
   symbols in the same split, drop overlapping observations at split boundaries, and
   preserve one untouched final test period.
5. Record every threshold, horizon and symbol variant tried, and correct for the search
   (Deflated Sharpe / White's Reality Check).
6. Deploy only if the lower 95% bound of net expectancy stays above zero and the result
   survives 1.5–2× measured costs. Then paper-trade the locked rule before any capital.

## Dashboard

`--serve` (the default) starts a stdlib `http.server` on `127.0.0.1:8787` and pushes one
full JSON snapshot per second over **Server-Sent Events** — `EventSource` is native to the
browser, data flows one way, reconnection is free. No framework, no build step, no
JavaScript dependency.

| Panel | Shows |
|---|---|
| **Radar** (left) | Anomalies that passed every gate, newest first, with side, move, idiosyncratic move, z and funding |
| **Watchlist** (right) | Flagged-and-undecided pinned to the top, then the largest 30s movers, with sparkline, funding and OI |

Controls: **Pause** freezes rendering while the stream keeps running; **hide THIN**
filters the sub-$1M tail.

Design notes: direction is never colour alone (green↔red measures ΔE 7.4 under
deuteranopia — inside the band that is only permissible with secondary encoding), so
every move carries a ▲/▼ glyph, a signed number, and a screen-reader-only "up"/"down".
The alert feed is not an `aria-live` region — a single `role="status"` announces a
contextual phrase only when the count changes.

## Known limits

- **Volume-burst confirmation covers ~45 symbols.** Everything else scores on price
  alone and shows `n/a`. Per-symbol `@aggTrade` streams would fix it, but the 200-stream
  connection cap means ~4 connections for full coverage.
- **No order-flow imbalance.** Signed aggressive flow is more informative than unsigned
  volume at these horizons, but it needs per-symbol aggTrade for the same reason.
- **24h volume is seeded once** at startup and refreshed only for actively trading
  symbols. Re-poll if you run for days.
- **Mark price fallback.** Symbols with no book yet fall back to mark price, which is
  smoother than the tape.
- **No persistence.** Alerts go to stdout and the dashboard holds the last 60 in memory.
  Pipe to a file for history.
- **Dashboard is localhost-only and unauthenticated.** Bound to `127.0.0.1` on purpose.
- **Sparklines are normalised per symbol**, so their vertical scales are not comparable
  to each other. They show shape; the Move column carries magnitude.
- **Not backtested.** No edge has been demonstrated. This is a monitor.

## Self-check

`--test` runs 22 assertions with no network: robust-sigma outlier resistance, the
volatility floor, beta recovery and clamping, market-move removal, every executability
gate, the alert gate, unknown-vs-zero volume, direction always returning WAIT, and
sparkline normalisation.

## Layout

```
aster_pulse.py     scanner + dashboard server
dashboard.html     the UI
run.ps1            Windows: venv + deps + launch
requirements.txt   httpx, websockets
```
