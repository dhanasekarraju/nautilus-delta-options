# Experiment 1: isolated collector release checklist

Status: implementation candidate, NOT authorization to deploy or collect.
Base: df894a7259fc592a818504b0c8dd059dd2ff7f14.
Frozen production: fa0396a242543c8687f46d1b976d84d2bafffda0.

## Invariants

- Python 3.12 in the application container. Do not install dependencies on host Python 3.10.
- Preserve baseline_v34 and its completed 20-trade result.
- Never open the primary/payoff databases or mount /data into this service.
- Never invoke the paper application/session/admission path.
- No exchange credentials. The transport permits only HTTPS GET to the India public history and ticker endpoints and refuses redirects.
- Use only compose.experiment1.yaml, project nautilus-experiment1. Never use compose up/down against the existing deployment. Do not inspect or interact with Freqtrade.
- Existing journal databases are never migrated or repaired. This collector uses schema version 3; use a new empty volume/path for a new experiment.

## Protocol decisions to seal before collection

The original September retrospective remains blocked. Do not use September 29 or any elapsed window.
Seal a future UTC window [start,end), indexed by completed candle CLOSE boundaries.
A slot at start consumes the candle [start-5m,start). The last slot closes at end-5m.
P0 is the exact candle version consumed by the first actual successful evaluator return,
including WAIT. Conflicting retries are quarantined; missing first-return evidence is never
replaced with a later signal. Signal eligibility is limited to the original five-minute
slot; late evaluations remain evidence but produce invalid endpoints.

P30 uses the first archived, valid completed Delta candle with close exactly P0.close+1800s.
No nearest candle, interpolation, revised P0 or zero return for missing observations.
Continue history collection after the signal window through an explicitly sealed acquisition
deadline >= end+30m. Endpoint bindings finalize only at/after that deadline, when retries and
conflicts are known. No option requests or new signal evaluations during endpoint tail.
All raw revisions remain archived; existing endpoint bindings never change.

Keep the preregistered directional endpoint (signed log return in basis points) and analysis
rules in protocol.md, including the intended 56-day window, minimum sample/asset coverage,
95% completeness gates, UTC day clustering and sealed holdout. This collector records evidence;
it does not optimize thresholds or calculate profitability. Validate these rules independently
before approving the manifest; this code does not constitute statistical sign-off.

### Persistence timing and restart policy

Durable evaluation intent uses a bounded synchronous SQLite transaction after draining the
background journal (2-second acknowledgement; SQLite busy timeout 250ms). Record intent latency.
This deliberately adds latency to the isolated collector; it is NOT an exact timing clone of
production. The standalone process consumes its own public observations and does not tap the
running dashboard. No claim of zero API-quota or shared-host impact is made.

Preserve previous-chain state ONLY from a fully committed BTC/ETH cycle. Partial cycles never
advance rolling state. Clean restarts restore that state; this continuity policy differs from
a production cold start and is part of the sealed experiment.
A fsynced .inflight marker covers each cycle until evidence is durable. Any ambiguous interruption
blocks restart. Do not delete the marker to force continuation; preserve the failed experiment
and resolve its evidence status. Journal rejection stops research after the current computation;
it cannot change any production trading readiness or decision.

## Offline gates

- Full Python 3.12 tests, Ruff, strict mypy, source runtime smoke check.
- Build wheel and verify installed schema/CLI and runtime independently of source checkout.
- Diff production directories against frozen base: must be empty.
- Independent review of changes and fault tests; no profitability tuning.
- Verify schema incompatibility refuses mutation, raw parse/HTTP failures persist bytes,
  timeout partials are marked incomplete, in-flight batches remain in the memory budget,
  exact endpoints do not substitute adjacent/revised/future data, and restart is fail-closed.

## Docker and VPS gates (still required)

Use an isolated checkout and immutable EXPERIMENT1_BUILD_ID. Never rebuild the dashboard tag.
Review rendered compose configuration: dedicated volume only, no credentials, /data, host root,
Docker socket or privileged mode; no ports are published. Build validation target and runtime
image. The default command is --help and cannot collect.

Example OFFLINE build gates (operator execution only after review):

```sh
EXPERIMENT1_BUILD_ID="<reviewed-commit>" docker build \
  -f Dockerfile.experiment1 --target validation \
  --build-arg EXPERIMENT1_BUILD_ID="<reviewed-commit>" \
  -t nautilus-experiment1-validation:<reviewed-commit> .
```

On the VPS verify CPU/RAM/disk isolation, network deadlines, SIGTERM completion,
raw/body byte counts, write latency, journal growth and that production health stays unchanged.
No live soak is authorized by this document. A separately authorized disposable soak must use
its own experiment identity/window and must not be included in the sealed validation sample.
Rate is the frozen default: a cycle followed by a 5-second wait, no aggressive retries.
Set a realistic capacity budget after observing growth: current safety limits halt at 8 GiB DB
or less than 512 MiB free disk; never delete or rotate evidence to continue a cohort.

## Explicit command flow inside the reviewed Python 3.12 image

All paths below refer exclusively to /research-data. Supply a reviewed protocol.md and a
future untouched start/end/deadline. Do not execute application modules on host Python 3.10.

1. `python -m nautilus_delta_options.research.experiment1 seal --experiment-id <id> --build-id <sha> --start <UTC> --end <UTC> --deadline <UTC> --protocol /research-data/protocol.md --output /research-data/manifest.json`
2. Independently review the produced manifest and record the printed SHA-256 externally.
3. `init --database /research-data/observations.sqlite --manifest /research-data/manifest.json --manifest-sha256 <sealed-file-sha256> --protocol /research-data/protocol.md --build-id <sha>` (same module prefix). Must occur before window start.
4. `verify` with the same arguments: no market requests.
5. Only after explicit collection authorization: `run` with the same arguments plus `--authorize-public-collection`.

Manifest pins engine and collector source hashes, build ID, exact Python/core-dependency/SQLite
identity, config, protocol, schedule and rules. A runtime/config change requires a new experiment.
All raw bodies are losslessly zlib-compressed and content-addressed; request occurrences retain
separate provenance. Successful evaluator envelopes retain full ordered input candles and the
actual filtered chain/previous state. Normalized endpoint candidates do not replace raw evidence.

Monitor journal faults and process exits. A healthy legacy dashboard does not prove this collector
is running. No automatic restart policy is configured. After acquisition deadline, verify endpoint
counts, gaps, conflicts, late events, all provenance and SQLite integrity before analysis.
