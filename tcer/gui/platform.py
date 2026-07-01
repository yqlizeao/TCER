"""Cross-platform GUI helpers: fonts, file-manager open, mousewheel binding.

Centralises every ``sys.platform`` branch so the rest of the GUI imports
simple constants / functions instead of scattering OS checks.
"""
from __future__ import annotations

import sys

PLATFORM = sys.platform  # 'win32', 'darwin', 'linux'

# ---------------------------------------------------------------------------
# Fonts — each OS picks its best CJK / monospace font; tkinter falls back
# gracefully if the exact name is missing.
# ---------------------------------------------------------------------------
if PLATFORM == "darwin":
    FONT_CJK = "PingFang SC"
    FONT_MONO_NAME = "Menlo"
elif PLATFORM == "linux":
    FONT_CJK = "Noto Sans CJK SC"
    FONT_MONO_NAME = "DejaVu Sans Mono"
else:  # win32
    FONT_CJK = "Microsoft YaHei"
    FONT_MONO_NAME = "Consolas"


# ---------------------------------------------------------------------------
# File-manager open
# ---------------------------------------------------------------------------
def open_in_file_manager(path: str) -> None:
    """Open *path* in the platform's default file manager.

    On Windows, file paths use ``explorer /select`` so the file is
    highlighted in its parent directory rather than opened by its
    default application.
    """
    import subprocess
    from pathlib import Path
    try:
        if PLATFORM == "darwin":
            subprocess.Popen(["open", path])
        elif PLATFORM == "linux":
            subprocess.Popen(["xdg-open", path])
        else:
            if Path(path).is_file():
                subprocess.Popen(["explorer", f"/select,{path}"])
            else:
                subprocess.Popen(["explorer", path])
    except Exception:
        pass


FILE_MANAGER_NAME: str = {
    "win32": "资源管理器",
    "darwin": "Finder",
    "linux": "文件管理器",
}[PLATFORM]


# ---------------------------------------------------------------------------
# Mousewheel — three different conventions across OS/tk builds
# ---------------------------------------------------------------------------
def bind_mousewheel(canvas, callback):
    """Bind mouse-wheel scrolling on *canvas*, calling ``callback(units)``.

    Returns an *unbind* callable; invoke it on ``<Leave>`` to detach.
    """
    if PLATFORM == "darwin":
        # macOS tk: <MouseWheel>, delta is ±1 per notch
        handler = lambda e: callback(int(-e.delta))
        canvas.bind_all("<MouseWheel>", handler)
        return lambda: canvas.unbind_all("<MouseWheel>")

    if PLATFORM == "linux":
        # X11/Wayland: Button-4 = scroll up, Button-5 = scroll down
        def _on_up(e):
            callback(1)
        def _on_down(e):
            callback(-1)
        canvas.bind_all("<Button-4>", _on_up)
        canvas.bind_all("<Button-5>", _on_down)
        return lambda: (canvas.unbind_all("<Button-4>"),
                        canvas.unbind_all("<Button-5>"))

    # Windows: <MouseWheel>, delta is ±120 per notch
    handler = lambda e: callback(int(-e.delta / 120))
    canvas.bind_all("<MouseWheel>", handler)
    return lambda: canvas.unbind_all("<MouseWheel>")
