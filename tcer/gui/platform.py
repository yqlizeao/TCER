"""Cross-platform GUI helpers: fonts, file-manager open, mousewheel binding.

Centralises every ``sys.platform`` branch so the rest of the GUI imports
simple constants / functions instead of scattering OS checks.
"""
from __future__ import annotations

import sys

PLATFORM = sys.platform  # 'win32', 'darwin', 'linux'

# 可点击控件光标：Windows/Linux 用 hand2（手型，可点击反馈）；mac 的 Tk hand2 是
# 低清位图小手、观感差，且 mac 原生按钮 hover 不变光标，故 mac 用默认（空串）。
CLICK_CURSOR = "" if PLATFORM == "darwin" else "hand2"

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


# ---------------------------------------------------------------------------
# Windows 标题栏深色（主窗口 + 所有子窗口共用）
# ---------------------------------------------------------------------------
_DARK_THEME: bool | None = None  # 缓存系统 AppsUseLightTheme 读取结果


def apply_dark_titlebar(widget) -> None:
    """Windows: 让 Tk 窗口（主窗口或子窗口）标题栏跟随系统暗/亮主题。

    Tk 默认不请求「沉浸式暗色标题栏」(DWMWA_USE_IMMERSIVE_DARK_MODE)，故系统
    暗色模式下 Tk 标题栏仍浅色，与深色界面冲突。读注册表系统主题（首次缓存）
    并设 DWM 属性。主窗口与 ``widgets.new_window`` 创建的子窗口统一调用。
    **限制**：仅启动时检测，运行中切换系统主题需重启 GUI 才生效。
    """
    if PLATFORM != "win32":
        return  # mac/linux：标题栏由系统绘制，跟随系统外观（mac 深色模式即深色标题栏，无需 DWM）
    global _DARK_THEME
    try:
        import ctypes
        from ctypes import wintypes
        if _DARK_THEME is None:
            light = 1
            HKCU = 0x80000001
            SUB = r"Software\Microsoft\Windows\CurrentVersion\Themes\Personalize"
            key = wintypes.HKEY()
            if ctypes.windll.advapi32.RegOpenKeyExW(HKCU, SUB, 0, 0x20019, ctypes.byref(key)) == 0:
                val = wintypes.DWORD()
                size = wintypes.DWORD(4)
                if ctypes.windll.advapi32.RegQueryValueExW(
                        key, "AppsUseLightTheme", None, None,
                        ctypes.byref(val), ctypes.byref(size)) == 0:
                    light = val.value
                ctypes.windll.advapi32.RegCloseKey(key)
            _DARK_THEME = light == 0  # AppsUseLightTheme=0 → 暗色
        widget.update_idletasks()  # 确保窗口已映射，DWM 属性才生效
        hwnd = widget.winfo_id()
        parent = ctypes.windll.user32.GetParent(hwnd)  # Tk toplevel 的真实顶层 HWND
        target = parent or hwnd
        v = ctypes.c_int(1 if _DARK_THEME else 0)
        for attr in (20, 19):  # 20=Win10 2004+/Win11，19=旧 build
            ctypes.windll.dwmapi.DwmSetWindowAttribute(
                target, attr, ctypes.byref(v), ctypes.sizeof(v))
    except (OSError, AttributeError):
        pass
