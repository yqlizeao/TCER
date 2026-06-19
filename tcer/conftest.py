"""Pytest bootstrap: put the ``src/`` layout on the import path.

This project is run green/no-install (``python -m tcer.cli`` from ``src/``), so
there's no packaging config. This conftest lets ``python -m pytest`` from the
``tcer/`` directory import the ``tcer`` package without an editable install.
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent / "src"))
