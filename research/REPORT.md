# Can the Aster Pulse detector be turned into a profitable rule on Aster perps?

**Verdict: NULL RESULT.** No directional rule built on the `aster_pulse`
detector survives realistic Aster trading costs. The evidence below is from real
Aster data only (historical `aggTrades` + live `!bookTicker`); no prices were
synthesised and no other venue was substituted. No live order was placed.

The reproduction pipeline: `download_universe.py` → `reconstruct.py` →
`phase1.py` → `phase3.py` → `diagnostics.py`. Raw results in `results/*.json`,
the full search ledger in `LEDGER.md`.

---

## 0. Data substrate (verified, not assumed)

- **`GET /fapi/v1/aggTrades` has deep history.** `fromId=0` returns BTCUSDT agg
  trade `a=1` at `2021-08-29`; time-window queries serve old data (BTC/ETH at
  365d/730d/1000d ago; newer listings like ASTERUSDT to ~180d). Retention is
  effectively "since listing" per symbol — sample size is not the binding
  constraint.
- Fields give **signed order flow**: `m=true` ⇒ aggressor was the seller.
- **The book has no history.** aggTrades are trades, not quotes, so historical
  midquote/spread cannot be reconstructed. Handled explicitly (below), not
  papered over.
- 1m klines are the finest native candle, so all 30s bars are built from
  aggTrades timestamps (`fetch_bars.py`, verified full-coverage: BTC 3d = exactly
  8,640 × 30s bars).

## 1. Cost floor (arithmetic before any strategy code)

- Aster taker fee 0.04%/side = **0.08% (8bp) round trip** before spread/slippage.
- **Measured live spreads** (`recorder.py`, 552 symbols, ~1 hr): the liquid
  universe is tight — BTC 0.01bp, ETH 0.04bp, SOL 0.99bp, DOGE 1.22bp, ASTER
  1.44bp — so the **8bp fee dominates**; total `cost_rt ≈ 8–11bp`. The illiquid
  tail is very wide (cross-symbol median 19bp, up to 738bp) and is untradeable
  for a $10k book.
- **Target tested:** positive out-of-sample net expectancy with controlled
  drawdown (the objective), *not* a $100/day quota. The $100/day quota implies
  ~2.6%/day gross at 20 round trips/day — declared implausible up front. The
  break-even hurdle a rule must clear is a net directional edge > ~8–11bp per
  round trip.

## 2. Signal reconstruction (faithful to the detector)

`reconstruct.py` rebuilds the detector exactly on its native 30s grid:
`r = log(close/close_prev)`, rolling BTC-beta over ≤240 bars (≥40 to score,
clamped [0,3]), residual median/MAD z-score, and a volume-burst gate (always
applied — aggTrades give volume for every symbol, unlike the ~45 that carry live
`!ticker`). The reconstruction matches `aster_pulse`'s own
`beta`/`residual_stats`/`zscore` to 1e-9 (`selftest_equivalence`).

- Universe: 33 perps with 24h quote volume ≥ $2M (the detector's tradeable tier)
  + BTC as beta reference. Window: 21 days, ~11.6M trades folded into 30s bars.
- **8,805 detector events** fire; 7,392 have a usable next-bar entry price.
  Events concentrate in volatile low-caps (memecoins), not the majors (BTC ≈ 0
  by construction; ETH 29, SOL 48).

## 3. Phase 1 — DIRECTION (predeclared)

Two predeclared hypotheses (written before viewing forward returns):
**momentum** (anomaly → same direction) vs **reversal** (anomaly → opposite).
Entry = the **next** 30s bar's open after the signal (never the signal's own
price); exit `H` seconds later; `H ∈ {30,60,120,300}s`. Returns are executable
trade prices net of costs. Null H0: net expectancy ≤ 0.

Independent-event net return per trade (bps), 95% CI by day-cluster bootstrap:

| cost scenario | best cell | mean | median | hit | 95% CI | verdict |
|---|---|---|---|---|---|---|
| fees only (8bp) | momentum_300s | +4.9 | −11.0 | 46% | [−4.9, +16.0] | no edge |
| + measured p50 spread | momentum_300s | −8.3 | −20.8 | 42% | [−17.8, +2.4] | no edge |
| + measured p90 spread | momentum_300s | −14.5 | −25.9 | 40% | [−24.0, −4.0] | no edge |

- **No cell (of 24) has a 95% CI lower bound above zero** — the deploy criterion
  fails at the first gate. Reversal is strongly negative everywhere (−12 to
  −40bps). Every hit rate is < 50%.
- Median net ≈ −(cost) in every cell ⇒ the median post-anomaly move is ~zero;
  the signal is a coin flip minus costs. The lone positive mean (momentum_300s,
  fees-only) has a negative median → a fat right tail, not an edge.

![Direction test — every predeclared cell](../artifacts/direction_test_cells.png)

![Momentum @300s net distribution](../artifacts/net_return_distribution_mom300.png)

## 4. Robustness (all confirm the null)

- **Walk-forward** (14d in-sample / 7d untouched out-of-sample, touched once):
  no cell has CI lower bound > 0 in IS or OOS; positive means carry negative
  medians; 30s OOS is significantly negative.
- **Outlier sensitivity:** momentum_300s fees-only mean **+4.9 → −0.5bps after
  dropping the top 10 winners** of 3,133. The apparent edge is a handful of
  outliers — "if it rests on three outliers it is not a strategy".
- **Liquidity tiers** (≥$20M / $5–20M / $2–5M): none robustly positive.
- **Look-ahead ceiling** (enter at the signal's own price — impossible to trade,
  a strict upper bound): max gross reversion is **5.8bps at 30s, below the 8bp
  fee floor**; momentum net negative at every horizon, reversal net negative at
  every horizon. So the only systematic move is an *immediate reversion that
  lives inside the untradeable gap* between signal formation and the first
  executable price, and it is too small to beat fees regardless.
- **Search correction (N=24 predeclared cells):** best per-trade Sharpe +0.021
  vs deflated benchmark SR0=0.169 ⇒ **DSR = 0.000** (pass needs > 0.95); White's
  Reality Check **p = 0.488** ⇒ the best cell is entirely consistent with luck.

## 5. The book-history limitation, handled

- The backtest enters/exits on **trade prices**, which reintroduces bid-ask
  bounce — the exact artefact the live detector avoids with midquote. This adds
  variance and, if anything, flatters a mean-reversion read of entries.
- Spread is applied as a **modelled cost from live measurement**, with
  sensitivity across fees-only / p50 / p90. The result is robust to it because
  even the **fees-only** case (no spread at all) already fails.
- The result therefore does **not** depend on an optimistic spread assumption.

## 6. What would change the answer (to revisit, not to deploy now)

- **Order-flow imbalance (OFI):** captured per bar (`buyvq`/`sellvq`) but not yet
  used as the direction input; signed aggressive flow is more informative than
  unsigned volume and is the most plausible next feature. It would still have to
  clear the 8bp fee floor.
- **Sub-30s execution microstructure:** a maker/limit entry (earning rather than
  paying the spread, and avoiding taker fees) changes the cost model
  fundamentally; that is a different, execution-first study.
- **A live book recorder over weeks** to backtest the true executability gate
  (spread ≤ 20bp, top-of-book ≥ $500) instead of modelling it.

## 7. Phases 2 & 4

Not entered by design. Phase 2 (sizing) and Phase 4 (freeze + live paper-trade)
presuppose a validated direction; there is none to size or to freeze. The live
spread recorder (`recorder.py`) is running and persisting forward book data, so
a future candidate rule could be paper-traded against it without new plumbing.

---

# Expanded thesis: OFI direction + live paper-trading

**Verdict: AUGMENTED NULL for deployment — but with a genuine gross signal.**
Order-flow imbalance (OFI = signed aggressive quote volume on the signal bar)
*does* predict the direction of the post-anomaly move in GROSS terms, yet no
OFI-conditioned rule clears Aster's ~8–11bp round-trip cost floor net of measured
costs. Because the user explicitly wants to explore this live, the most-promising
predeclared rule is locked as a **CANDIDATE** and taken to a real-time paper
trader as an **out-of-sample falsification test**, not a deployment.

Pipeline: `phase1b_ofi.py` (backtest) → `phase3b_ofi.py` (robustness + search
correction) → `paper_trader.py` (live). Raw results in `results/phase1b_ofi.json`,
`results/phase3b_ofi.json`, locked params in `results/locked_rule.json`, full
predeclaration + trial log in `LEDGER.md` §5–9.

## 8. The thesis and its predeclaration

The unsigned anomaly sign is direction-agnostic (§3–4, established NULL). New
hypothesis: an anomaly driven by **net aggressive buying** continues up; one
driven by **net aggressive selling** continues down. Direction predictor =
`sign(ofi)`, `ofi=(buyvq−sellvq)/vq` on the signal bar (known at signal time, no
leakage; already stored per event). Four mutually-exclusive rules were
**predeclared before viewing any conditional return** (LEDGER §5):
`follow_flow`, `fade_flow`, `ofi_confirmed_momentum`, `ofi_divergence_reversal`.
Primary threshold tau=0; tau∈{0.3,0.6} robustness only. Entry = next-bar open,
horizons 30/60/120/300s, costs fees-only / measured p50 / p90. Null H0: net ≤ 0.

## 9. Backtest result (48 OFI cells, same 7,392 events)

| cost scenario | best cell | mean | median | hit | 95% CI | verdict |
|---|---|---|---|---|---|---|
| fees only (8bp) | follow_flow_120s | +2.1 | −8.0 | 47% | [−4.5, +9.5] | no edge |
| + measured p50 | ofi_confirmed_momentum_300s | −11.1 | −20.7 | 42% | [−20.8, −0.0] | no edge |
| + measured p90 | ofi_confirmed_momentum_300s | −16.9 | −25.2 | 41% | [−26.5, −6.0] | no edge |

_(Measured-spread cells drift ≈0.5 bps as the live recorder appends; fees-only,
decile, and DSR/White's numbers are spread-independent and stable. No drift
changes any verdict — every measured-cost CI stays ≤ 0.)_

- **No cell (of 48) has a 95% CI lower bound above zero net of measured cost.**
  `fade_flow` is strongly negative everywhere (−14 to −38bps) → trading WITH the
  flow is clearly less wrong than against it, consistent with a weak continuation.
- **Walk-forward:** the small fees-only IS means do not survive the untouched 7d
  OOS; under measured cost every IS/OOS cell is negative.
- **Outliers:** `follow_flow_120s` fees-only +2.1 → −1.7 after dropping 10 of
  4,017 winners.
- **tau robustness (diagnostic, not selection):** raising the flow filter to
  tau=0.6 concentrates the real gross edge — `ofi_confirmed_momentum_120s`
  reaches mean +9.5bps, 95% CI **[+1.3, +19.1]** under *fees-only* (the only
  CI>0 cell in the whole study). It still **fails the gate**: tau≠0 is not the
  predeclared selection tau, and under measured p50 it collapses to [−13.2, +4.3].
- **Search correction, cumulative N=72** (24 prior + 48 OFI): best cell per-trade
  Sharpe +0.021 vs deflated SR0=0.199 ⇒ **DSR=0.000**; White's **p=0.771**.

## 10. The core thesis test — OFI-decile monotonicity (POSITIVE)

Bucketing events into OFI deciles, the mean **GROSS** forward log-return rises
~monotonically with signed flow: Spearman(decile OFI, gross fwd) = **+0.75 / +0.78
/ +0.83 / +0.93** at 30/60/120/300s (p = 0.013 → 0.0002). Extreme buy-flow deciles
show **+30 to +43bps gross at 300s**; extreme sell-flow deciles are negative. So
**OFI genuinely carries directional information** — the reason the net rule still
fails is the 8bp+ cost floor plus the dilution from the noisy middle deciles at
tau=0, not an absence of signal.

![OFI-decile monotonicity (core thesis)](../artifacts/ofi_decile_monotonicity.png)

![OFI direction test — all 16 cells × 3 cost scenarios](../artifacts/ofi_direction_test_cells.png)

## 11. Locked candidate + live paper-trading design (Part B)

Per the predeclared gate, since nothing passed, the single most-promising
predeclared rule is locked as a **CANDIDATE**: **`follow_flow` @ 120s, tau=0**
(highest 14d in-sample fees-only mean, +3.5bps). `research/results/locked_rule.json`.

`research/paper_trader.py` runs a real-time paper trader off the SAME live feed
as the detector (`!markPrice@arr@1s/!bookTicker/!ticker@arr`), restricted to the
33-symbol universe + BTC:

- Reconstructs the detector on the live 30s grid, importing `beta`,
  `residual_stats`, `zscore`, `flagged`, `burst` and all constants from
  `aster_pulse` (detector logic untouched).
- On each `flagged` event, REST-queries `aggTrades` for the just-closed 30s bar,
  computes OFI (m=true ⇒ seller aggressor), applies `follow_flow` → LONG/SHORT/none.
- Simulates a **taker fill on the REAL live book** — LONG @ best ask, SHORT @ best
  bid — and marks out 120s later (LONG @ bid, SHORT @ ask). Net = directional log
  return − 8bp fee; the crossed spread is genuinely paid (recorded as
  `spread_bps`), **not double-counted**. This live cost model is *more* realistic
  than the historical trade-price backtest, which had no book.
- Fixed $500 notional (candidate, not Kelly), 3× leverage cap, isolated margin,
  ≤6 concurrent positions, and circuit breakers (max daily loss $200 / max
  drawdown $500 both HALT new entries). Every decision and fill is appended to
  `research/data/paper_trades.jsonl` (resumes across restarts). No live order is
  ever placed.

## 12. Live monitoring results (falsification, ongoing)

The paper trader warmed up cleanly (`based=33/33` after ~20 min, the detector's
`MIN_BASE=40` non-overlapping 30s returns) and then scored the live feed. In the
first ~40 min of live scoring it produced **18 detector events, 17 entered, 17
closed** (the trader is **left running** for continued accumulation; anomalies are
rare, ~17/hr across the universe):

| metric | live (17 closed) | backtest predicted |
|---|---|---|
| mean net / trade | **−21.4 bps** | +2.1 (fees-only) / −12.1 (measured p50) |
| median net / trade | −44.6 bps | −8.0 / −19.6 |
| hit rate | 35% | 47% / 41% |
| cum P&L on $10k | **−$18.15** | — |
| median spread PAID at entry | **19.3 bps** (range 0.9–29.9) | (universe-wide p50 modelled) |

- **The live falsification corroborates the augmented NULL**, and is *harsher*
  than the backtest: the detector fires almost entirely on the widest-spread
  low-caps (`CASHCATUSDT`, `PONSUSDT`, `MARSCOINUSDT`, `牛来USDT`), where the
  spread genuinely paid at entry (median **19.3 bps**) dwarfs any directional edge.
  Per-trade net is wildly fat-tailed (+244 to −184 bps); the one tight-spread fill
  (PONS at 0.9bp) came out at −1.6 bps — essentially just the 8bp fee.
- This is the value of the live model: it pays the *real* spread on the *actual*
  names the detector flags, exposing that the tradeable universe for these
  anomalies is exactly the illiquid tail the cost floor rules out.
- Verdict unchanged: **candidate `follow_flow` is being falsified live, not
  deployed.** No live order was placed; fills are simulated against real quotes.
  Per-trade decisions and fills persist to `data/paper_trades.jsonl`; a formatted
  session snapshot is in `/opt/cursor/artifacts/live_paper_trading_results.log`.

## 13. Longer holding periods (Phase 1c) — 15m / 1h / 4h

This is not a meme-scalp question. The 8bp fee is **fixed per round trip**, so a
larger swing over minutes-to-hours could in principle clear it. Direction stayed
locked on `follow_flow` (tau=0); only three new horizons were predeclared
(LEDGER §10). Funding prints that fall inside the hold are included (5,260
historical prints). New selection cells = 9; cumulative N = 81.

| horizon | n (indep.) | fees-only mean / median / CI | measured p50 mean / CI | OOS fees-only | verdict |
|---|---|---|---|---|---|
| 15m | 2178 | +3.1 / −10.0 / [−14.3, +20.2] | −9.3 / [−26.7, +7.8] | −6.2 | no edge |
| 1h | 1272 | −22.1 / −30.1 / [−53.6, +9.2] | −33.1 / [−65.0, −1.7] | −40.6 | no edge |
| 4h | 621 | −24.0 / −58.0 / [−81.6, +37.6] | −33.6 / [−91.6, +28.2] | −74.5 | no edge |

**Holding longer does not help.** The small 15m fees-only mean fails the CI and
fails the untouched week. At 1h and 4h, `follow_flow` is significantly negative
out-of-sample — the continuation, if any, is gone within minutes and then the
position sits in residual noise plus cost. fade_flow (sanity only) flips to a
slightly positive mean at 1h/4h, which is consistent with later mean-reversion,
but those CIs include 0 and fade_flow was **not** reopened as a candidate.

The live paper-trader stays locked at 120s. Do not retune it from this test.

## 14. +60% 24h runner fade — was this tested?

**Not before this run.** Phases 1–1c used 30-second detector flags. The word
"24h" in the detector is quote **volume**, not a +60% daily price change.

The claim: names that have already printed **+60% over 24 hours** usually get a
decent reversion, so fade them (SHORT).

- **Right now on Aster:** 0 perps are ≥ +60% on the 24h ticker (largest: UAI
  +43.6%, AI +41.7%, MARSCOIN +38.7%).
- **In the 21-day history** of the 33 liquid names: 258 such crossings, but only
  6 symbols (牛来, PONS, AKE, CASHCAT, BTR, MARSCOIN). After dropping overlapping
  holds: **61 independent 1h trades, 28 at 4h**. 12h/24h samples are too small
  to score (n<20) — stopped, no substitution of a lower threshold.

| hold | n | fees-only mean / median / hit | 95% CI | verdict |
|---|---|---|---|---|
| 1h | 61 | −36.9 / **+3.7** / 53% | [−266, +168] | no edge |
| 4h | 28 | −162 / −94 / 43% | [−1045, +597] | no edge |

The intuition is half-right: the **median** 1h fade is a few basis points of
reversion — not a "decent" move, and not enough to pay the 8bp fee plus the
~19bp live spread these names actually quote. The **mean** is badly negative
because a subset of +60% runners **keep going**; shorting them is how you get
run over. Fat tails make the CI uselessly wide. This is not a candidate.

## 15. Revised thesis run (Phase 1e) — maker + liquid book + extreme OFI

Predeclared before viewing forwards (LEDGER §12). Universe frozen to 15
tight-spread names we already have bars for (BTC/ETH/SOL and other books with
p50 ≤ 5bp and top ≥ $2k). Signal = |OFI| ≥ 0.6 on any 30s bar — **no detector**.
Follow flow; stand down if 24h ≥ +40% and ofi > 0. Maker fill in the next 30s
only if ≥ $500 of opposite aggressive volume; otherwise cancel and count as 0.
Aster maker fee = 0%. Holds 60 / 120 / 300s. Gate: CI lower bound > 0 after
missed-fill, and in the untouched week.

| cell | n | mean | median | 95% CI | OOS mean | verdict |
|---|---|---|---|---|---|---|
| maker 60s | 78780 | −0.04 | 0 | [−0.08, −0.00] | +0.01 | no edge |
| maker 120s | 59860 | 0.00 | 0 | [−0.05, +0.05] | +0.02 | no edge |
| maker 300s | 36014 | +0.02 | 0 | [−0.11, +0.14] | +0.09 | no edge |

**The revised thesis does not pass.** 171k signals, 42% fill, essentially
zero mean even with **no fee**. Filled-only is still ~0. Forcing a taker exit
is −1.65bps. Getting picked off (adverse entry) is −2.5bps.

What this means: the earlier “OFI is real” result was **conditional on the
detector firing in wide-spread meme names**. On the books you would actually
want to make a market in, |OFI| ≥ 0.6 every 30s is mostly noise — it fires
thousands of times per name per week. Removing the cost floor does not reveal
a hidden edge. OFI remains a forecasting curiosity on the anomaly tail, not a
$10k maker rule on liquid Aster perps.

Live paper-trader is unchanged (old 120s taker candidate). No new deployment.

## Bottom line

On real Aster data, over 21 days and 33 liquid perps, the `aster_pulse`
detector's anomalies are **not** followed by a directional move large enough to
overcome Aster's 0.08% round-trip taker fee — under any predeclared direction,
any short *or* swing horizon (30s through 4h), any liquidity tier, in- or
out-of-sample. Conditioning on **order-flow imbalance** reveals a *real* gross
signal (monotone in OFI, Spearman up to +0.93), but it is **eaten by the cost
floor** and does not grow with hold time. The most-promising rule
(`follow_flow` @ 120s) is locked as a candidate and is being **falsified live**,
not deployed. Moving the same OFI idea onto **tight books with maker fills**
(Phase 1e) also fails: 171k signals, 0-fee mean ≈ 0 bps. The OFI pattern does
not survive once you leave the untradeable tail. **No rule passed the gate.**
