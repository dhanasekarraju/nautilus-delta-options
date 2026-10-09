# Experiment 1 collector completion handoff

Branch: fix/experiment1-collector-completion
Exact parent: df894a7259fc592a818504b0c8dd059dd2ff7f14
Frozen production base: fa0396a242543c8687f46d1b976d84d2bafffda0

## Implemented and locally verified

- Raw capture occurs before JSON/frozen parsing; captured partial/error bodies retain completeness
  and transport status, and are independently journaled. Requests and successful evaluations link
  to immutable capture IDs. zlib bodies retain SHA-256 of original exact bytes.
- Background writer capacity includes active batches until commit/discard. A bounded flush
  supplies explicit durability acknowledgement. New database creation reserves the path exclusively.
- Public collector CLI has separate seal/init/verify/run commands; no live action without explicit
  authorization. No paper app/session/admission is instantiated. Old schema is refused, not migrated.
- Manifest/schedule/source/config/runtime checks, collector ownership and crash marker protection.
- Close-indexed UTC schedule and fixed 30-minute endpoint bindings. No nearest-candle fill, revised
  P0 substitution or late/inconsistent signal promotion; conflicting returns are quarantined.
- History-only endpoint tail after the signal window, immutable first observations and explicit gaps.
- Dedicated Dockerfile and Compose project/volume, nonroot/read-only runtime, no ports, credentials,
  Docker socket or /data mount. Default command is help; restart is disabled.

## Deliberate semantics

The isolated collector has its own observations. Durable intent adds bounded waiting; it is not a
production timing clone. Prior-chain restoration after clean restart is an explicitly different
policy from the original observer cold start. Ambiguous cycles stop research and block restart.
Instrumentation faults never affect the existing production process.

New research resource limits and lateness rules affect evidence eligibility/availability; no
signal-generation thresholds, contract ranking, sizing, payoff or production health logic changed.
An untouched future window must be sealed after independent review and runtime verification.

## Validation

Python 3.12.14, dependencies installed with constraints-py312.txt:

- Full suite: 423 passed.
- Experiment 1 tests: 152 (130 existing, 22 new cases).
- Ruff src/tests: passed.
- Strict mypy src: passed, 69 source files.
- Source runtime smoke check: passed, no exchange calls.
- pip dependency compatibility: passed.
- Wheel built and installed into a separate Python 3.12 environment.
- Installed-wheel runtime/CLI and schema/manifest/schedule initialization: passed, no exchange calls.
- Frozen paper/delta/signals/selection/web diff against production base: empty.

New tests cover malformed JSON/UTF-8/parser errors through actual capture, HTTP error body retention,
partial timeout shutdown, active-batch memory accounting, exact/revised/adjacent/wrong-asset/late
endpoints, conflicting signals, late evaluation, manifest mismatch, path aliases, dirty restart,
clean offline shutdown, full evaluation-to-tail finalization, schema tampering and authorization gate.
Existing differential observer tests and baseline financial golden tests remain in the full suite.

## Files changed

- Dockerfile.experiment1
- compose.experiment1.yaml
- docs/experiment1-collection-checklist.md
- docs/experiment1-completion-handoff.md
- src/nautilus_delta_options/research/experiment1/__main__.py
- src/nautilus_delta_options/research/experiment1/archive.py
- src/nautilus_delta_options/research/experiment1/capture.py
- src/nautilus_delta_options/research/experiment1/collector.py
- src/nautilus_delta_options/research/experiment1/endpoints.py
- src/nautilus_delta_options/research/experiment1/journal.py
- src/nautilus_delta_options/research/experiment1/schema.sql
- src/nautilus_delta_options/research/experiment1/schema_identity.py
- src/nautilus_delta_options/research/experiment1/service.py
- tests/research/experiment1/test_completion.py
- tests/research/experiment1/test_population.py

## Remaining gates

Docker is unavailable in this implementation environment: image build, Compose mount/resource
verification, live public transport soak, VPS health/shutdown/recovery and storage sizing remain
unverified. Independent review is still required. No live market collection or deployment has
been performed. Do not treat unit tests as profitability evidence or collection authorization.

Use experiment1-collection-checklist.md for the exact deployment/sealing sequence. Preserve all
existing branches, production containers, primary/payoff databases and the failed baseline cohort.
