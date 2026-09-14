"""Offline application-runtime smoke check; never calls exchange APIs."""

import importlib
import json
import os
import sys
import tempfile
from importlib.metadata import version
from pathlib import Path

from fastapi import Response


def main() -> None:
    if sys.version_info[:2] != (3, 12):
        raise RuntimeError("Supported application runtime is Python 3.12")
    for name in (
        "nautilus_trader",
        "numpy",
        "talib",
        "fastapi",
        "uvicorn",
        "nautilus_delta_options.paper.v34_paper_live",
        "nautilus_delta_options.paper.payoff_profiles",
        "nautilus_delta_options.paper.reconcile",
    ):
        importlib.import_module(name)
    for name, expected in (("nautilus_trader", "2.0.0rc3"), ("numpy", "2.3.5")):
        if version(name) != expected:
            raise RuntimeError(f"{name}: expected {expected}, got {version(name)}")
    from nautilus_delta_options.paper.reconcile import reconcile

    # Importing the ASGI module creates its app. Isolate every configured DB first.
    with tempfile.TemporaryDirectory(prefix="nautilus-runtime-") as folder:
        root = Path(folder)
        overrides = {
            "PAPER_DATABASE": str(root / "legacy.sqlite"),
            "V34_PAPER_DATABASE": str(root / "primary.sqlite"),
            "V34_PAYOFF_DATABASE": str(root / "research.sqlite"),
            "V34_PAPER_ENTRIES_ENABLED": "false",
            "V34_PAYOFF_PROFILE": "baseline_v34",
            "V34_PAYOFF_PENALTY": "0",
        }
        previous = {key: os.environ.get(key) for key in overrides}
        try:
            os.environ.update(overrides)
            module = importlib.import_module("nautilus_delta_options.web.v34_paper_app")
            app = module.create_v34_paper_app(entries_enabled=False)
            routes = {getattr(route, "path", ""): route for route in app.routes}
            for required in ("/", "/health", "/api/dashboard", "/api/payoff-research"):
                if required not in routes:
                    raise RuntimeError(f"Missing application route: {required}")
            response = Response()
            health = routes["/health"].endpoint(response)
            if response.status_code != 503 or health["real_orders_enabled"]:
                raise RuntimeError("Uninitialized service must fail readiness and prohibit orders")
            provenance = health["provenance"]
            if not provenance["run_id"] or not provenance["fingerprint"]:
                raise RuntimeError("Run identity missing")
            from nautilus_delta_options.paper.provenance import bind_provenance

            bind_provenance(root / "primary.sqlite", provenance)
            bind_provenance(root / "research.sqlite", provenance)
            html = routes["/"].endpoint().body
            if b"<html" not in html.lower():
                raise RuntimeError("Packaged dashboard HTML missing")
            if not reconcile(root / "primary.sqlite", root / "research.sqlite")["ok"]:
                raise RuntimeError("Fresh paper databases failed reconciliation")
        finally:
            for key, value in previous.items():
                if value is None:
                    os.environ.pop(key, None)
                else:
                    os.environ[key] = value
    print(
        json.dumps(
            {
                "ok": True,
                "python": sys.version.split()[0],
                "paper_only": True,
                "checks": [
                    "native imports",
                    "pinned core dependencies",
                    "ASGI factory",
                    "unready HTTP status",
                    "packaged HTML",
                    "SQLite reconciliation",
                    "persistent run provenance",
                ],
                "network_calls": 0,
                "docker_verified": False,
                "vps_verified": False,
            },
            sort_keys=True,
        )
    )


if __name__ == "__main__":
    main()
