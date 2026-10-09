"""Independent, lossless response archive; no evaluator or paper authority."""

from __future__ import annotations

import time
import uuid
import zlib
from collections.abc import Callable
from threading import Event

from nautilus_delta_options.delta.history import DeltaCandleSnapshot
from nautilus_delta_options.delta.public_client import DeltaOptionChainSnapshot, DeltaUnderlying
from nautilus_delta_options.research.experiment1.capture import (
    CapturedHistorySnapshot,
    RawPublicResponse,
    fetch_captured_5m_candles,
    fetch_captured_option_chain,
)
from nautilus_delta_options.research.experiment1.journal import ExperimentJournal, JournalStatement
from nautilus_delta_options.research.experiment1.manifest import canonical_json, sha256_text
from nautilus_delta_options.research.experiment1.market_data import Experiment1CapturedMarketData
from nautilus_delta_options.research.experiment1.service import (
    _raw_candle_records,
    _source_response_statement,
)


def event_statement(
    experiment_id: str,
    run_id: str,
    kind: str,
    payload: dict[str, object],
    *,
    now_ns: int | None = None,
) -> JournalStatement:
    body = canonical_json(payload)
    return (
        "INSERT INTO journal_events (event_id,experiment_id,run_id,event_type,"
        "event_time_utc_ns,payload_json,payload_sha256) VALUES (?,?,?,?,?,?,?)",
        (
            uuid.uuid4().hex,
            experiment_id,
            run_id,
            kind,
            time.time_ns() if now_ns is None else now_ns,
            body,
            sha256_text(body),
        ),
    )


class EvidenceArchive:
    def __init__(
        self,
        journal: ExperimentJournal,
        experiment_id: str,
        run_id: str,
        endpoint_boundaries: set[int],
        deadline_ns: int,
    ) -> None:
        self.journal = journal
        self.experiment_id = experiment_id
        self.run_id = run_id
        self.endpoint_boundaries = endpoint_boundaries
        self.deadline_ns = deadline_ns
        self.fault: str | None = None
        self._seen: set[tuple[str, int]] = set()

    def submit(self, statements: tuple[JournalStatement, ...]) -> None:
        try:
            if not self.journal.enqueue_batch(statements):
                self.fault = "archive queue rejected evidence"
        except Exception as exc:
            self.fault = f"archive failure: {exc}"

    def record(self, kind: str, payload: dict[str, object]) -> None:
        self.submit((event_statement(self.experiment_id, self.run_id, kind, payload),))

    def raw(self, asset: str, kind: str, response: RawPublicResponse) -> str:
        response_id = response.capture_id
        metadata = {
            "response_id": response_id,
            "asset": asset,
            "response_kind": kind,
            "url": response.url,
            "http_status": response.http_status,
            "body_complete": response.body_complete,
            "transport_error": response.transport_error,
            "clock_anomaly": response.clock_anomaly,
            "request_started_utc_ns": response.request_started_utc_ns,
            "response_received_utc_ns": response.response_received_utc_ns,
            "request_elapsed_ns": response.request_elapsed_ns,
        }
        self.submit(
            (
                (
                    "INSERT INTO raw_payloads (sha256,codec,body) VALUES (?,'zlib',?) "
                    "ON CONFLICT(sha256) DO NOTHING",
                    (response.sha256, zlib.compress(response.body)),
                ),
                (
                    "INSERT INTO raw_observations "
                    "(response_id,experiment_id,run_id,sha256,metadata_json) "
                    "VALUES (?,?,?,?,?)",
                    (
                        response_id,
                        self.experiment_id,
                        self.run_id,
                        response.sha256,
                        canonical_json(metadata),
                    ),
                ),
            )
        )
        return response_id

    def history(self, captured: CapturedHistorySnapshot) -> None:
        response, snapshot = captured.response, captured.snapshot
        if (
            response.clock_anomaly
            or not response.body_complete
            or response.response_received_utc_ns > self.deadline_ns
        ):
            return
        candidates = [
            c
            for c in snapshot.candles
            if (c.time_s + 300) * 10**9 in self.endpoint_boundaries
            and (snapshot.underlying, c.time_s) not in self._seen
            and (c.time_s + 300) * 10**9 <= response.response_received_utc_ns
        ]
        if not candidates:
            return
        response_id = uuid.uuid4().hex
        statements = [
            _source_response_statement(
                response_id=response_id,
                experiment_id=self.experiment_id,
                run_id=self.run_id,
                request_id=response_id,
                response_kind="history",
                asset=snapshot.underlying,
                source_symbol=snapshot.symbol,
                response=response,
            )
        ]
        for candle in candidates:
            statements.append(
                (
                    "INSERT INTO candle_observations "
                    "(observation_id,experiment_id,run_id,request_id,"
                    "response_id,asset,source_symbol,source_timestamp_raw,normalized_open_ns,"
                    "normalized_close_boundary_ns,open_decimal,high_decimal,low_decimal,close_decimal,"
                    "volume_decimal,request_started_utc_ns,response_received_utc_ns,request_elapsed_ns,"
                    "source_payload_sha256,raw_candle_json,parse_status) "
                    "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                    (
                        uuid.uuid4().hex,
                        self.experiment_id,
                        self.run_id,
                        response_id,
                        response_id,
                        snapshot.underlying,
                        snapshot.symbol,
                        str(candle.time_s),
                        candle.time_s * 10**9,
                        (candle.time_s + 300) * 10**9,
                        str(candle.open),
                        str(candle.high),
                        str(candle.low),
                        str(candle.close),
                        str(candle.volume),
                        response.request_started_utc_ns,
                        response.response_received_utc_ns,
                        response.request_elapsed_ns,
                        response.sha256,
                        canonical_json(_raw_candle_records(response)[candle.time_s]),
                        "parsed",
                    ),
                )
            )
        self.submit(tuple(statements))
        self._seen.update((snapshot.underlying, c.time_s) for c in candidates)


class ArchivedMarketData(Experiment1CapturedMarketData):
    """Retain raw bodies even if frozen parsing raises; callbacks cannot change results."""

    def __init__(
        self,
        archive: EvidenceArchive,
        *,
        stop: Event,
        utc_ns: Callable[[], int],
        start_ns: int,
        end_ns: int,
        timeout_seconds: float = 20,
    ) -> None:
        super().__init__(timeout_seconds=timeout_seconds, stop=stop)
        self.archive = archive
        self.utc_ns = utc_ns
        self.start_ns = start_ns
        self.end_ns = end_ns
        self.tail = False
        self._raw_ids: dict[tuple[str, str], str] = {}

    def _callback(self, asset: str, kind: str) -> Callable[[RawPublicResponse], None]:
        def receive(response: RawPublicResponse) -> None:
            try:
                self._raw_ids[(asset, kind)] = response.capture_id
                self.archive.raw(asset, kind, response)
            except Exception as exc:
                self.archive.fault = f"raw capture failed: {exc}"

        return receive

    def fetch_5m_candles(
        self, underlying: DeltaUnderlying, *, count: int = 240, now_s: int | None = None
    ) -> DeltaCandleSnapshot:
        self._raw_ids.pop((underlying, "history"), None)
        try:
            captured = fetch_captured_5m_candles(
                underlying,
                count=count,
                now_s=now_s,
                timeout=self._timeout_seconds,
                stop=self._stop,
                on_response=self._callback(underlying, "history"),
            )
        except Exception as exc:
            self.archive.record(
                "source_parse_failed",
                {
                    "asset": underlying,
                    "kind": "history",
                    "error": str(exc),
                    "response_id": self._raw_ids.get((underlying, "history")),
                },
            )
            raise
        self._history[underlying] = captured
        self.archive.history(captured)
        self.archive.record(
            "source_parse_completed",
            {
                "asset": underlying,
                "kind": "history",
                "sha256": captured.response.sha256,
                "response_id": captured.response.capture_id,
            },
        )
        boundary = (captured.snapshot.candle_close_ms + 1) * 10**6
        now = self.utc_ns()
        # Research eligibility, not signal logic: never evaluate a future, stale,
        # pre-window or endpoint-tail candle as a prospective scheduled signal.
        if not self.tail and not (
            self.start_ns <= boundary < self.end_ns and boundary <= now < boundary + 300 * 10**9
        ):
            raise ValueError("history outside prospective evaluation slot")
        return captured.snapshot

    def fetch_option_chain(self, underlying: DeltaUnderlying) -> DeltaOptionChainSnapshot:
        self._raw_ids.pop((underlying, "option_chain"), None)
        try:
            captured = fetch_captured_option_chain(
                underlying,
                timeout=self._timeout_seconds,
                stop=self._stop,
                on_response=self._callback(underlying, "option_chain"),
            )
        except Exception as exc:
            self.archive.record(
                "source_parse_failed",
                {
                    "asset": underlying,
                    "kind": "option_chain",
                    "error": str(exc),
                    "response_id": self._raw_ids.get((underlying, "option_chain")),
                },
            )
            raise
        self._option_chains[underlying] = captured
        self.archive.record(
            "source_parse_completed",
            {
                "asset": underlying,
                "kind": "option_chain",
                "sha256": captured.response.sha256,
                "response_id": captured.response.capture_id,
            },
        )
        return captured.snapshot
