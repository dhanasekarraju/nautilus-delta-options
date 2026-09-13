"""Offline application-runtime smoke check; never calls exchange APIs."""

import importlib
import sys
from importlib.metadata import version


def main() -> None:
    if sys.version_info[:2] != (3, 12):
        raise RuntimeError("Supported application runtime is Python 3.12")
    for name in (
        "nautilus_trader", "numpy", "talib", "fastapi", "uvicorn",
        "nautilus_delta_options.paper.v34_paper_live",
    ):
        importlib.import_module(name)
    for name, expected in (("nautilus_trader", "2.0.0rc3"), ("numpy", "2.3.5")):
        if version(name) != expected:
            raise RuntimeError(f"{name}: expected {expected}, got {version(name)}")
    print("Python 3.12 paper application imports and pinned core dependencies: OK")


if __name__ == "__main__":
    main()
