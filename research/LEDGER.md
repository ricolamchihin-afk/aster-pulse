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
| T1 | 09-01 | Phase 1 base run, 2 hyp × 4 horizons × 3 cost scenarios = 24 cells | universe qvol>=$2M (33 sym), 21d, entry=next-bar open, LAT=1 bar | predeclared direction test |

### Phase 1 result (recorded before robustness)

- events = 8805 detector fires; 7392 with a usable next-bar entry price.
- **No cell passes.** Under fees-only (optimistic 8bp): momentum mean net
  −3.7→+4.9 bps across 30→300s, median ≈ −8 bps, hit rate 43–46%, and every
  95% CI lower bound ≤ 0. Reversal is strongly negative (−12→−21 bps).
- With measured p50 spread every cell is −8→−34 bps; with p90 spread −14→−40 bps.
- Median net ≈ −(cost) everywhere ⇒ the median trade has ~zero gross directional
  move; the signal is a coin flip minus costs. The only positive mean
  (momentum_300s, fees-only, +4.9bps) has median −9.2bps and CI [−4.9, +16.0]
  ⇒ fat-tail / outlier artefact, fails the deploy criterion.
- Next: robustness (walk-forward OOS, outlier trim, liquidity tiers, time-of-day)
  and search correction (DSR / White's) to confirm the null. Phase 2 sizing is
  NOT entered because Phase 1 produced no direction.

| T2 | 09-01 | Walk-forward IS(14d)/OOS(7d), momentum, fees-only | 4 horizons | does anything hold OOS? |
| T3 | 09-01 | Outlier trim of momentum_300s fees-only | drop top 1/3/5/10 winners | is the +mean outlier-driven? |
| T4 | 09-01 | Liquidity tiers (>=$20M / $5-20M / $2-5M), momentum | 60s, 300s | edge in a specific tier? |
| T5 | 09-01 | Time-of-day buckets, momentum_300s fees-only | 4×6h UTC | hour-of-day dependence |
| T6 | 09-01 | Look-ahead ceiling (enter at signal price, NOT tradeable) | 4 horizons, mom+rev | strict optimistic upper bound |

### Robustness result (confirms null)

- **Walk-forward:** no cell has CI lower bound > 0 in IS or OOS; positive means
  carry negative medians (fat tail). 30s OOS is significantly negative.
- **Outliers:** momentum_300s fees-only mean +4.9bps → −0.5bps after dropping
  just the top 10 winners of 3133. Rests on a handful of outliers.
- **Tiers:** no liquidity tier is robustly positive (all CIs span 0).
- **Look-ahead ceiling:** entering at the signal's OWN price (impossible to
  trade), the max gross reversion is 5.8bps (30s) — BELOW the 8bp fee floor.
  Momentum net negative at every horizon; reversal net negative at every
  horizon. No edge exists even before requiring an executable entry.
- **Search correction (N=24 predeclared cells):** best cell per-trade
  Sharpe +0.021 vs deflated benchmark SR0=0.169 ⇒ DSR=0.000 (needs >0.95).
  White's Reality Check p=0.488 ⇒ best cell fully consistent with luck.

### Total trials

- 24 predeclared Phase-1 cells (2 hyp × 4 horizons × 3 cost scenarios) — the
  set used for the DSR / White's correction.
- Robustness slices (T2–T6): walk-forward views (8), outlier trims (5), tier
  slices (6), time-of-day (4), look-ahead ceiling (8). These are diagnostics on
  the SAME predeclared hypotheses; none was used to select a deployable rule,
  because none passed. No threshold, horizon, or filter was tuned to "find" a
  passing variant. **No variant passed the deploy criterion at any stage.**

## 4. Verdict: NULL RESULT

No directional rule built on the aster_pulse detector survives realistic Aster
costs. Phase 2 (sizing) and a Phase 4 live paper-trade are not entered: there is
no validated direction to size or to freeze. The single binding reason is the
cost floor — the detector's post-anomaly move is a near-symmetric coin flip
whose only systematic component (immediate reversion) is smaller than the 0.08%
round-trip taker fee and occurs inside the untradeable gap before the first
executable price.
