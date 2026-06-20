"""``python -m tcer`` — launch the TCER GUI (the product's only entry point).

The CLI has been retired; TCER is GUI-only. ``python -m tcer.gui`` is an alias.
"""
from __future__ import annotations

import sys

from .gui import main

if __name__ == "__main__":
    sys.exit(main())
