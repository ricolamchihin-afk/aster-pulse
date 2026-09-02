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

## 4. Verdict: NULL RESULT (Phase 1, momentum/reversal)

No directional rule built on the aster_pulse detector survives realistic Aster
costs. Phase 2 (sizing) and a Phase 4 live paper-trade are not entered: there is
no validated direction to size or to freeze. The single binding reason is the
cost floor — the detector's post-anomaly move is a near-symmetric coin flip
whose only systematic component (immediate reversion) is smaller than the 0.08%
round-trip taker fee and occurs inside the untradeable gap before the first
executable price.

---

# Phase 1b — OFI-conditioned direction (expanded thesis)

## 5. Predeclarations (written 2026-09-01 BEFORE viewing any OFI-conditional forward returns)

The unsigned anomaly sign carries no tradeable direction (established NULL, §2–4).
New economic hypothesis: an anomaly driven by **net aggressive buying** tends to
continue up; one driven by **net aggressive selling** tends to continue down. The
direction predictor is `sign(ofi)` on the signal bar, where
`ofi = (buy_aggressor_qvol − sell_aggressor_qvol)/total_qvol` on the just-closed
30s signal bar. `ofi` is known at signal time (computed from the signal bar's own
aggTrades, no forward leakage) and is already stored per event in
`research/data/events.parquet` / `research/data/forward.parquet`.

### Four mutually-exclusive predeclared rules

- **follow_flow**: `ofi>0 → LONG`, `ofi<0 → SHORT` (trade with the flow).
- **fade_flow**: `ofi>0 → SHORT`, `ofi<0 → LONG` (trade against the flow).
- **ofi_confirmed_momentum**: take the move direction (`sign(move)`) **only when**
  `sign(move)==sign(ofi)`; otherwise no trade (flat).
- **ofi_divergence_reversal**: fade the move (`−sign(move)`) **only when**
  `sign(move)!=sign(ofi)`; otherwise no trade (flat).

### Parameters (fixed before results)

- Primary OFI magnitude threshold **tau = 0** (pure sign). `tau ∈ {0.3, 0.6}`
  reported as ROBUSTNESS ONLY, never used for rule selection.
- Horizons: **30 / 60 / 120 / 300 s**.
- Entry = **next-bar open** (`open[k+1]`), never the signal price. Exit `H` s later.
- Costs: **fees_only (8bp)**, **measured p50 spread**, **measured p90 spread**
  (from `research/spread_model.py`, live recorder).
- Null H0: **net expectancy ≤ 0**.

### Deploy gate (predeclared)

A rule PASSES only if, at some horizon: (i) day-cluster-bootstrap 95% CI lower
bound > 0 net of MEASURED cost, AND (ii) holds in an untouched 7d out-of-sample,
AND (iii) survives DSR > 0.95 and White's Reality Check on the CUMULATIVE trial
count, AND (iv) survives 1.5–2× measured costs. If a rule passes: LOCK it. If
none passes: LOCK the single most-promising predeclared rule (highest in-sample
mean net at its best horizon) as a CANDIDATE, and label the live run an
out-of-sample FALSIFICATION test, not a deployment.

## 6. Cumulative trial count (declared before results)

- Prior Phase 1 (§3): **24** predeclared momentum/reversal cells
  (2 hyp × 4 horizons × 3 cost scenarios).
- New Phase 1b PRIMARY set: **48** cells (4 rules × 4 horizons × 3 cost scenarios)
  at tau=0. This is the selection set.
- **Cumulative selection trials = 24 + 48 = 72.** DSR / White's Reality Check are
  computed on this honest cumulative N.
- tau robustness variants (tau ∈ {0.3,0.6}: +96 diagnostic cells) and other
  robustness slices (walk-forward, outlier trims, liquidity tiers, OFI deciles)
  are DIAGNOSTICS on the same predeclared rules; none is used to select a
  deployable rule. They are logged in §7 for full honesty but excluded from the
  selection-set correction (they would only inflate N and make passing harder,
  never easier).

## 7. Phase 1b trials log

| # | date | what | params | why |
|---|------|------|--------|-----|
| T7 | 09-01 | Phase 1b base run, 4 OFI rules × 4 horizons × 3 cost scenarios = 48 cells | tau=0, entry=next-bar open, LAT=1 bar, 33 sym, 21d | predeclared OFI direction test |
| T8 | 09-01 | Walk-forward IS(14d)/untouched-OOS(7d) on best OFI rule/horizon | fees-only + measured | does it hold OOS? |
| T9 | 09-01 | Outlier trim of best OFI cell | drop top 1/3/5/10 winners | is any +mean outlier-driven? |
| T10 | 09-01 | Liquidity tiers (>=$20M / $5-20M / $2-5M) on best OFI rule | 60s, 300s | edge in a specific tier? |
| T11 | 09-01 | OFI-decile monotonicity of GROSS forward return | 4 horizons | core thesis: does forward return rise with OFI? |
| T12 | 09-01 | tau thresholds {0.3,0.6} robustness | all rules/horizons | does a stronger flow filter help? |
| T13 | 09-01 | Search correction DSR + White's on cumulative N=72 | — | honest multiple-testing correction |

### Phase 1b result (recorded after running, discipline preserved)

- Reused the SAME 7,392 events with a usable next-bar entry (no re-download).
- **No cell of the 48 primary (tau=0) cells passes the deploy gate.** Under the
  optimistic **fees-only (8bp)** scenario the best cells are `follow_flow_120s`
  (mean +2.1bps, median −8.0, hit 46.6%, 95% CI **[−4.5, +9.5]**) and
  `ofi_confirmed_momentum_300s` (+1.9bps, CI [−8.0, +13.3]) — every CI lower
  bound ≤ 0. `fade_flow` is strongly negative everywhere (−14 to −18bps
  fees-only), so trading WITH the flow is clearly "less wrong" than against it.
- Under **measured p50 spread** every cell is −8 to −32bps; under **p90** −17 to
  −38bps. Medians are ≈ −(cost) everywhere ⇒ the median flow-conditioned trade
  still has ~zero gross directional move.
- **Walk-forward (14d IS / 7d untouched OOS), `follow_flow`:** the small fees-only
  IS means (120s +3.5) do NOT hold OOS (120s OOS +0.3, CI [−8.7, +10.9]; 30s OOS
  significantly negative). Under measured p50 every IS and OOS cell is negative.
- **Outliers:** `follow_flow_120s` fees-only mean +2.1 → **−1.7 after dropping the
  top 10 winners** of 4,017; the tiny positive mean is fat-tail, not edge.
- **Liquidity tiers:** no tier robustly positive (all CIs span 0).
- **tau robustness (diagnostic, NOT selection):** raising the flow-magnitude
  filter concentrates the (real) gross edge — under **fees-only**,
  `ofi_confirmed_momentum_120s` at **tau=0.6** reaches mean +9.5bps with 95% CI
  **[+1.3, +19.1]** (the ONLY cell with a CI lower bound > 0), and
  `follow_flow_300s` tau=0.6 mean +8.5 CI [−2.6, +20.3]. **But these fail the
  gate**: (a) tau≠0 is diagnostic, not the predeclared selection tau; (b) they
  are fees-only — under measured p50 the same cell collapses to CI [−13.2, +4.3];
  (c) n shrinks. So no deployable cell exists at measured cost.
- **OFI-decile monotonicity (the core thesis) — POSITIVE:** mean GROSS forward
  log-return rises ~monotonically across OFI deciles. Spearman(decile OFI, gross
  fwd) = **+0.75 (30s), +0.78 (60s), +0.83 (120s), +0.93 (300s)**, p = 0.013 →
  0.0002. Extreme buy-flow deciles show **+30 to +43bps GROSS at 300s**; extreme
  sell-flow deciles are negative. So **OFI genuinely carries directional
  information** — the anomaly is NOT direction-agnostic once you condition on
  signed flow. The gross signal is real; it is the **8bp+ cost floor plus the
  dilution from the noisy middle deciles at tau=0** that keeps the net rule ≤ 0.
- **Search correction (CUMULATIVE N=72 = 24 prior + 48 OFI):** best cell by mean
  (still the prior `momentum_300s` fees-only, +4.9bps) has per-trade Sharpe
  +0.021 vs deflated benchmark **SR0=0.199 ⇒ DSR=0.000** (needs >0.95); White's
  Reality Check **p=0.771** ⇒ the best of all 72 cells is fully consistent with
  luck.

### Total trials (cumulative, honest)

- **Selection set: 72** = 24 prior momentum/reversal cells + 48 OFI cells
  (4 rules × 4 horizons × 3 cost scenarios at tau=0). DSR / White's use N=72.
- Diagnostics NOT in the selection set (never used to pick a rule): tau∈{0.3,0.6}
  (+96 cells), walk-forward views, outlier trims, liquidity tiers, OFI deciles.
- **No variant passed the deploy criterion at any stage.**

## 8. Verdict: AUGMENTED NULL (deployment), with a real gross signal

OFI is a **genuine directional predictor in GROSS terms** (monotone decile
relationship, Spearman up to +0.93). But **no OFI-conditioned rule clears Aster's
~8–11bp round-trip cost floor net of measured costs**, at any of the four
predeclared rules, any horizon, in- or out-of-sample, and it fails DSR/White's on
the cumulative trial count. Per the predeclared gate, since none passed, the
single most-promising predeclared rule — **`follow_flow` @ 120s, tau=0** (highest
14d in-sample fees-only mean, +3.5bps) — is LOCKED as a **CANDIDATE**, and the
live run is explicitly an **out-of-sample FALSIFICATION test, not a deployment**
(`research/results/locked_rule.json`). The economically strongest observation
(`ofi_confirmed_momentum` @ tau=0.6, the only fees-only CI>0 cell) is recorded so
the live monitor logs `ofi` and `move` on every event and both can be evaluated
offline. Phase 2 Kelly sizing is NOT entered (no gate pass); the live trader uses
a fixed $500 notional for clean per-trade measurement, with circuit breakers.

## 9. Live paper-trading log (Part B)

- Engine: `research/paper_trader.py`, tmux session `paper-trader`, tee
  `/tmp/paper_trader.log`, decisions+fills appended to
  `research/data/paper_trades.jsonl`.
- Reconstructs the detector live on the 30s grid (imports `beta`,
  `residual_stats`, `zscore`, `flagged`, `burst` and all constants from
  `aster_pulse`), restricted to the 33-symbol universe + BTCUSDT beta reference.
- On each `flagged` event it REST-queries `aggTrades` for the just-closed 30s bar,
  computes OFI (m=true ⇒ seller aggressor), applies `follow_flow`, and simulates a
  taker fill on the REAL live book (LONG@ask / SHORT@bid), marking out 120s later
  (LONG@bid / SHORT@ask). Net = directional log return − 8bp fee; the crossed
  spread is paid, not double-counted (recorded as `spread_bps`).
### Live monitoring result (first ~40 min of live scoring; trader left running)

- Warmup completed cleanly: `based=33/33` after ~20 min (MIN_BASE=40 × 30s).
- **18 detector events, 17 entered, 17 closed.** One event had no aggTrades for
  its bar (OFI undefined) and was correctly skipped (`side=none`).
- Sides: 14 LONG / 3 SHORT (follow_flow tracks flow, not the move — e.g. PONS
  move=+0.51% but ofi=−0.98 → SHORT; 牛来 move=−1.17% ofi=+0.72 → LONG).
- **mean net −21.4 bps, median −44.6 bps, hit 35%, cum P&L −$18.15 on $10k.**
  Per-trade net is wildly fat-tailed (+244 to −184 bps). Median spread PAID at
  entry = **19.3 bps** (range 0.9–29.9) — the detector fires almost only on the
  widest-spread low-caps, so live realized cost exceeds the universe-wide backtest
  cost, and the live mean (−21.4) is below the measured-p50 backtest prediction
  (−12.1) and far below the optimistic fees-only (+2.1).
- The one tight-spread fill (PONS @ 0.9bp) closed at −1.6 bps ≈ just the 8bp fee.
- **Live falsification corroborates the augmented NULL.** No live order placed;
  fills simulated against real live quotes. Trader remains running in tmux
  `paper-trader`; log `/tmp/paper_trader.log`; data `data/paper_trades.jsonl`.

# Phase 1c — Longer holding periods (predeclared 2026-09-02 BEFORE viewing returns)

The 30/60/120/300s set is a meme-style scalp window. The fee floor is **fixed
per round trip**, so a larger move over minutes-to-hours could theoretically
clear the same 8–11bp cost. This is a different economic question, not a
re-search of short horizons.

## 10. Predeclarations (written before computing any 15m/1h/4h return)

- **Direction is locked:** only `follow_flow` (ofi>0→LONG, ofi<0→SHORT) at
  tau=0. Do not re-open fade_flow / confirmed-momentum / divergence as
  selection candidates. fade_flow is reported as a one-line sanity check
  (should stay negative if the thesis is continuation).
- **New horizons:** 15 minutes (900s), 1 hour (3600s), 4 hours (14400s).
  300s is restated as a baseline already known, not a new trial.
- **Entry/exit unchanged:** next-bar open after the signal; exit at the open
  `H` seconds later. Independent-event mask uses the longer window (adjacent
  4h holds on the same symbol are not independent).
- **Costs:** fees_only (8bp), measured p50, measured p90, PLUS any **funding**
  timestamp that falls strictly inside (entry, exit]. Long pays the funding
  rate; short receives it. Aster funding is typically 8-hourly, so 15m/1h
  usually pay 0 and 4h pays 0 or 1 print.
- **Walk-forward:** same 14d IS / 7d untouched OOS as Phase 1b. Touch OOS once.
- **Null / gate:** same as §5. New selection cells = 3 horizons × 3 cost
  scenarios = **9**. Cumulative selection N = 72 + 9 = **81**.
- The live paper-trader stays locked at 120s. Do not retune it mid-falsification.

| # | date | what | params | why |
|---|------|------|--------|-----|
| T14 | 09-02 | Longer-hold follow_flow at 15m / 1h / 4h | tau=0, +funding, 3 cost scenarios = 9 cells | does a swing hold clear the fixed fee? |

### Phase 1c result (recorded after the run)

- **No cell of the 9 passes.** Holding longer does not clear the fee; it makes
  `follow_flow` worse.
- 15m fees-only: mean +3.1bps, median −10.0, hit 48%, CI [−14.3, +20.2], n=2178.
  Untouched OOS: −6.2bps (CI [−20.0, +7.6]).
- 1h fees-only: mean −22.1bps, median −30.1, OOS −40.6bps (CI excludes 0).
- 4h fees-only: mean −24.0bps, median −58.0, OOS −74.5bps (CI excludes 0).
- Measured-spread cells are more negative still. Independent n drops as required
  (4h: 621 independent events).
- fade_flow sanity (not a candidate): mean flips slightly positive at 1h/4h
  (+6 / +8bps fees-only) but CIs include 0. That is consistent with later
  mean-reversion, **not** a reason to reopen fade_flow as a selection rule.
- Cumulative selection N stays **81**. Live paper-trader remains locked at 120s.

# Phase 1d — +60% 24h runner fade (predeclared 2026-09-02 BEFORE viewing forwards)

This is a **different event** from the 30s detector. The claim: names that have
already printed **+60% or more over the trailing 24 hours** are typically
followed by a decent reversion, so the trade is SHORT. That thesis was not
tested in Phases 1–1c (those used 30s z-score flags, and "24h" in the detector
is quote *volume*, not price change).

## 11. Predeclarations

- **Event:** trailing 24h simple return `close[t]/close[t-24h] − 1` first
  crosses ≥ **+60%** from below (one fire per excursion; no re-fire until the
  24h return drops back under 60%).
- **Direction:** SHORT only (fade the runner). LONG is not a candidate.
- **Entry:** next 30s bar open after the cross. Exit at 1h / 4h / 12h / 24h.
- **Costs:** 8bp fee + measured p50/p90 spread + funding prints inside the hold.
- **Universe:** the same 33 liquid perps we already have 21d bars for. If the
  independent-event count at +60% is too small to estimate a mean (n < 20),
  **stop and say so** — do not silently switch to +20%/+40% as the selection
  rule. Those lower thresholds may be reported only as sample-size diagnostics.
- **Null / gate:** same as §5. New selection cells = 4 horizons × 3 costs = **12**.
  Cumulative N = 81 + 12 = **93** if the +60% sample is large enough to evaluate.

| # | date | what | params | why |
|---|------|------|--------|-----|
| T15 | 09-02 | Fade +60% 24h runners | SHORT, 1h/4h/12h/24h, +funding | has the "decent reversion after a +60% day" thesis been tested? |

### Phase 1d result

- **This thesis had not been tested before this run.**
- Live tape at test time: **0** names with 24h pct ≥ +60% (top was UAI +43.6%).
- Historical 21d / 33 names: 258 +60% crossings (6 symbols, almost all meme:
  牛来 94, PONS 68, AKE 51, CASHCAT 24, BTR 14, MARSCOIN 7). 230 with an entry.
- After the independence mask: n=61 at 1h, n=28 at 4h; **12h/24h n<20 → stop**.
- SHORT after +60% does **not** show a decent, tradeable reversion:
  1h fees-only mean **−36.9bps**, median +3.7bps, hit 52.5%, CI [−266, +168].
  4h fees-only mean **−162bps**, median −94bps. The median 1h fade is a few
  bps — not "decent" — and the mean is wrecked by runners that **keep going**.
- No cell has CI lower bound > 0. Not a candidate. Cumulative N = 93 if
  counted; the 12h/24h cells were not evaluated (insufficient n).

# Phase 1e — Revised thesis (predeclared 2026-09-02 BEFORE viewing forwards)

The detector-as-trigger + taker stack is falsified. Revised claim: **on tight
books, extreme signed flow predicts the next 1–5 minutes, but only if you are
a maker and you stand down on an accelerating runner.**

## 12. Predeclarations

- **Universe (frozen from live recorder at predeclaration):** perps with
  p50 spread ≤ 5 bp AND top-of-book p50 ≥ $2,000, intersected with symbols
  that already have 21d bars. That list is:
  `BTCUSDT, ETHUSDT, BTCUSD1, XAUUSD1, ETHUSD1, SOLUSDT, CLUSDT, CLUSD1,
  ASTERUSDT, XAGUSDT, SNDKUSD1, SOLUSD1, SPCXUSD1, MUUSD1, SKHYNIXUSD1`
  (15 names). No download of extra symbols. No meme names.
- **Event:** every 30s bar with |OFI| ≥ 0.6. **No** z-score / MIN_MOVE /
  detector flag. OFI = (buyvq − sellvq)/vq on that bar.
- **Side:** follow_flow (ofi>0→LONG, ofi<0→SHORT).
- **Runner veto:** if trailing 24h simple return ≥ +40% **and** ofi > 0,
  **stand down** (no trade). Do not chase an accelerating runner. If 24h ≥ 40%
  and ofi < 0, SHORT is allowed (flow flipped).
- **Execution (maker only):** after the signal bar k, rest during bar k+1
  (30s). LONG fills only if sell-aggressor quote vol in k+1 ≥ $500; SHORT
  fills only if buy-aggressor quote vol in k+1 ≥ $500. Else **cancel**.
  Unfilled signals count as **0** in the primary mean (missed-fill bias).
  Entry price = close of the fill bar (trade-price proxy; no spread paid).
  Maker fee = **0%** (Aster perps, fee schedule as of Feb 2026).
- **Hold:** 60 / 120 / 300 s from the fill bar. Primary exit assumes maker
  (0 fee). Diagnostic only: exit as taker (4 bp).
- **Adverse-selection diagnostic (not selection):** long entry at fill-bar
  low, short entry at fill-bar high.
- **Walk-forward:** same 14d IS / 7d untouched OOS. Touch OOS once.
- **Gate:** primary (0 fee, unfilled=0) 95% CI lower bound > 0 at some
  horizon, **and** holds in untouched OOS. New selection cells = **3**.
  Cumulative N = 93 + 3 = **96**.
- Live paper-trader stays on the old 120s taker candidate. Do not retune it.

| # | date | what | params | why |
|---|------|------|--------|-----|
| T16 | 09-02 | Maker + liquid book + |OFI|≥0.6 follow_flow | 15 names, 60/120/300s, unfilled=0 | revised thesis |
