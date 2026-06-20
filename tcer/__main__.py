"""``python -m tcer`` — launch the TCER GUI."""
from __future__ import annotations

import sys

from tcer.gui import main

if __name__ == "__main__":
    sys.exit(main())
