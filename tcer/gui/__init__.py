"""TCER Tkinter GUI package.

Launch via ``python -m tcer`` (or ``python -m tcer.gui``). ``main()`` is wired up
in this package's ``__main__``; the controller lives in ``app``.
"""
from __future__ import annotations


def main() -> int:
    """Entry point — implemented lazily to keep tkinter import out of headless use."""
    from .app import TcerGui
    return TcerGui.run()


__all__ = ["main"]
