# Search Ledger — Aster Pulse direction study

This file is append-only and written **before** looking at results where it
concerns predeclaration. Every threshold / horizon / symbol filter / variant
tried is logged here, including abandoned ones, so the final trial count is
honest and the corrected statistics use the true number of trials.

## 0. Data substrate (verified 2026-09-01)

- `GET /fapi/v1/aggTrades` — deep history. `fromId=0` returns agg trade `a=1`
  for BTCUSDT at `T=2021-08-29`. Time-window queries (`startTime`/`endTime`)
  serve old data: BTCUSDT/ETHUSDT return trades at 365d, 730d, 1000d ago;
  ASTERUSDT (newer listing) returns data to ~180d but not 365d. Retention is
  effectively "since listing" per symbol.
- Fields: `p` price, `q` qty, `T` ms timestamp, `m` = buyer-is-maker. So
  `m=true` ⇒ aggressor was the **seller** (sell print); `m=false` ⇒ aggressor
  was the **buyer** (buy print). This gives signed order flow.
- `GET /fapi/v1/klines` 1m is the finest native candle — too coarse for a 30s
  signal, so 30s bars are built from aggTrades timestamps.
- **THE LIMITATION**: aggTrades give TRADES, not the BOOK. No historical
  bid/ask ⇒ no historical midquote or spread. Consequences, handled explicitly:
  1. Backtest entry/exit use **trade prices**, which reintroduces bid-ask
     bounce — the exact artefact the live detector avoids with midquote.
  2. Live spread distribution is measured with `research/recorder.py`
     (`!bookTicker` sampled on the 1 Hz markPrice clock) and applied as a
     modelled cost, with sensitivity reported.
  3. A rule that only works under an optimistic spread assumption is NOT shown
     to work.

## 1. Cost floor (arithmetic done before any strategy code)

- Aster taker fee = 0.04% per side ⇒ **0.08% per round trip** before spread,
  slippage, impact, funding.
- On $10,000: $100 = 1.0% of equity.
- Modelled entry+exit cost per round trip:
      cost_rt = 2 * taker_fee + spread_bps_paid + slippage
  Using taker on both legs (a 30s momentum/reversal rule crosses the spread):
      = 8 bps (fees) + spread_bps + slippage_bps.
- The live spread distribution (from the recorder) sets `spread_bps`. Even the
  most liquid Aster perps rarely quote < 1–2 bps; the median symbol is far
  wider. Crossing the spread on entry AND exit pays ~1× spread per round trip.

**Target under test:** *positive out-of-sample net expectancy with controlled
drawdown.* $100 is an outcome to measure, not a quota. Implied gross edge needed
per round trip at cost_rt: a trade must clear `cost_rt` in expectation just to
break even. If the measured net edge per trade cannot exceed ~costs, the rule is
rejected. If the implied required gross edge exceeds ~15–20 bps net of measured
spread at the natural signal frequency, the target is declared implausible
before spending compute.

## 2. Predeclared hypotheses (written before inspecting any forward returns)

Signal = detector event: |move30s| ≥ 0.5%, |z| ≥ 4 (robust, beta-adjusted vs
BTC), volume-confirmed where knowable. Two mutually exclusive directional rules:

- **H_momentum**: positive anomaly → LONG; negative anomaly → SHORT.
- **H_reversal**: positive anomaly → SHORT; negative anomaly → LONG.

Null hypothesis H0: net expectancy ≤ 0 for both, at every horizon.

Horizons: 30 / 60 / 120 / 300 s. Entry at the **next** 30s bar after the signal
plus a realistic processing delay (measured), never at the signal's own price.

Return definitions (executable, trade-price proxy for bid/ask):
- `R_long  = log(exit / entry) - cost_rt`
- `R_short = log(entry / exit) - cost_rt`
where entry/exit are modelled from trade prices with spread applied so entry is
worse and exit is worse by the modelled half-spread on each side.

## 2b. Measured live spread (recorder, ~10+ min sample, 549 symbols)

- BTCUSDT p50 0.01bp, ETHUSDT 0.04bp, SOLUSDT 0.99bp, XRPUSDT 0.73bp,
  DOGEUSDT 1.22bp, ASTERUSDT 1.44bp, BNBUSDT 0.88bp.
- Cross-symbol median of per-symbol p50 spread = 19.4bp (illiquid tail up to
  738bp). Top-of-book depth is thin (BTC ~$6k, many names <$1k).
- **Consequence:** for the liquid universe the 8bp round-trip taker fee
  dominates; total modelled `cost_rt` ≈ 8–11bp. Break-even therefore needs a
  net directional edge > ~8–11bp per round trip. Required gross edge is well
  under the "implausible" 15–20bp screen for the most liquid names, so the
  study is worth running — but the edge must clear the fee floor after the
  bid-ask bounce that trade-price entries reintroduce.

## 3. Trials log

(Appended as variants are run. Each entry: id, description, params, purpose.)

| # | date | what | params | why |
|---|------|------|--------|-----|
| — | — | predeclaration only above | — | — |
