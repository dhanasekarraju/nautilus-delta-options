# RC2 adversarial hardening and deployment supplement

## Scope

Paper-only correctness release candidate. No deployment or profitability certification.
Application: Python 3.12; leave VPS host Python 3.10 unchanged. No signal scores, indicators,
confirmation thresholds, ranking, sizing, risk percentages, baseline payoff rules or 1R hypothesis tuning.

Protected tips remain:
- fix/astra-v34-audit-remediation: 9a9dd9554e9f985b2c3a3fca0280acaea89b8254
- exp/astra-v34-payoff-research: 8bcb4dd2d721e440c00154b42a098ec1c6daa6c0

RC2 is one new commit on rc/v34-paper-live-validation directly after
4925407805c9f5342e08ec4ea03d2085e5d24ea1. RC1 remains unchanged in history.
Original handoff documents and RC1 checklist are preserved as historical records.

## Confirmed findings and trade-outcome impact

| Finding | File/function | Implemented correction | Valid trade outcome impact |
|---|---|---|---|
| P1: readiness race during exact refresh | web/v34_paper_app.py _poll_v34_signals/_required_readiness; paper/v34_paper_live.py _try_open; paper/session.py open_long_for_signal; paper/persistence.py save_with_signal | Reserve SQLite writer first; check current readiness and admission freshness/risk under session lock; hold shared health lock through receipt/snapshot commit. Health mutations and shutdown use that lock. | Rejects unhealthy/stale admissions RC1 could accept; healthy financial formulas unchanged. |
| P1: health omitted signal warnings | web/v34_paper_app.py _health_errors | Include signal warnings, stopping state, malformed/future wall timestamps and monotonic loop age. | Invalid/unhealthy events rejected; no signal changes. |
| P1: DNS/read stalls could indefinitely delay worker drain | public_http.py public_json; DeltaPublicClient, DeltaHistoryClient, BinanceFuturesPublicClient | Killable stdlib public GET subprocess, monotonic deadline, shutdown event, 8 MiB response cap, no redirects or HTTP retries. Approved HTTPS paths only. | Timely response parsing unchanged. Process startup/transport latency can change the received quote or freshness admission; quantify on VPS. |
| P1: penalty could create zero-price research fill | paper/payoff_profiles.py initial_state/observe | Defer nonpositive bid-minus-penalty without updating excursions, best value, stop or closing. | Only non-executable penalized observations change; default positive bids unchanged. |
| P1: absent persistent build/config/runtime identity | paper/provenance.py make_provenance/bind_provenance; app lifespan | Bind identity and per-run journal in both DBs under leases before loops; persist entry provenance atomically with receipts. Reject incompatible/unknown historical cohorts. | Metadata only for compatible runs; incompatible starts rejected. No balance reset or historical relabelling. |
| P2: hardlink alias bypass | web/v34_paper_app.py create_v34_paper_app | samefile check in addition to resolved paths. | Reject invalid primary/research alias; symlink and relative aliases also tested. |
| P2: post-settlement event could pass small clock skew | paper/ledger.py _validate_exit_ticker; paper/payoff_profiles.py observe | Reject when receipt OR exchange timestamp is at/after settlement. | Only invalid post-settlement observations change; no invented zero/intrinsic exit. |

The deterministic race test begins healthy, fails each required loop on the second exact quote
refresh, and proves unchanged cash, positions, closed trades, receipts and companion cohorts/events.
Candidate construction is ephemeral. Admission executes after SQLite lock acquisition, so lock wait
cannot bypass the existing signal/quote freshness check. Memory publishes only after persistence.

## Provenance contract

Health, dashboard API and visible dashboard expose supplied NAUTILUS_BUILD_ID (or packaged
Python/HTML SHA256), source digest, random run UUID, start time, effective config and its SHA256,
combined identity fingerprint, Python implementation/version and core runtime dependency versions.
Config includes profile, penalty, risk/sizing/exit/signal/quality defaults and polling intervals.
Entry-enable state is recorded per run but excluded from immutable fingerprint so operational
paper entry toggling remains possible.

validation_identity stores cohort specification; validation_runs retains compatible process runs.
Entries carry provenance in the same snapshot/receipt transaction; companion entry events inherit it.
The package digest works in Docker/wheels without .git. Retain actual image digest and complete
resolved dependency inventory externally; this is not whole-image cryptographic attestation.

Build/source/runtime/dependency/config changes require a fresh validation database pair. Never
reset an old account to bypass this check. Nonempty pre-provenance databases fail closed; preserve
and reconcile prior positions/receipts separately, including unresolved liabilities.

Both identities are checked before either is bound. Each DB binding is atomic, not a distributed
transaction. Interrupted startup may leave a run record in one DB, before any trading loop starts;
compatible restart is safe. Cross-database reconciliation remains necessary after crashes or manual
file replacement. No claim of arbitrary filesystem corruption or malicious-writer resistance.

## Tests and evidence

New test modules: tests/unit/test_rc2_hardening.py and tests/unit/test_rc2_public_http.py.
Coverage includes readiness loss during exact refresh for all three required loops; real SQLite
writer contention; injected full/I/O failure after receipt insertion and commit failure; rollback
and retry/replay on restart; cancellation during blocked refresh; exit commit failure and gap-close
recovery; relative/symlink/hardlink aliases; future wall timestamps and monotonic health age;
provenance compatibility, legacy rejection and atomic entry metadata; nonpositive penalized exits;
post-settlement clock skew; repeated public failures and readiness recovery; hung request kill,
shutdown cancellation, malformed JSON recovery, timeout validation and public endpoint restrictions.

Seeded tests exercise 100 randomized experimental price/depth paths: immutable initial R,
fee-adjusted >=1R activation, nondecreasing stop, unchanged thin-depth state and executable gap
price minus penalty. Thirty randomized baseline close/restart cycles verify cash conservation,
fee arithmetic and unique IDs. These are deterministic fault injections/invariant samples, not
physical disk-exhaustion/power-loss tests or exhaustive property proofs.

Existing tests retain coverage for stale competing writers, future/stale/out-of-order quotes,
nonfinite market values, thin bids, IV/theta paths, research journal failure, companion public-data
failure while primary stops continue, repeated cancellation, duplicate signals and financial goldens.
Baseline golden fixtures and signal/ranking/sizing configurations are unchanged.

## RC2-only files (relative to 4925407)

- Dockerfile — immutable build ID argument/environment.
- compose.v34.yaml — build ID forwarding.
- src/nautilus_delta_options/delta/history.py — bounded/cancellable transport.
- src/nautilus_delta_options/delta/public_client.py — bounded/cancellable transport.
- src/nautilus_delta_options/signals/binance.py — transport only; calculations unchanged.
- src/nautilus_delta_options/public_http.py — public-only deadline transport.
- src/nautilus_delta_options/paper/ledger.py — provenance and settlement rejection.
- src/nautilus_delta_options/paper/payoff_profiles.py — non-executable/settlement rejection.
- src/nautilus_delta_options/paper/persistence.py — final transactional admission.
- src/nautilus_delta_options/paper/session.py — guard/provenance forwarding.
- src/nautilus_delta_options/paper/v34_paper_live.py — guard forwarding.
- src/nautilus_delta_options/paper/provenance.py — persistent cohort/run identity.
- src/nautilus_delta_options/runtime_check.py — offline provenance gate.
- src/nautilus_delta_options/web/v34_paper_app.py — health/shutdown/alias/provenance wiring.
- src/nautilus_delta_options/web/v34_paper_dashboard.html — visible identity.
- tests/unit/test_rc2_hardening.py — accounting/lifecycle/payoff regressions.
- tests/unit/test_rc2_public_http.py — transport regressions.
- docs/rc2-hardening-report.md — this report and deployment supplement.

## Remaining deployment gates

Docker validation/runtime builds and VPS live-health verification remain REQUIRED and UNVERIFIED.
No deployment, merge, private credentials, real-order capability or Freqtrade interaction occurred.
Use the original RC checklist plus these additions, only after deployment authorization:

1. Review delivered RC2 SHA in isolated project checkout. Set NAUTILUS_BUILD_ID to that SHA
   before this project's Compose build. Direct builds use --build-arg NAUTILUS_BUILD_ID=<SHA>.
2. Build Docker validation/runtime targets; run offline runtime check inside built image without
   networking. Retain build logs, image digest, dependency inventory and native CPU check. Verify
   request subprocess creation under actual container process/memory/CPU limits.
3. Preserve/reconcile both old databases. Choose a fresh prospective pair explicitly if prior
   trades lack provenance; never erase unresolved exposure.
4. With entries disabled, verify health/dashboard identity matches image/config and both DB
   fingerprints. Restart must create a new run ID while preserving cohort identity and accounting.
5. Observe multiple genuine five-minute boundaries. Measure transport startup overhead, latency,
   deadline rate, stale/deferred quotes, exit cadence and shutdown during stalled public requests.
6. Verify HTTP 503 during failure, recovery, no unhealthy admission, restart receipts and complete
   primary/research reconciliation. Public requests have deadlines, not aggressive retry loops.
7. Only then enable 20-trade prospective PAPER validation. Primary remains baseline_v34;
   experimental is opt-in paired exit research, not an independently sized portfolio. Count genuine
   primary trades once; retain all deferred/unresolved outcomes and observation gaps.

Measure fees, slippage penalties, gap beyond stop, MFE/MAE, activation, DTE/Greeks/IV, latency,
downtime and accounting discrepancies. Stops depend on executable depth; displayed bids can vanish,
polling misses intraperiod paths, and settlement requires verified external evidence. Twenty trades
cannot prove expectancy. No historical threshold tuning or extra strategy features were implemented.

## Completed local verification

- Python 3.12.14 full suite: 271 passed (47 additional cases over RC1's 224).
- Ruff: all checks passed.
- Strict mypy: no issues in 49 source files.
- Source runtime smoke check: passed, zero network calls.
- Newly built and separately installed wheel runtime smoke check: passed; import path verified.
- Source and installed-wheel source SHA256 both:
  ac5cfb7e0f88b8cfb2a1f84b0ddc9747471b45e4dafc39689497a7d141a21750.
- pip check: no broken requirements.
- git diff --check: passed.

The initial local wheel attempt lacked the hatchling build backend. Installing that backend
in the isolated build environment resolved it; the subsequent build and wheel checks passed.
No application dependency constraints were changed. Docker/VPS checks remain unverified.
Bundle verification and fresh-clone fsck are reported with the final handoff after commit creation.
