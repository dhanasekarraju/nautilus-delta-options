# V3.4 audit record

Scope: repository base dbbe3e47d421c16ef514b499405229f0d034f8b7.
Software remediation: 9a9dd9554e9f985b2c3a3fca0280acaea89b8254.
This document distinguishes base findings from implemented corrections. No VPS
inspection, deployment, real order placement or profitability validation occurred.

## 1. Architecture assessment

The architecture separates public market-data clients, pure signal/selection/
payoff functions, a paper ledger, SQLite session persistence and FastAPI polling.
Retain that separation. The older web.app path uses Binance-derived signals and
V3.4 shadow observation. The new web.v34_paper_app uses DeltaHistoryClient
BTCUSD/ETHUSD five-minute candles for V3.4 direction. They are different runtime
paths; the original Compose file did not select the new V3.4 paper service.

V34ShadowObserver aligns BTC/ETH completed-candle boundaries, retains a preceding
option-chain observation for flow confirmation, then run_v34_paper_entry_cycle
selects exact contracts and performs two quote/quality/payoff checks. A shared
v34:<candle>:<direction> receipt and directional exposure guard arbitrate BTC/ETH.
The paper session is the durable entry owner; there is no need for real orders.

## 2. Ranked confirmed defects and conditional risks

Paths below are under src/nautilus_delta_options unless stated otherwise.

| Severity | Exact location and base evidence | Failure and practical impact | Status / smallest correction |
|---|---|---|---|
| P0 | No confirmed order-routing or capital-transfer defect identified in the reviewed paper path. | Absence of a found defect is not a security certification. | Preserve paper-only authority. |
| P1 confirmed | paper/session.py, open_long/process_exit/process_exit_ticker/close_long: base mutated self._ledger before store.save; signal entry already used a candidate. | Disk failure could leave in-memory cash/positions different from restart state, invalidating reported results. | All session mutations now commit candidates before publishing memory; injected disk-failure regression. |
| P1 confirmed | paper/persistence.py, SQLitePaperLedgerStore.save: snapshot UPSERT had no revision predicate; session RLock is process-local. | A second writer could erase another writer's entry/exit and cash changes. | BEGIN IMMEDIATE plus expected-revision rejection; competing-writer regression; one deployment worker. |
| P1 confirmed | paper/ledger.py, _validate_exit_ticker: base checked against opened_ns but not receipt-clock freshness/future skew. | Old bids or future timestamps could close positions at unavailable prices and inflate P&L. | Shared 15-second freshness / 5-second skew checks; explicit test clocks, out-of-order rejection. |
| P1 confirmed | paper/v34_paper_live.py, _try_open: portfolio and direction checks preceded session mutation under a different lock boundary. | Concurrent entry could pass stale portfolio/correlation checks. | Final admission callback validates current state and clock while session lock is held. |
| P1 confirmed measurement defect | paper/portfolio_risk.py, evaluate_portfolio_entry: account_equity = initial_cash + realized_pnl. | It excluded open losses/spread/exit fees, overstating usable equity and risk capacity. | Executable liquidation marks retained; V3.4 final admission caps limits using liquidation value and rejects incomplete valuation. Dashboard exposes completeness and unrealized P&L. |
| P2 confirmed | web/v34_paper_app.py, _poll_v34_signals / health: failed observer cycles could be returned as warnings while error was cleared; Docker health checked PID and writable storage only. | A dashboard could appear alive while not producing valid observations or exits. | Signal-success timestamp, loop warnings and age checks; explicit V3.4 Compose health examines /health status. |
| P2 confirmed | compose.yaml service command is nautilus_delta_options.web.app:app. | Launching the existing Compose file does not validate the new V3.4 paper entry path. | Separate compose.v34.yaml; existing service selection not overwritten. |
| P2 confirmed | delta/history.py, parse_history_candles_payload: sorting/deduplication alone accepted gaps and unaligned times. Decimal parsing accepted nonfinite values. | Invalid time series/numeric inputs could contaminate indicators or fail unpredictably. | Reject gaps, misalignment and nonfinite market numbers. |
| P2 conditional | delta/public_client.py, fetch_option_products originally made one products request. | A paginated response could omit contracts. Actual truncation was not established; one live audit response had no next cursor. | Follow validated pagination cursors with repeat/bounds protection. |
| P2 confirmed display defect | paper/v34_shadow.py, v34_shadow_signal_payload mapped score_edge from confidence. signals/v34.py makes confidence=max(call_score,put_score) for active signals. | Active-signal dashboard edge did not mean CALL-minus-PUT separation. | Display abs(call_score-put_score); directional rules untouched. |
| P2 lifecycle weakness | paper/observer.py, run_cycle requires a complete tradeable market record before time exits. | Missing Greeks/full records could delay an otherwise executable time exit. | V3.4 fast ticker exits apply the existing hold/settlement limits from persisted contract metadata. |
| P2 unresolved execution assumption | paper/ledger.py, _close_long_with_ticker executes the entire size at best_bid if displayed bid_size suffices. | The displayed book can disappear before execution; there is no partial fill/queue/latency simulation. | Preserved baseline; optional symmetric price-penalty research and deferred-depth instrumentation. |
| P2 unresolved settlement operation | paper/ledger.py, settle_long requires caller-provided verified settlement spot, fee and reference. | A missing/expired contract is not automatically reconciled; no-bid exposure can remain open. | Never manufacture a zero fill. Verify settlement manually before accepting validation P&L. |
| P3 statistical | No sufficiently sized, representative live paper sample is supplied by these code changes. | Clean code cannot establish expectancy or profitable thresholds. | Instrument and collect prospective observations. |

## 3. Trading logic and signal quality

signals/v34.py, evaluate_v34_shadow_signal requires an ADX regime gate, same-sign
15m/30m momentum, no overextension, minimum total score, underlying directional
score, score edge, and flow confirmations. For example:
call_score - put_score >= cfg.min_score_edge and
flow.call_confirmations >= cfg.min_flow_confirmations.
PUT uses the symmetric directional comparisons. A missing flow state waits.

evaluate_v34_chain_flow compares common eligible contracts from prior/current
snapshots. These are interval observations, not transaction-level signed order
flow. Quote freshness and universe turnover can alter what is compared. That is
a signal-quality hypothesis to measure, not evidence that CALL/PUT signs are wrong.
No indicator weight, score, confirmation or flow threshold was changed.

selection/v34_quality.py, rank_v34_contract_quality combines liquidity and relative
Greek/IV scores. Relative ranking does not establish fair option value or positive
expected return. Validate vendor Greek units and IV convention before using them
economically. No claim of profitable ranking follows from its code structure.

## 4. Paper/live-market divergence

The model uses ask entries, bid exits and full displayed size. Polling misses the
intraperiod path. There is no order acknowledgement latency, queue, partial fill or
measured book depletion. Fee caps/GST are estimates from the configured exchange
schedule, not reconciled billing. Contract metadata, quotes and Greeks are fetched
at different moments. The new chain freshness filter rejects stale ticker
timestamps but cannot prove each embedded Greek was independently refreshed.

The experimental companion is a matched admitted-entry cohort. It is not a
separately capitalized portfolio and cannot determine how altered holding times
would change future risk/cash/correlation admissions.

## 5. Look-ahead and fills

No explicit future-bar signal access was found in the completed-candle path.
The parser excludes incomplete candles. Do not equate that with a proof that all
external timestamps or replay datasets are correct. The confirmed old exit-clock
defect admitted stale/future quotes; remediation addresses that boundary.

Stops and targets are triggers, not guaranteed prices. Gap tests verify current
bid execution rather than the threshold. Stale observations cannot mutate
experimental MFE/MAE or exits. MFE/MAE are sampled executable, fee-adjusted values,
not true tick-by-tick extrema. Downtime paths are not reconstructed.

## 6. Risk management

Initial premium debit limits and planned stop-loss limits are different controls.
The full debit can be lost if a stop is unexecutable. Existing sizing fractions,
premium exposure caps, position counts and initial payoff gate are retained.
The guard blocks same-direction BTC/ETH overlap; it is not a measured portfolio
correlation or net-Greek risk model. Opposite option directions can still share
long-vega/theta exposure.

The experiment preserves initial risk R and adds only the approved protection
hypothesis. It does not increase size or treat unrealized gains as guaranteed cash.
Fresh full-size executable marks are needed for V3.4 final portfolio admission.

## 7. Runtime and deployment

Python 3.12 in the application Docker container is the supported boundary.
PEP 695 aliases, StrEnum, UTC and Self do not need backports for that runtime.
Do not execute application modules using the VPS host 3.10 or alter that host.

The base also had one Ruff import issue and two mypy annotation issues in the new
V3.4 service files; these were corrected. Constraints record tested dependency
versions and Docker runs an offline runtime import check at build time.
Actual Docker image build, healthy live loops and VPS deployment remain unverified
here. See runtime-validation.md for isolated project-specific commands.

## 8. Tests

Base: 161 passing tests; new V3.4 service orchestration lacked direct coverage.
Remediation: 174 passing tests, adding fault injection, stale/future/out-of-order
quotes, concurrent writers, executable equity, settlement, malformed candles,
time exits without Greeks and dashboard health/runtime configuration.
Experiment: 199 passing tests, including adverse paths and a frozen-remediation
financial golden file, baseline-vs-native comparisons, restart persistence,
journal rollback and default/opt-in wiring.

Remaining evidence gaps: image build; live API end-to-end soak; process termination
during durable writes; realistic depth/latency replay; official settlement
reconciliation; full V3.4 arbitration under repeated API faults; and a prospective
statistical sample. These are not represented by the passing fixture count.

## 9. Twenty-trade readiness

The original dbbe3e4 base had material software issues and was not ready to produce
trustworthy results. The local branches correct the confirmed defects described
above and pass their stated offline checks. They are not deployed or certified
healthy under live conditions. Require image build and healthy public-data/exit
loops first; count only genuine primary paper trades and reconcile unresolved
positions. Twenty trades validate operation, not profitability.

## 10. Minimal continuation plan

Import/review the two local commits without merging protected branches. Build the
isolated V3.4 image under Python 3.12, verify health and clock/quote ages with entries
disabled, then explicitly enable paper entries in the intended environment.
Use a fresh validation ledger and immutable companion profile configuration.
Collect and reconcile 20 genuine primary trades, retain all rejected/deferred
observations, and report paired exit results separately. No real execution is part
of this plan. Do not optimize the 1R hypothesis on that sample.
