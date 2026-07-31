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
ALLOWED_RESIDUAL_HEX = {"#007acc", "#14202e", "#142814", "#1a3a5a", "#2a2a2a", "#2e1414", "#2e1e14", "#2e2a14", "#2fc4c4", "#3a3a3a", "#42a5f5", "#444444", "#4aa8ec", "#4cc96a", "#4ec9b0", "#555555", "#569cd6", "#6B7077", "#888888", "#9cdcfe", "#9d9da5", "#b65bd6", "#c586c0", "#ce9178", "#dcdcaa", "#e2a23c", "#e53935", "#ef6c00", "#f9a825"}

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
