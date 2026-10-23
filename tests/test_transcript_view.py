"""会话记录阅读器 GUI 测试：Markdown 解析（纯函数）+ 完整 TcerGui 入口级验收。

入口级测试走真实回调链：会话卡片右键菜单 →「查看会话记录」→ ``TcerGui.show_transcript`` →
worker 线程 ``load_transcript`` → 窗口自有队列 60ms 轮询回填 → 分批渲染。无显示环境自动 skip。
"""
from __future__ import annotations

import dataclasses
import json
import time
from pathlib import Path

import pytest

from tcer.gui import transcript_md as MD

tk = pytest.importorskip("tkinter")


# =========================================================================== #
# Markdown 解析（纯函数）
# =========================================================================== #
def test_md_blocks_basic():
    text = ("# 标题\n\n段落 **粗** 和 `code`\n\n- 一\n  - 二\n1. 三\n- [x] 完成\n\n> 引用\n\n---\n\n"
            "```python\nprint(1)\n```\n\n| a | b |\n|---|---|\n| 1 | 2 |\n")
    kinds = [b[0] for b in MD.parse_blocks(text)]
    assert kinds == ["h", "p", "li", "li", "li", "li", "quote", "hr", "code", "table"]
    blocks = MD.parse_blocks(text)
    assert blocks[0] == ("h", 1, "标题")
    assert blocks[2][:3] == ("li", 0, "•") and blocks[3][1] == 1
    assert blocks[4][2] == "1." and blocks[5][2] == "[x]"
    assert blocks[8] == ("code", "python", "print(1)")
    assert blocks[9][1] == [["a", "b"], ["1", "2"]]


def test_md_unterminated_fence_runs_to_end():
    blocks = MD.parse_blocks("```\nA\nB")
    assert blocks == [("code", "", "A\nB")]


def test_md_over_limit_degrades_to_plain_but_keeps_fences():
    big = "# not a heading\n" + "x" * 100
    blocks = MD.parse_blocks(big + "\n```\ncode\n```\n", limit=50)
    assert blocks[0][0] == "p" and blocks[0][1].startswith("# not")
    assert ("code", "", "code") in blocks


def test_md_inline_segments():
    segs = MD.inline_segments("a **b `c`** d *e* ~~f~~ [g](http://x) ![i](p.png) http://u")
    flat = {s: f for s, f in segs}
    assert flat["b "] == ("md_bold",)
    assert flat["c"] == ("md_bold", "md_code")
    assert flat["e"] == ("md_italic",)
    assert flat["f"] == ("md_strike",)
    assert flat["g"] == ("md_link",)
    assert any(s.startswith(" (http://x") for s, _ in segs)
    assert any(s == "[图片：i]" for s, _ in segs)
    assert any(s == "http://u" and f == ("md_link",) for s, f in segs)
    # 不是斜体：snake_case / 乘号
    assert [s for s, _ in MD.inline_segments("a_b_c 2*3*4")] == ["a_b_c 2*3*4"]


def test_md_table_layout_rows_and_rules():
    widths, lines = MD.layout_table([["名称", "n"], ["a", "10"], ["b", "2"]], 80)
    assert [k for k, _ in lines] == ["head", "sep", "body", "sep", "body"]
    assert lines[0][1] == ["名称", "n"] and lines[2][1] == ["a", "10"]
    assert MD.display_width("名称") == 4
    assert widths == [4, 2]                      # 显示列口径：列宽 = 该列最宽单元格


def test_md_table_strips_inline_marks_and_wraps_to_fit():
    """单元格去行内标记；超宽时收缩列宽并折行——内容不丢、每列不超列宽、总宽不超可用宽。"""
    long = "文档列表 V2（返回 Gzip 压缩的二进制文档流），主界面首屏消费点"
    rows = [["端点", "URL", "说明"],
            ["**`get-documents-v2`**", "`https://api.granola.ai/v2/get-documents`", long]]
    widths, lines = MD.layout_table(rows, 60, gap=3)
    assert sum(widths) + 3 * (len(widths) - 1) <= 60
    cells = [c for _, ln in lines for c in ln]
    assert not any("**" in c or "`" in c or "…" in c for c in cells)
    body = [ln for k, ln in lines if k == "body"]
    assert len(body) > 1                                   # 折行而非截断
    assert all(MD.display_width(c) <= w for ln in body for c, w in zip(ln, widths))
    joined = "".join(ln[2] for ln in body)
    assert joined.replace(" ", "") == long.replace(" ", "")
    assert "".join(ln[1] for ln in body) == "https://api.granola.ai/v2/get-documents"


# =========================================================================== #
# 入口级：完整 TcerGui
# =========================================================================== #
_T0 = "2026-10-01T10:{m:02d}:{s:02d}Z"


def _ts(sec: int) -> str:
    return _T0.format(m=sec // 60, s=sec % 60)


def _cu(text, sec):
    return {"type": "user", "uuid": f"u{sec}", "timestamp": _ts(sec), "sessionId": "sess-1",
            "cwd": "C:/proj/demo", "message": {"role": "user", "content": text}}


def _ca(content, sec, mid):
    return {"type": "assistant", "uuid": f"a{sec}", "timestamp": _ts(sec), "sessionId": "sess-1",
            "cwd": "C:/proj/demo",
            "message": {"role": "assistant", "id": mid, "model": "claude-opus-4-8",
                        "content": content,
                        "usage": {"input_tokens": 100, "output_tokens": 50,
                                  "cache_read_input_tokens": 1000, "cache_creation_input_tokens": 10}}}


def _tu(cid, name, inp):
    return {"type": "tool_use", "id": cid, "name": name, "input": inp}


def _cr(cid, text, sec, is_error=False, tur=None):
    d = {"type": "user", "uuid": f"r{sec}", "timestamp": _ts(sec), "sessionId": "sess-1",
         "message": {"role": "user", "content": [
             {"type": "tool_result", "tool_use_id": cid, "content": text,
              **({"is_error": True} if is_error else {})}]}}
    if tur is not None:
        d["toolUseResult"] = tur
    return d


def _write_session(dir_: Path, sid="sess-1", n_filler=28) -> Path:
    """第 1 轮：提问 + 失败的 Bash + Read + Edit + 最终回复；其后若干纯问答轮（让文档足够长可滚动）。"""
    lines = [
        {"type": "ai-title", "aiTitle": "修复登录流程并补测试", "sessionId": sid},
        _cu("帮我修复登录 bug，并补充测试", 0),
        _ca([{"type": "text", "text": "我先跑一下测试"},
             _tu("t1", "Bash", {"command": "pytest -x tests/test_login.py"})], 5, "m1"),
        _cr("t1", "Exit code 1\nFAILED tests/test_login.py::test_login\n"
            + "\n".join(f"out line {i}" for i in range(20)), 9, True),
        _ca([_tu("t2", "Read", {"file_path": "C:/proj/demo/login.py", "offset": 1, "limit": 40})], 10, "m2"),
        _cr("t2", "def login():\n    pass", 11),
        _ca([_tu("t3", "Edit", {"file_path": "C:/proj/demo/login.py",
                                "old_string": "def login():\n    pass",
                                "new_string": "def login(user):\n    return user is not None"})], 12, "m3"),
        _cr("t3", "ok", 13, tur={"type": "update", "filePath": "C:/proj/demo/login.py",
                                 "structuredPatch": [{"oldStart": 1, "newStart": 1, "lines": [
                                     "-def login():", "-    pass", "+def login(user):",
                                     "+    return user is not None"]}]}),
        _ca([{"type": "text", "text": "## 修复完成\n\n已把 `login()` 改为接收 **user** 参数。\n\n- 补充了测试\n"}], 20, "m4"),
    ]
    sec = 30
    for i in range(n_filler):
        lines.append(_cu(f"第 {i} 个追问：README 要不要更新？", sec))
        lines.append(_ca([{"type": "text", "text": f"回复 {i}：不需要更新。\n\n" + "说明 " * 40}], sec + 4, f"f{i}"))
        sec += 8
    d = dir_ / "proj"
    d.mkdir(parents=True, exist_ok=True)
    p = d / f"{sid}.jsonl"
    with p.open("w", encoding="utf-8") as fh:
        for o in lines:
            fh.write(json.dumps(o, ensure_ascii=False) + "\n")
    return p


def _report(path: Path, sid="sess-1", title="修复登录流程并补测试"):
    from tcer.core import metrics
    from tcer.core.models import SessionMeta, TokenUsage
    meta = SessionMeta(session_id=sid, cwd="C:/proj/demo", title=title, path=path,
                       is_subagent=False, source="claude")
    u = TokenUsage(input_tokens=1000, output_tokens=500, models={"claude-opus-4-8"},
                   assistant_msgs=3, user_msgs=2)
    return metrics.compute(meta, u, net_loc=10, task_type="feature")


def _pump(root, cond, timeout=8.0):
    deadline = time.time() + timeout
    while time.time() < deadline:
        root.update()
        if cond():
            return True
        time.sleep(0.01)
    return False


def _ready(root, win, timeout=10.0):
    return _pump(root, lambda: win._state in ("ready", "error") and win._render_done, timeout)


def _doc(win) -> str:
    return win._txt.get("1.0", "end")


@pytest.fixture()
def app(root, tmp_path, monkeypatch):
    from tcer.core import llm_prefs, llm_reports, transcript, ui_prefs
    from tcer.gui.app import TcerGui
    monkeypatch.setattr(ui_prefs, "_prefs_path", lambda: tmp_path / "tcer_ui.json")
    monkeypatch.setattr(llm_prefs, "_prefs_path", lambda: tmp_path / "tcer_llm.json")
    monkeypatch.setattr(llm_reports, "_path", lambda: tmp_path / "tcer_llm_reports.json")
    monkeypatch.setattr(TcerGui, "refresh_projects", lambda self: None)
    transcript.clear_cache()
    gui = TcerGui(root)
    yield gui
    for w in list(root.winfo_children()):
        try:
            w.destroy()
        except tk.TclError:
            pass


def _right_click_open(app, root, monkeypatch, reports, which=0):
    """走真实右键回调链：SessionColumn._on_right_click 建菜单 → 取「查看会话记录」命令并触发。"""
    from tcer.gui.widgets import FlatMenu
    cmds: dict = {}
    orig = FlatMenu.add_command

    def rec(self, label="", command=None, **kw):
        cmds[label] = command
        return orig(self, label=label, command=command, **kw)

    monkeypatch.setattr(FlatMenu, "add_command", rec)
    monkeypatch.setattr(FlatMenu, "tk_popup", lambda self, *a: self._close())
    app.session_col.update(reports)
    root.update()

    class Ev:
        x_root = y_root = 0

    r = reports[which]
    app.session_col._on_right_click(Ev(), r, r.meta.session_id)
    assert "查看会话记录" in cmds, list(cmds)
    cmds["查看会话记录"]()
    return app._transcript_win


def test_entry_right_click_to_reader_full_flow(root, app, tmp_path, monkeypatch):
    from tkinter import filedialog

    from tcer.core import transcript as T
    p = _write_session(tmp_path)
    report = _report(p)
    win = _right_click_open(app, root, monkeypatch, [report])

    # 加载中占位（worker 尚未回填）→ 完成
    assert win.alive()
    assert _ready(root, win)
    assert win._state == "ready"
    tr = win._tr
    assert tr.title == "修复登录流程并补测试" and tr.question_count == 29

    # 页头 / 信息行
    assert "修复登录流程并补测试" in win._title_full
    info = " ".join(w.cget("text") for w in win._info.winfo_children() if w.winfo_class() == "Label")
    assert "29 次提问" in info and "次工具调用" in info and "claude-opus-4-8" in info
    assert str(win._resume_btn.cget("state")) == "normal"
    win._copy_resume()
    assert root.clipboard_get() == "claude --resume sess-1"

    # 目录条数 = 数据层目录条目数；错误红点只在第 1 轮
    idx = tr.index()
    assert len(win._outline.entries) == len(idx) == 29
    assert idx[0].has_error and not idx[1].has_error
    assert win._outline.canvas.find_withtag("ei:0")
    assert "对话目录 29" in win._outline._title.cget("text")

    # 文档：提问气泡 + AI 头像行 + 执行过程摘要 + 最终回复（markdown）
    text = _doc(win)
    assert "Claude Code" in text and "执行过程 · 3 步" in text and "修复完成" in text
    assert "**" not in text                       # 粗体标记被渲染掉
    assert "帮我修复登录 bug，并补充测试" in text          # 提问气泡（纯文本，可选中复制）
    assert win._txt.tag_ranges("q_bub")
    assert "Bash(pytest -x tests/test_login.py)" in text   # 失败步骤折叠态也常显

    # 展开本轮：步骤明细出现
    assert "Read(" not in text
    win._toggle_turn(tr.turns[0].key, False, 0)
    root.update()
    text = _doc(win)
    assert "Read(login.py:1-40)" in text and "Update(login.py)" in text

    # 失败步骤默认展开 6 行预览；「显示全部」经懒取拿到完整输出（out line 19 在预览之外）
    bash_step = next(s for s in tr.turns[0].steps if s.kind == "tool" and s.call.kind in ("shell", "search"))
    assert "out line 19" not in _doc(win) and "显示全部" in _doc(win)
    win._show_all_output(bash_step, bash_step.result, 0)
    assert _pump(root, lambda: "out line 19" in _doc(win))

    # 展开 Edit 步骤：diff 行（含 +/-）
    edit_step = next(s for s in tr.turns[0].steps if s.kind == "tool" and s.call.kind == "edit")
    win._toggle_step(edit_step.id, False, 0)
    root.update()
    assert "return user is not None" in _doc(win)
    assert win._txt.tag_ranges("dt_add") and win._txt.tag_ranges("dt_del")

    # 点击动作（mark 机制）：在摘要行上模拟点击 → 折叠
    win._txt.see("1.0")
    root.update()
    names = [n for n in win._txt.mark_names() if n.startswith("a:")]
    assert names                                   # 每个可点击片段一个 mark
    first_act = names[0]
    win._acts[first_act]()                         # 直接触发登记的回调
    root.update()

    # 查找：命中数与数据层一致、高亮、计数、下一个
    win._open_find()
    root.update()
    win._find_var.set("login")
    win._apply_query("login")
    root.update()
    hits = T.find_search_hits(win._turns, "login")
    assert win._search.total == hits.total > 0
    assert win._txt.tag_ranges("find_hit")
    assert win._find_count.cget("text") == f"1/{hits.total}"
    win._find_step(+1)
    assert win._find_count.cget("text") == f"2/{hits.total}"
    win._find_step(-1)
    win._apply_query("绝对不存在的词")
    assert win._find_count.cget("text") == "无结果"
    win._apply_query("")

    # Esc：先关查找条，再关窗口
    win._on_escape()
    assert not win._findbar.winfo_ismapped() and win.alive()

    # 切视图：对话 / 改动 / 全部
    win._set_view("conversation")
    assert _ready(root, win)
    t_conv = _doc(win)
    assert "Bash(" not in t_conv and "修复完成" in t_conv and "执行过程" not in t_conv
    win._set_view("changes")
    assert _ready(root, win)
    t_chg = _doc(win)
    assert "Update(login.py)" in t_chg and "回复 0" not in t_chg
    win._set_view("all")
    assert _ready(root, win)
    assert "执行过程" in _doc(win)

    # 导出 Markdown：内容 = 数据层 transcript_to_markdown
    out = tmp_path / "exported.md"
    monkeypatch.setattr(filedialog, "asksaveasfilename", lambda *a, **k: str(out))
    win._export_md()
    md = out.read_text(encoding="utf-8")
    assert md == T.transcript_to_markdown(tr)
    assert md.startswith("# 修复登录流程并补测试") and "帮我修复登录 bug" in md and "## 修复完成" in md

    # 跳转：目录点击 / 键盘 j k（文档足够长可滚动）
    win._on_outline_you(win._outline.entries[20])
    root.update()
    assert win._top_turn() is not None and win._top_turn() >= 15
    top = win._top_turn()
    win._jump_turn(-1)
    root.update()
    assert win._top_turn() < top
    win._goto_turn(0)
    root.update()
    win._jump_turn(+1)
    root.update()
    assert win._top_turn() == 1

    # 「↓ 最新」
    win._txt.yview_moveto(0)
    root.update()
    win._on_scrolled()
    win._update_latest_btn()
    assert win._latest_btn.winfo_ismapped()
    win._scroll_latest()
    root.update()
    assert win._txt.yview()[1] >= 0.999 and not win._latest_btn.winfo_ismapped()

    # Esc 关窗
    win._on_escape()
    root.update()
    assert not win.alive()


def test_entry_switch_sessions_cancels_previous_and_prev_next(root, app, tmp_path, monkeypatch):
    p1 = _write_session(tmp_path, "sess-1", n_filler=3)
    p2 = _write_session(tmp_path / "other", "sess-2", n_filler=2)
    r1, r2 = _report(p1, "sess-1", "第一个"), _report(p2, "sess-2", "第二个")
    win = _right_click_open(app, root, monkeypatch, [r1, r2], which=0)
    first_cancel = win._cancel
    assert str(win._prev_btn.cget("state")) == "disabled"
    assert _ready(root, win)
    assert str(win._next_btn.cget("state")) == "normal"
    win._step_session(+1)                       # 取消上一次加载 → 加载第二个
    assert first_cancel.is_set() and win._cancel is not first_cancel
    assert _ready(root, win)
    assert win._idx == 1 and win._tr.session_id == "sess-2"
    assert str(win._next_btn.cget("state")) == "disabled"
    assert str(win._prev_btn.cget("state")) == "normal"
    # 快速连点：旧结果不得覆盖新会话
    win._step_session(-1)
    win._step_session(+1)
    assert _ready(root, win)
    assert win._tr.session_id == "sess-2"
    # 再次从会话卡片右键打开：复用同一窗口
    again = _right_click_open(app, root, monkeypatch, [r1, r2], which=0)
    assert again is win and win._idx == 0
    assert _ready(root, win)
    assert win._tr.session_id == "sess-1"


def test_entry_metric_header_button_opens_reader(root, app, tmp_path):
    """看板头部「会话记录」按钮（真实点击链）：会话视角显示并打开该会话，序列为会话
    列表全部；项目汇总视角隐藏。「用户消息」格仍走旧弹窗，不受影响。"""
    from types import SimpleNamespace
    p1 = _write_session(tmp_path, "sess-1", n_filler=2)
    p2 = _write_session(tmp_path / "other", "sess-2", n_filler=2)
    r1, r2 = _report(p1, "sess-1", "第一个"), _report(p2, "sess-2", "第二个")
    agg = _report(p1, "(aggregate)", "项目汇总")
    app._current = SimpleNamespace(reports=[r1, r2], aggregate=agg)
    app.session_col.update([r1, r2])
    btn = app.metric_panel._transcript_btn

    app.metric_panel.update(agg)
    app._rendered_report = agg
    root.update()
    assert not btn.winfo_manager()

    app.metric_panel.update(r2)
    app._rendered_report = r2
    root.update()
    assert btn.winfo_manager() == "pack"
    btn.event_generate("<Button-1>")
    win = app._transcript_win
    assert win.alive() and _ready(root, win)
    assert win._tr.session_id == "sess-2" and len(win._refs) == 2
    assert app.metric_panel._cells["user_msgs"].value is not None

    app.metric_panel.clear()
    assert not btn.winfo_manager()


def test_entry_load_failure_shows_error_not_crash(root, app, tmp_path, monkeypatch):
    p = _write_session(tmp_path, n_filler=1)
    good, bad = _report(p, "sess-1", "好的"), _report(tmp_path / "nope" / "missing.jsonl", "missing", "坏的")
    win = _right_click_open(app, root, monkeypatch, [good, bad], which=1)
    assert _ready(root, win)
    assert win._state == "error"
    assert "加载失败" in _doc(win)
    assert str(win._export_btn.cget("state")) == "disabled"
    assert str(win._resume_btn.cget("state")) == "disabled"
    # 能从错误态切回正常会话
    win._step_session(-1)
    assert _ready(root, win)
    assert win._state == "ready" and win._tr.session_id == "sess-1"
    assert "加载失败" not in _doc(win)


def test_entry_worker_exception_is_reported(root, app, tmp_path, monkeypatch):
    from tcer.core import transcript as T
    p = _write_session(tmp_path, n_filler=1)

    def boom(*a, **k):
        raise RuntimeError("磁盘炸了")

    monkeypatch.setattr(T, "load_transcript", boom)
    win = _right_click_open(app, root, monkeypatch, [_report(p)])
    assert _ready(root, win)
    assert win._state == "error" and "磁盘炸了" in _doc(win)


def test_resume_button_disabled_with_reason_when_no_command(root, app, tmp_path, monkeypatch):
    p = _write_session(tmp_path, n_filler=1)
    win = _right_click_open(app, root, monkeypatch, [_report(p)])
    assert _ready(root, win)
    tr2 = dataclasses.replace(win._tr, resume_command=None, source="workbuddy")
    win._fill_info(tr2)
    assert str(win._resume_btn.cget("state")) == "disabled"
    assert "WorkBuddy" in win._reason_lbl.cget("text") and win._reason_lbl.winfo_manager()


def test_prefs_persist_via_controller_dict(root, app, tmp_path, monkeypatch):
    from tcer.core import ui_prefs
    p = _write_session(tmp_path, n_filler=2)
    win = _right_click_open(app, root, monkeypatch, [_report(p)])
    assert _ready(root, win)
    assert not win._expand_all
    win._set_expand_all(True)
    assert _ready(root, win)
    assert "Read(login.py:1-40)" in _doc(win)           # 全部展开
    assert ui_prefs.load().get("transcript_expand_all") is True
    assert app._ui_prefs.get("transcript_expand_all") is True   # 与控制器共用同一份，不被覆盖
    win._set_expand_all(False)
    assert _ready(root, win)
    assert "Read(login.py:1-40)" not in _doc(win)


def test_plain_inline_strips_markers():
    from tcer.gui import transcript_md as MD
    assert MD.plain_inline("问题 **是** `x` 和 [链接](http://a)") == "问题 是 x 和 链接 (http://a)"
