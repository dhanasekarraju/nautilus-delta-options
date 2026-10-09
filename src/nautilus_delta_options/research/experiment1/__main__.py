"""Explicit research-only commands. Running this module without arguments collects nothing."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import signal
import time
from dataclasses import asdict
from datetime import UTC, datetime
from pathlib import Path
from threading import Event

from nautilus_delta_options.research.experiment1.collector import (
    checked_path,
    initialize,
    make_manifest,
    run_collection,
    runtime_identity,
    validate_manifest,
    verify_database,
)
from nautilus_delta_options.research.experiment1.manifest import canonical_json
from nautilus_delta_options.research.experiment1.models import ExperimentManifest


def utc_ns(value: str) -> int:
    date = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if date.tzinfo is None or date.utcoffset() != UTC.utcoffset(date):
        raise argparse.ArgumentTypeError("explicit UTC timestamp required")
    return int(date.timestamp()) * 10**9


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    seal = commands.add_parser("seal", help="offline manifest creation; no market requests")
    seal.add_argument("--experiment-id", required=True)
    seal.add_argument("--start", type=utc_ns, required=True)
    seal.add_argument("--end", type=utc_ns, required=True)
    seal.add_argument("--deadline", type=utc_ns, required=True)
    seal.add_argument("--protocol", type=Path, required=True)
    seal.add_argument("--output", type=Path, required=True)
    seal.add_argument("--build-id", required=True)
    for name in ("init", "verify", "run"):
        sub = commands.add_parser(name)
        sub.add_argument("--root", type=Path, default=Path("/research-data"))
        sub.add_argument("--database", type=Path, required=True)
        sub.add_argument("--manifest", type=Path, required=True)
        sub.add_argument("--manifest-sha256", required=True)
        sub.add_argument("--protocol", type=Path, required=True)
        sub.add_argument("--build-id", default=os.environ.get("EXPERIMENT1_BUILD_ID", ""))
        if name == "run":
            sub.add_argument("--authorize-public-collection", action="store_true")
    args = parser.parse_args()
    runtime_identity()
    if args.command == "seal":
        manifest = make_manifest(
            args.experiment_id,
            args.build_id,
            args.start,
            args.end,
            args.deadline,
            args.protocol.read_bytes(),
            time.time_ns(),
        )
        data = (canonical_json(asdict(manifest)) + "\n").encode()
        with args.output.open("xb") as handle:
            handle.write(data)
        print(
            json.dumps(
                {"manifest_sha256": hashlib.sha256(data).hexdigest(), "collection_started": False}
            )
        )
        return
    data = args.manifest.read_bytes()
    if hashlib.sha256(data).hexdigest() != args.manifest_sha256:
        raise ValueError("manifest file hash differs from sealed authorization")
    manifest = ExperimentManifest(**json.loads(data))
    validate_manifest(manifest, args.build_id)
    if hashlib.sha256(args.protocol.read_bytes()).hexdigest() != manifest.protocol_sha256:
        raise ValueError("protocol hash mismatch")
    path = checked_path(args.root, args.database)
    if args.command == "init":
        initialize(path, manifest, time.time_ns())
    elif args.command == "verify":
        verify_database(path, manifest)
    else:
        if not args.authorize_public_collection:
            parser.error("run requires explicit --authorize-public-collection")
        stop = Event()
        signal.signal(signal.SIGTERM, lambda *_: stop.set())
        signal.signal(signal.SIGINT, lambda *_: stop.set())
        run_collection(path, manifest, stop)
    print(json.dumps({"ok": True, "command": args.command, "paper_authority": False}))


if __name__ == "__main__":
    main()
