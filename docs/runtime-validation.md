# Supported runtime and deployment boundary

The supported application runtime is Python 3.12 inside this project's Docker
container. The VPS host Python 3.10 is unrelated and must remain unchanged.
Do not install/backport the dependency stack into the host Python or execute
application modules there. PEP 695 aliases and StrEnum are valid in the container.

From this repository only, validate a newly built image before starting it:

    docker compose -f compose.v34.yaml build dashboard
    docker compose -f compose.v34.yaml run --rm --no-deps --entrypoint python dashboard -m nautilus_delta_options.runtime_check

Development verification must also run under Python 3.12 with the project's dev
dependencies: pytest, ruff check src tests, and mypy src.
A local Python check is not proof that the deployment image builds or is healthy.

compose.v34.yaml explicitly selects the V3.4 paper dashboard, isolated database,
loopback port 8014, single worker, and real loop-health checks. The original
compose.yaml selects the older dashboard. Never run both against the same database.
Entries are disabled by default; enabling them enables simulated paper entries only.
No order credentials or order transport are needed. Do not use host-wide Docker
commands, or inspect or operate any other project on the VPS.

After a restart, health remains unready until the signal and both exit loops
have succeeded. Market-data failure must not be counted as a healthy observation.
Stale SQLite writers fail closed and need a deliberate reload/restart; retries
must not overwrite the newer ledger.

Missing or non-executable quotes are unresolved exposure, not zero-price fills.
Settlement requires a verified official settlement price, actual fee, and source
reference; the manual ledger settlement method must never receive an invented price.
Historical positions without settlement metadata need reconciliation first.

constraints-py312.txt records the Python 3.12 dependency versions used for local
verification. Docker installs application dependencies under these constraints
and runs the offline import check during its build. Dev tools use the same
constraints: python -m pip install -c constraints-py312.txt '.[dev]'.
This is a version constraints file, not a hash-locked supply-chain manifest.

Validation on the remediation branch: full suite, Ruff, strict mypy and offline
runtime imports. No VPS deployment or Docker build was performed in the editing
environment (Docker is unavailable). A successful image build and healthy live
public-data observation remain deployment gates before collecting genuine trades.
Fixtures are not genuine paper trades.

Remaining execution limitations: full displayed depth is assumed available at
observation; no queue, partial fills, latency model, or automatic official
settlement feed. Missing depth defers an exit and does not guarantee a stop.
No statistical edge has been established. Current signal rules, risk percentages,
initial stop/target, and contract-ranking weights were not tuned.
