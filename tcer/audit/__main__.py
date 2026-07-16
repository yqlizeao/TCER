"""``python -m tcer.audit`` entrypoint."""
from __future__ import annotations

import sys

from tcer.core.audit import main

if __name__ == "__main__":
    raise SystemExit(main())
