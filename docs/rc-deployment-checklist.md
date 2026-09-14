# V3.4 paper-live release-candidate checklist

## Scope and immutable ancestry

- Remediation remains 9a9dd9554e9f985b2c3a3fca0280acaea89b8254.
- Experiment remains 8bcb4dd2d721e440c00154b42a098ec1c6daa6c0.
- RC branch: rc/v34-paper-live-validation, based directly on that experiment.
- No merges, publication, VPS changes or real orders were performed.
- Original audit-findings.md, payoff-research.md and runtime-validation.md are
  preserved unchanged. This checklist records the RC additions and newer gates.

This is a locally verified release candidate, not a certification of a production
deployment. Passing fixture tests does not establish profitability or live fills.
The application runs only under Python 3.12 in its project Docker container.
Leave VPS host Python 3.10 unchanged. All commands here apply only to this project.
Do not inspect or operate any Freqtrade resource or other Docker project.

## Verification status

| Gate | Status at handoff |
|---|---|
| Local Python 3.12.14 full tests | 224 passed |
| Local Ruff | passed |
| Local strict mypy | passed, 47 source files |
| Source runtime smoke check | passed, no market network calls |
| Built-wheel runtime smoke check | passed in an isolated local package installation |
| Dependency consistency | pip check passed |
| Docker validation-stage build | still required; not run here |
| Docker runtime-image build / native CPU compatibility | still required |
| VPS clock, data freshness, healthy loops and restart recovery | still required |
| Twenty genuine primary paper trades | not collected by this task |

The handoff includes a self-contained Git bundle with the complete ancestry of all
three branch refs. Verify it before importing. Do not force-update existing branch
names. A fresh review clone is safest when those names already exist. No merge is
required to inspect or build the RC.

## RC implementation

- V3.4 service leases both resolved database paths. Another cooperating service
  cannot run the same primary or companion DB concurrently. Revision checks still
  protect SQLite writers outside this service's lease protocol.
- Startup rejects a primary snapshot changed between factory creation and lease
  acquisition. Shutdown drains in-flight blocking operations before releasing
  ownership, including repeated cancellation.
- /health returns HTTP 503 until all loops have recent successful observations;
  HTTP 200 denotes current readiness. Entry admission is suppressed when those
  loop checks fail. Exits continue attempting to manage existing exposure.
- Reject crossed/nonfinite exit quotes and quotes received after settlement.
  These are invalid-market corrections, not payoff tuning. Verified settlement
  must use the existing explicit reconciliation path; missing bids are not zero
  fills. Normal baseline golden results remain unchanged.
- SQLite store connections close explicitly. The reconciliation command checks
  database integrity, cash, entry arithmetic, closed P&L, receipt references and
  companion identity/state/event presence without SQL mutations.
- Reject a shared primary/research path and invalid explicitly supplied polling
  intervals. Packaging metadata now matches the supported Python 3.12 boundary.
- The runtime check verifies native imports, core dependency versions, ASGI
  construction, HTTP readiness, packaged HTML and fresh SQLite reconciliation.
- Docker has an optional validation stage and an explicit runtime stage. Compose
  limits resources, rotates logs and allows 120 seconds for graceful shutdown.
  CI configuration runs Python checks and isolated image checks; CI has not been
  executed or published by this task.

## Docker-build gate (operator-run later)

Run in an isolated checkout of the RC. These commands build only this repository:

    docker build --target validation --progress plain .
    docker build --target runtime -t nautilus-v34-rc:review .
    docker run --rm --network none --entrypoint python nautilus-v34-rc:review -m nautilus_delta_options.runtime_check
    docker compose -f compose.v34.yaml config

The validation target installs constrained dev dependencies and runs all tests,
Ruff, mypy and runtime checks. It must pass in Docker, not merely on the host.
The final/default image is the runtime target and runs as the application user.
Confirm the rendered Compose configuration selects:
- nautilus_delta_options.web.v34_paper_app:app, one worker;
- loopback port 8014, the dedicated V3.4 data volume and correct DB paths;
- V34_PAPER_ENTRIES_ENABLED=false;
- V34_PAYOFF_PROFILE=baseline_v34 unless paired research is explicitly intended.

Record the immutable resulting image digest, build logs and native-import result.
The base image tag and build-tool versions are not a fully hermetic supply-chain
lock; record the actual resolved build inputs. Do not promote based on a tag alone.

## VPS live-health gate (requires separate deployment authorization)

No deployment command was run in this task. Before authorizing the 20-trade run:

1. Preserve existing ledgers, companion databases and logs. Do not reset balances,
   overwrite databases or discard unresolved positions. Use SQLite's backup API
   after stopping this service and draining its workers; copying only a live main
   SQLite file can omit WAL data. Back up both DBs from the same quiescent point.
2. Reconcile prior positions and cash. Use a distinct validation account only by
   explicit decision, preserving the prior account and its liabilities. Legacy
   trades without entry observation metadata cannot be honestly reconstructed
   into the paired research cohort.
3. Start the reviewed image only after approval, with paper entries disabled.
   Verify Python 3.12, native imports on the VPS CPU, storage ownership/free space,
   UTC clock accuracy and graceful signal handling. Never run application modules
   through host Python 3.10.
4. Observe multiple completed five-minute boundaries. Confirm fresh Delta quotes,
   correct BTC/ETH symbols and market-data provenance, expected flow warm-up, loop
   success timestamps and HTTP 200 readiness. A live PID alone is insufficient.
5. Exercise controlled public-data failure and recovery in the paper validation
   environment: HTTP 503, entry suppression, unresolved-exit diagnostics, retained
   balances/receipts and successful later recovery. Do not manufacture test trades
   in the account used to count genuine market trades.
6. Restart the service and reconcile before/after states. Check single-service
   ownership, duplicate-signal receipts and continued companion observation.
   Forced termination may interrupt a transaction; SQLite durability and offline
   reconciliation remain necessary even with graceful shutdown support.
7. Authorize paper entries only after those checks pass. No private exchange
   credentials or order API is required or implemented.

From this project's container, read-only reconciliation is:

    python -m nautilus_delta_options.paper.reconcile --database /data/v34-paper.sqlite --research-database /data/v34-paper.sqlite.payoff.sqlite

Run that command against stopped/quiescent writers or consistent backup copies.
Its cross-database reads are separate snapshots, not a distributed transaction.
An OK result establishes accounting consistency only, not correct settlement
sources, executable liquidity, complete event history or profitable signals.

## Research and twenty-trade acceptance

The primary account remains baseline_v34. Opt-in astra_payoff_experimental adds
paired exit research on the same admitted entries and quote stream; it does not
change primary entries/sizing or simulate independent experimental admissions.
The approved 1R hypothesis and all directional/selection rules are unchanged.

Count genuine primary closed trades once. Do not count companion copies or unit
fixtures. Record cash/P&L reconciliation, missing quotes, spreads/depth, fees,
gap beyond stop, observed MFE/MAE, activation, holding time, IV/Greeks/DTE and all
unresolved outcomes. Keep research profile and penalty immutable within each
cohort database. Report paired differences separately from primary-account P&L.

Before calling the 20-trade operational validation complete:
- reconcile every trade and receipt, including any verified settlement;
- retain failures/deferred exits and identify observation gaps or downtime;
- explain fee/slippage assumptions and any insufficient-depth events;
- confirm no duplicates or restart-induced account changes;
- record live-health and image evidence alongside the primary and companion DBs.

Remaining model limits: displayed full bid depth may vanish, polling misses
intraperiod extremes, fee/slippage estimates are not exchange billing, and
settlement requires an external verified source. A stop does not guarantee its
price or limit loss below the premium debit. Twenty trades do not prove expectancy.
