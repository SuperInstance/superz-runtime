"""
SuperZ Runtime — Package Entry Point.

Usage::

    python -m superz_runtime              # boot the fleet
    python -m superz_runtime --headless   # daemon mode
    python -m superz_runtime --doctor     # run diagnostics
"""

from __future__ import annotations

import sys
from pathlib import Path

# Ensure the package root is on sys.path for direct module execution
_PACKAGE_ROOT = Path(__file__).resolve().parent
if str(_PACKAGE_ROOT) not in sys.path:
    sys.path.insert(0, str(_PACKAGE_ROOT))

from runtime import main  # noqa: E402

if __name__ == "__main__":
    main()
