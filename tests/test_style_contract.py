"""样式契约测试（doc/style.md 的可执行版）。

防回退：§1 禁 magic hex、§3 禁手写 tk.Button、§4 禁 tk.Menu、§16 禁 Checkbutton。
magic hex 用棘轮策略：冻结现存允许集，只许减少不许新增——新颜色必须进 theme.py。
"""
import pathlib
import re

GUI = pathlib.Path(__file__).resolve().parent.parent / "tcer" / "gui"
# theme.py 是色值 SSOT；html_report.py 是 HTML/CSS 字符串，暂豁免（待收编）。
HEX_EXEMPT = {"theme.py", "html_report.py"}

# 棘轮允许集：这些 hex 是历史残留（语义套色，待收编进 theme）。
# 只可从此集合删除条目，禁止添加——新增颜色一律定义在 theme.py。
# （2026-09-15 收紧：模型对比 _COL_COLORS 六色删除改用 CHART_PALETTE；
#  widgets 提白白名单死值 #888888/#6B7077 删除。仅剩 charts/popups 画布暗刻度两值）
ALLOWED_RESIDUAL_HEX = {"#444444", "#555555"}

def _gui_sources():
    for f in sorted(GUI.glob("*.py")):
        yield f, f.read_text(encoding="utf-8")


def test_no_new_magic_hex():
    violations = []
    for f, src in _gui_sources():
        if f.name in HEX_EXEMPT:
            continue
        for i, line in enumerate(src.splitlines(), 1):
            for h in re.findall(r'"(#[0-9a-fA-F]{3,8})"', line):
                if h not in ALLOWED_RESIDUAL_HEX:
                    violations.append(f"{f.name}:{i}: {h}")
    assert not violations, (
        "新增 magic hex，请改为 theme.py 常量 (style.md §1):\n" + "\n".join(violations))


def test_no_tk_menu():
    bad = [f.name for f, src in _gui_sources() if re.search(r"\btk\.Menu\(", src)]
    assert not bad, f"禁用 tk.Menu，用 widgets.FlatMenu (style.md §4): {bad}"


def test_no_checkbutton():
    bad = []
    for f, src in _gui_sources():
        for i, line in enumerate(src.splitlines(), 1):
            if re.search(r"\b(tk|ttk)\.Checkbutton\(", line) and "style-exempt" not in line:
                bad.append(f"{f.name}:{i}")
    assert not bad, f"禁用 Checkbutton，用 widgets.CheckRow (style.md §16): {bad}"


def test_no_handwritten_tk_button():
    bad = []
    for f, src in _gui_sources():
        for i, line in enumerate(src.splitlines(), 1):
            if re.search(r"\btk\.Button\(", line) and "style-exempt" not in line:
                bad.append(f"{f.name}:{i}")
    assert not bad, f"禁止手写 tk.Button，用 widgets.flat_button (style.md §3): {bad}"


def test_format_plot_no_scientific_notation():
    """style.md §8：图表 tooltip 数值禁科学计数法（:g 对 |v|<1e-4 会漏）。"""
    from tcer.gui.metric_defs import format_plot
    for v in (0.000012, 1.5e-05, -3e-06, 0.5, 999.9, 1234.5, 1.2e7, 0.0):
        out = format_plot("cost", v, None)
        assert "e" not in out.lower(), f"{v} -> {out}"


def test_card_no_border_state():
    """Card 体系边框退出状态表达（style.md §Card）：widgets/views 不得再出现
    highlightbackground 状态/装饰边框——hover/selected 全靠 bg + 左 rail，
    容器靠明度阶。豁免：PhasePortraitWidget 类段（相图功能专区，待专项收编）。"""
    import ast

    EXEMPT_CLASSES = {"PhasePortraitWidget"}

    def _bad_lines(src: str) -> list[int]:
        tree = ast.parse(src)
        exempt_ranges = []
        for node in ast.walk(tree):
            if isinstance(node, ast.ClassDef) and node.name in EXEMPT_CLASSES:
                exempt_ranges.append((node.lineno, node.end_lineno))
        hits = []
        for i, line in enumerate(src.splitlines(), 1):
            if "highlightbackground" not in line:
                continue
            if any(a <= i <= b for a, b in exempt_ranges):
                continue
            hits.append(i)
        return hits

    for name in ("widgets.py", "views.py"):
        src = (GUI / name).read_text(encoding="utf-8")
        hits = _bad_lines(src)
        assert not hits, (
            f"{name} 禁用 highlightbackground 状态边框（hover/selected 用 bg + rail, "
            "容器用明度阶, style.md §Card）: 行 {hits}")


def test_font_value_discipline():
    """数值字体纪律：FONT_VALUE 必须与正文字号一致（10pt bold），
    层级靠字重+等宽家族——防止仪表盘大字号回潮。"""
    from tcer.gui import theme
    assert theme.FONT_VALUE[1] == theme.FONT_UI[1] == 10, (
        f"FONT_VALUE={theme.FONT_VALUE} 必须与 FONT_UI={theme.FONT_UI} 同字号（纪律派）")
    assert theme.FONT_VALUE[2] == "bold"
    assert len(theme.FONT_UI) == 2  # 正文 regular（无 weight 项）

