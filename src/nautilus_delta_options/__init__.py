"""Paper-only application. Run in its Python 3.12 Docker environment."""

import sys

if sys.version_info[:2] != (3, 12):
    raise RuntimeError(
        "Nautilus Delta Options supports Python 3.12 in its Docker container. "
        "Do not run application modules with the VPS host Python. Leave host Python unchanged."
    )
