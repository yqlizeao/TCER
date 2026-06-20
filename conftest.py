"""Pytest bootstrap: put the repo root on the import path.

The ``tcer`` package lives at the repo root. This conftest ensures
``python -m pytest`` finds it regardless of which directory it's run from.
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
