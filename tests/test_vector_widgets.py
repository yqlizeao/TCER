"""验证三层抗锯齿圆角矢量渲染引擎（Tk 9.0 SVG / Pillow 4x / 纯标准库 SDF PNG）、
9-slice 容器以及 5 个 OpenResearch 级核心组件（SurfaceCard / SegmentedControl /
ToggleSwitch / StatusBadge / RoundedEntry）的渲染、交互与 TclObj 契约。"""
from __future__ import annotations

import tkinter as tk
import pytest

from tcer.gui import theme, widgets


@pytest.fixture(scope="module")
def tk_root():
    try:
        r = tk.Tk()
        r.withdraw()
    except tk.TclError:
        pytest.skip("No Tk display available")
    yield r
    try:
        r.destroy()
    except tk.TclError:
        pass


class _FakeTclObj:
    """模拟 Tk 8.6 / 9.0 cget() 返回非 str 的 Tcl_Obj 包装对象。"""

    def __init__(self, val: str) -> None:
        self._val = val

    def __str__(self) -> str:
        return self._val


def test_sdf_png_bytes_pure_stdlib_header_and_dimensions(tk_root):
    """纯标准库 SDF 抗锯齿圆角渲染器必须输出合法 PNG 字节且可被 tk.PhotoImage 直接解码。"""
    png = widgets._render_sdf_png_bytes(
        64, 28, 8, theme.SURFACE_CARD, theme.BG, outline=theme.BORDER_SUBTLE, outline_width=1
    )
    assert png.startswith(b"\x89PNG\r\n\x1a\n")
    assert b"IHDR" in png and b"IDAT" in png and b"IEND" in png
    img = tk.PhotoImage(master=tk_root, data=png, format="PNG")
    assert img.width() == 64
    assert img.height() == 28


def test_three_tier_rounded_engine_fallback_and_9slice(tk_root, monkeypatch):
    """禁用 PIL 与 Tk9 SVG 时，纯标准库 SDF 层必须无缝接管圆角矩形、左右端帽与四角 9-slice。"""
    widgets._ROUNDED_IMG_CACHE.clear()
    widgets._ROUNDED_CAPS_CACHE.clear()
    widgets._ROUNDED_CORNERS_CACHE.clear()
    monkeypatch.setattr(widgets, "_ALLOW_PIL", False)
    monkeypatch.setattr(widgets, "_ALLOW_TK9_SVG", False)

    rect_img = widgets.get_rounded_rect_img(
        tk_root, 80, 26, theme.RADIUS_M, theme.SURFACE_CARD, theme.BG, outline=theme.BORDER_SUBTLE
    )
    assert rect_img is not None
    assert rect_img.width() == 80 and rect_img.height() == 26

    left_cap, right_cap = widgets.get_rounded_caps(
        tk_root, 24, theme.RADIUS_M, theme.CONTROL_BG, theme.BG
    )
    assert left_cap is not None and right_cap is not None
    assert left_cap.width() == 2 * theme.RADIUS_M and left_cap.height() == 24
    assert right_cap.width() == 2 * theme.RADIUS_M and right_cap.height() == 24

    cs, quads = widgets.get_rounded_corners(
        tk_root, theme.RADIUS_L, theme.SURFACE_CARD, theme.BG, outline=theme.BORDER_SUBTLE
    )
    assert quads is not None and len(quads) == 4
    for c_img in quads:
        assert c_img.width() == cs and c_img.height() == cs

    # 9-slice 容器绘制与 resize 复用（宽度变化时不新增底层圆角四角缓存）
    cv = tk.Canvas(tk_root, width=200, height=90, bg=theme.BG, highlightthickness=0)
    widgets.draw_rounded_box(
        cv, 200, 90, theme.RADIUS_L, theme.SURFACE_CARD, theme.BG,
        outline=theme.BORDER_SUBTLE, tag="box_test"
    )
    corners_after_first = len(widgets._ROUNDED_CORNERS_CACHE)
    assert corners_after_first >= 1
    assert len(cv.find_withtag("box_test")) > 0

    widgets.draw_rounded_box(
        cv, 320, 140, theme.RADIUS_L, theme.SURFACE_CARD, theme.BG,
        outline=theme.BORDER_SUBTLE, tag="box_test"
    )
    assert len(widgets._ROUNDED_CORNERS_CACHE) == corners_after_first
    cv.destroy()


def test_status_badge_kinds_and_cget_compat(tk_root):
    """StatusBadge 支持 5 种语义色、圆点开关、configure/cget 兼容契约。"""
    f = tk.Frame(tk_root, bg=theme.BG)
    badge = widgets.StatusBadge(f, text="已配置 · claude", kind="success", dot=True)
    assert str(badge.cget("text")) == "已配置 · claude"
    assert str(badge.cget("fg")) == theme.SUCCESS

    badge.set_status("未填写", kind="warning", dot=False)
    assert str(badge.cget("text")) == "未填写"
    assert str(badge.cget("fg")) == theme.WARNING

    badge.configure(text="连接失败", fg=theme.ERROR)
    assert str(badge.cget("text")) == "连接失败"
    assert str(badge.cget("fg")) == theme.ERROR
    f.destroy()


def test_toggle_switch_and_segmented_control(tk_root):
    """ToggleSwitch 与 SegmentedControl 支持双向变量绑定、点击切换与 TclObj 父背景兼容。"""
    f = tk.Frame(tk_root, bg=theme.SURFACE_CARD)
    bvar = tk.BooleanVar(master=tk_root, value=False)
    toggled: list[bool] = []
    sw = widgets.ToggleSwitch(f, variable=bvar, command=lambda: toggled.append(bvar.get()))
    assert sw.get() is False
    sw.toggle()
    assert sw.get() is True and bvar.get() is True and toggled == [True]
    sw.configure(state="disabled")
    sw.toggle()
    assert sw.get() is True  # disabled 状态不响应点击

    svar = tk.StringVar(master=tk_root, value="all")
    changed: list[str] = []
    seg = widgets.SegmentedControl(
        f,
        [("all", "全部"), ("conv", "对话"), ("chg", "改动")],
        variable=svar,
        on_change=lambda v: changed.append(v),
    )
    assert seg.get() == "all"
    seg.set("chg", notify=True)
    assert seg.get() == "chg" and svar.get() == "chg" and changed == ["chg"]
    f.destroy()


def test_rounded_entry_placeholder_and_entry_delegation(tk_root):
    """RoundedEntry 占位提示绝不污染 .get()，且完整代理 Entry 的 insert/delete/select_range。"""
    f = tk.Frame(tk_root, bg=theme.SURFACE_CARD)
    v = tk.StringVar(master=tk_root, value="")
    re_box = widgets.RoundedEntry(
        f, textvariable=v, width=24, placeholder="https://api.anthropic.com"
    )
    # 占位提示显示时，get() 仍必须返回空串
    assert re_box.get() == ""
    assert re_box._ph_lbl is not None

    re_box.insert(0, "https://example.com")
    assert re_box.get() == "https://example.com"
    re_box.select_range(0, "end")
    re_box.delete(0, "end")
    assert re_box.get() == ""
    f.destroy()


def test_surface_card_and_touchpad_scroll_binding(tk_root):
    """SurfaceCard 9-slice 卡片构建与分割线、以及 platform.bind_mousewheel 对 TouchpadScroll 的兼容。"""
    from tcer.gui.platform import bind_mousewheel

    f = tk.Frame(tk_root, bg=theme.BG)
    card = widgets.SurfaceCard(f, title="服务配置", subtitle="兼容 OpenAI / Anthropic")
    card.pack(fill="x")
    tk.Label(card.body, text="第一行", bg=card.surface_bg, fg=theme.FG).pack(anchor="w")
    sep = card.add_divider()
    assert str(sep.cget("bg")) == theme.DIVIDER

    scrolled: list[int] = []
    bind_mousewheel(card, lambda u: scrolled.append(u))
    f.destroy()


def test_touchpad_raw_decode():
    """<TouchpadScroll> %d 回退解码：多值列表取第 2 项；裸 32 位按 WM_MOUSEWHEEL
    惯例取高 16 位为垂直分量（16 位补码符号）；垃圾输入安全归零。"""
    from tcer.gui.platform import touchpad_dy_from_raw

    assert touchpad_dy_from_raw("{4 -28}") == -28.0          # {dx dy} 像素对
    assert touchpad_dy_from_raw("{-7 120}") == 120.0
    # 高 16 位 = 垂直、低 16 位 = 水平（WM_MOUSEWHEEL 惯例）：只取高位
    assert touchpad_dy_from_raw((2 << 16) | ((-5) & 0xFFFF)) == 2.0
    assert touchpad_dy_from_raw(-2 << 16) == -2.0            # 高位 = -2（补码）
    assert touchpad_dy_from_raw(327680) == 5.0               # 5 << 16
    assert touchpad_dy_from_raw(0) == 0.0
    assert touchpad_dy_from_raw("garbage") == 0.0
    assert touchpad_dy_from_raw(None) == 0.0


def test_widget_option_contract(tk_root):
    """configure/cget 四种调用形契约（Tk 惯例）：单键查询返回 5 元组、dict 形
    生效、无参返回选项枚举 dict；cget("bg") 是稳定父底色而非悬停/胶囊面。"""
    f = tk.Frame(tk_root, bg=theme.BG)

    rf = widgets.RoundedFrame(f, fill=theme.SURFACE_CARD, parent_bg=theme.BG,
                              outline=theme.BORDER_SUBTLE, radius=theme.RADIUS_M)
    assert rf.configure("bg")[4] == theme.SURFACE_CARD       # 单键查询形
    assert rf.cget("bg") == theme.SURFACE_CARD
    rf.configure({"bg": theme.PANEL, "outline": theme.BORDER})   # dict 形生效
    assert str(rf.cget("bg")) == theme.PANEL and rf._rf_outline == theme.BORDER
    cfg = rf.configure()                                     # 无参枚举形
    assert isinstance(cfg, dict) and cfg["bg"][4] == theme.PANEL
    rf.destroy()

    badge = widgets.StatusBadge(f, text="已配置", kind="success", bg=theme.PANEL_2)
    assert str(badge.cget("bg")) == theme.PANEL_2            # 稳定父底色（非胶囊面）
    assert badge.configure("fg")[4] == theme.SUCCESS
    badge.configure({"text": "失败", "fg": theme.ERROR})     # dict 形一次改两项
    assert str(badge.cget("text")) == "失败" and str(badge.cget("fg")) == theme.ERROR
    badge.configure(bg=theme.BG)                             # kwargs 形改底色
    assert str(badge.cget("bg")) == theme.BG
    cfg = badge.configure()
    assert isinstance(cfg, dict) and cfg["text"][4] == "失败"
    badge.destroy()

    chip = widgets.RoundedKpiChip(f, label="成本", value="$1.00", bg=theme.BG)
    assert str(chip.cget("bg")) == theme.BG                  # 不随 hover 漂移
    chip._on_enter(None)
    assert str(chip.cget("bg")) == theme.BG
    chip.configure({"text": "$2.00"})
    assert chip.configure("text")[4] == "$2.00"
    chip.destroy()
    f.destroy()


def test_var_trace_released_on_destroy(tk_root):
    """ToggleSwitch / SegmentedControl / RoundedEntry 销毁时解除 Variable trace——
    tk Variable 以 PY_VARn 活在解释器级，不解除则闭包泄漏整棵控件子树。"""
    f = tk.Frame(tk_root, bg=theme.SURFACE_CARD)
    bvar = tk.BooleanVar(master=tk_root, value=False)
    sw = widgets.ToggleSwitch(f, variable=bvar)
    assert sw._trace_id is not None
    svar = tk.StringVar(master=tk_root, value="all")
    seg = widgets.SegmentedControl(f, [("all", "全部"), ("chg", "改动")], variable=svar)
    assert seg._var_trace_id is not None
    evar = tk.StringVar(master=tk_root, value="")
    ent = widgets.RoundedEntry(f, textvariable=evar)
    assert ent._var_trace_id is not None

    sw.destroy()
    seg.destroy()
    ent.destroy()
    # 销毁后 trace 已解除；变量继续写入不得触发任何已销毁回调路径
    assert sw._trace_id is None and seg._var_trace_id is None and ent._var_trace_id is None
    bvar.set(True)
    svar.set("chg")
    evar.set("x")
    f.destroy()


def test_rounded_frame_and_activity_bar_pill_collapse(tk_root):
    """RoundedFrame 原生几何主容器与 ActivityBar 悬浮圆角胶囊导航 + 一键收起/展开契约。"""
    from tcer.gui import views

    top = tk.Toplevel(tk_root)
    top.geometry("900x600")
    rf = widgets.RoundedFrame(
        top, fill=theme.SURFACE_CARD, parent_bg=theme.BG,
        outline=theme.BORDER_SUBTLE, radius=theme.RADIUS_L,
    )
    rf.pack(fill="x", padx=8, pady=8)
    lbl = tk.Label(rf, text="圆角主容器子项", bg=theme.SURFACE_CARD, fg=theme.FG)
    lbl.pack(padx=10, pady=8)
    tk_root.update_idletasks()
    rf._rf_on_configure()
    assert str(rf.cget("bg")) == theme.SURFACE_CARD
    assert len(rf.find_withtag("rf_bg")) > 0

    class _DummyCtrl:
        _ui_prefs = {"nav_collapsed": False}
        _nb = None

    ctrl = _DummyCtrl()
    bar = views.ActivityBar(top, ctrl)
    tk_root.update_idletasks()

    # 默认展开态：宽度 148，胶囊文字标签已挂载
    assert bar._collapsed is False
    assert widgets.to_int(bar.frame.cget("width")) == views.ActivityBar._W_EXPANDED
    assert bar._brand_lbl.winfo_manager() == "pack"
    assert len(bar._page_keys) == 7

    # 切换选中页签：高亮态切换至 PANEL_2 + BORDER
    bar.set_page_active(3)
    assert bar._active_page == "page:3"
    assert str(bar._items["page:3"][0].cget("bg")) == theme.PANEL_2
    assert str(bar._items["page:0"][0].cget("bg")) == theme.ACTIVITY_BG

    # 一键收起侧栏：宽度收缩为 50，中文标签隐藏，偏好写回
    bar.toggle_collapse()
    assert bar._collapsed is True
    assert ctrl._ui_prefs["nav_collapsed"] is True
    assert widgets.to_int(bar.frame.cget("width")) == views.ActivityBar._W_COLLAPSED
    assert bar._brand_lbl.winfo_manager() == ""

    # 再次点击恢复展开
    bar.toggle_collapse()
    assert bar._collapsed is False
    assert widgets.to_int(bar.frame.cget("width")) == views.ActivityBar._W_EXPANDED
    assert bar._brand_lbl.winfo_manager() == "pack"
    top.destroy()

