"""``python -m tcer.gui`` — alias entry that launches the GUI."""
from __future__ import annotations

import sys

from . import main

if __name__ == "__main__":
    sys.exit(main())
