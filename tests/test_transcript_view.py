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


def test_md_table_expands_capped_columns_into_remaining_budget():
    from tcer.gui import transcript_md as MD
    long_desc = "流中断时的估算占位 usage 被当成真实计费，已排除；接入新的 cost-state 自报成本，实测与价表计价的中位比值为 1.00"
    rows = [
        ["来源", "依据", "主要修复"],
        ["Claude Code", "本机 2.1.307 实测", long_desc],
    ]
    widths, _ = MD.layout_table(rows, 88, gap=3)
    # 窄列保持自然宽，剩余预算优先回补给被 max_col 截断的宽说明列
    assert widths[0] == MD.display_width("Claude Code")
    assert widths[1] == MD.display_width("本机 2.1.307 实测")
    assert widths[2] > 88 * 2 // 5
    assert sum(widths) + 3 * (len(widths) - 1) == 88


def test_atomic_inline_code_and_word_wrapping(root, app, tmp_path, monkeypatch):
    """行内代码芯片（如 `download`）与英文单词整体折行，不在词中硬生生拆成两截。"""
    p = _write_session(tmp_path, n_filler=1)
    win = _right_click_open(app, root, monkeypatch, [_report(p)])
    assert _ready(root, win)
    segs = MD.inline_segments(
        "https://api.granola.ai/v1/get-versions 已于昨天（2026-10-21）全线切入 `7.668.2`（`production`、`beta`、`download` 三位一体对齐）。"
    )
    vlines = win._wrap_md_segments(segs, 420, win._bfont)
    assert len(vlines) >= 2
    code_tokens = [txt for vline in vlines for txt, flags in vline if "md_code" in flags]
    assert any("download" in tok for tok in code_tokens)
    assert not any(tok.strip() in ("downl", "oad") for tok in code_tokens)


def test_md_syntax_highlight_and_path_detection():
    """纯函数校验：围栏代码轻量语法切片保真不丢字，本地路径识别排除版本号与 URL。"""
    py_ln = 'def calc(x: int = 42) -> str:  # comment'
    toks = MD.highlight_code_line(py_ln, "python")
    assert "".join(s for s, _ in toks) == py_ln
    tags = {s: tg for s, tg in toks if tg}
    assert tags["def"] == "syn_kw" and tags["calc"] == "syn_fn"
    assert tags["42"] == "syn_num" and tags["# comment"] == "syn_cmt"

    js_ln = '{"model": "claude-opus-4-8", "tokens": 1200, "cached": true}'
    jtoks = MD.highlight_code_line(js_ln, "json")
    assert "".join(s for s, _ in jtoks) == js_ln
    jtags = {s: tg for s, tg in jtoks if tg}
    assert jtags['"model"'] == "syn_kw" and jtags['"claude-opus-4-8"'] == "syn_str"
    assert jtags["1200"] == "syn_num" and jtags["true"] == "syn_num"

    assert MD.highlight_code_line("+added", "diff") == [("+added", "dt_sign_add")]
    assert MD.highlight_code_line("-removed", "diff") == [("-removed", "dt_sign_del")]

    assert MD.looks_like_path("CLAUDE.md")
    assert MD.looks_like_path("TCER/gui/transcript_view.py:10-25")
    assert MD.looks_like_path(r"C:\vg1\trunk\docs\MapAligner.md")
    assert not MD.looks_like_path("7.668.2")
    assert not MD.looks_like_path("v0.24.0")
    assert not MD.looks_like_path("https://api.granola.ai/v2")
    assert not MD.looks_like_path("--resume")
    assert MD.split_path_loc("login.py:1-40") == ("login.py", "1-40")
    assert MD.split_path_loc(r"C:\a\b.py") == (r"C:\a\b.py", None)


def test_kinsoku_and_outline_word_wrap(root, app, tmp_path, monkeypatch):
    """避头尾（Kinsoku）禁则与目录路径整词折行：句号/顿号不孤立在行首，英文名不在词中硬断。"""
    from tcer.gui import transcript_view as TV
    p = _write_session(tmp_path, n_filler=1)
    win = _right_click_open(app, root, monkeypatch, [_report(p)])
    assert _ready(root, win)

    # 目录 _wrap_lines：路径不在 M|onolith-AI 中间硬劈，句号不落行首
    ol_lines = TV._wrap_lines(
        win._outline._font,
        r"Monolith-AI 分发，只需要C:\vx-tools\Monolith-AI此路径即可，两处由于是同一仓库",
        210, 2,
    )
    assert not ol_lines[-1].startswith("onolith")
    ol_punc = TV._wrap_lines(
        win._outline._font,
        "阅读器界面已完成并验收过，没有新的进展。上一条汇报里的两件事还在等你",
        212, 2,
    )
    assert not ol_punc[-1].startswith("。")

    # 正文 _wrap_md_segments：右标点不落行首，左括号不孤留行尾
    segs = MD.inline_segments(
        "只有 Grok 会填的 13 个数据字段（如 `api_calls`、`ITL`、`ttft`）也删了。"
    )
    for w in range(180, 380, 25):
        vlines = win._wrap_md_segments(segs, w, win._bfont)
        for li, vl in enumerate(vlines):
            row_txt = "".join(s for s, _ in vl)
            if li > 0 and row_txt:
                assert row_txt[0] not in MD.KINSOKU_NO_START, (w, vlines)
            if li < len(vlines) - 1 and row_txt:
                assert row_txt[-1] not in MD.KINSOKU_NO_END, (w, vlines)


def test_polish_and_ux_upgrades_end_to_end(root, app, tmp_path, monkeypatch):
    """端到端验收：
    1) Claude 风格 ⏺ 规整为几何圆点 ●，超长步骤按像素截断且 step_d0 不软折行；
    2) 单会话自动收起 < 1/1 > 导航框，多会话自动显示；
    3) 富表格保留单元格代码胶囊、彩色状态符（✓/⚠/✗），且仅表头下有一条分隔线；
    4) 围栏代码语法着色 + 超 80 行折叠与点击展开；
    5) 单轮「复制」「复制回复」可用，且 ui_chrome 不污染 Ctrl+F 命中；
    6) 本地存在路径着色 md_code_path，目录顶栏显示异常轮徽标并支持一键跳错。"""
    proj_dir = tmp_path / "repo"
    proj_dir.mkdir()
    real_file = proj_dir / "CLAUDE.md"
    real_file.write_text("# Guide\n", encoding="utf-8")

    long_cmd = "git rm -q " + " ".join(f"TCER/core/module_{i}_reader.py" for i in range(25))
    long_py = "\n".join([f"def fn_{i}(x={i}):  # step {i}\n    return 'ok'" for i in range(55)])
    reply_md = (
        "已更新 `CLAUDE.md` 与 [规范文档](https://example.com/spec)：\n\n"
        "| 步骤 | 结果 |\n"
        "|---|---|\n"
        "| 检出 `CLAUDE.md` | ✅ 工作区干净 |\n"
        "| 编译 `monolith_proxy.cpp` | ⚠️ 需重新构建 |\n"
        "| 旧模块检查 | ❌ 已废弃 |\n\n"
        f"```python\n{long_py}\n```\n"
    )
    sess_lines = [
        {"type": "ai-title", "aiTitle": "全量打磨验收", "sessionId": "s-ux"},
        {"type": "user", "uuid": "u0", "timestamp": _ts(0), "sessionId": "s-ux",
         "cwd": str(proj_dir), "message": {"role": "user", "content": "请执行全量升级并输出验证表"}},
        {"type": "assistant", "uuid": "a1", "timestamp": _ts(2), "sessionId": "s-ux",
         "cwd": str(proj_dir),
         "message": {"role": "assistant", "id": "m1", "model": "claude-opus-4-8",
                     "content": [_tu("c1", "Bash", {"command": long_cmd, "description": "清理历史模块并核查残留"})],
                     "usage": {"input_tokens": 50, "output_tokens": 20}}},
        _cr("c1", "Exit code 128\nfatal: pathspec did not match", 4, is_error=True),
        {"type": "assistant", "uuid": "a2", "timestamp": _ts(8), "sessionId": "s-ux",
         "cwd": str(proj_dir),
         "message": {"role": "assistant", "id": "m2", "model": "claude-opus-4-8",
                     "content": [{"type": "text", "text": reply_md}],
                     "usage": {"input_tokens": 80, "output_tokens": 120}}},
        # 第 2 轮无错（验证目录异常徽标精准跳回第 1 轮）
        {"type": "user", "uuid": "u1", "timestamp": _ts(20), "sessionId": "s-ux",
         "cwd": str(proj_dir), "message": {"role": "user", "content": "第二轮确认"}},
        {"type": "assistant", "uuid": "a3", "timestamp": _ts(22), "sessionId": "s-ux",
         "cwd": str(proj_dir),
         "message": {"role": "assistant", "id": "m3", "model": "claude-opus-4-8",
                     "content": [{"type": "text", "text": "全部确认完毕。"}],
                     "usage": {"input_tokens": 20, "output_tokens": 10}}},
    ]
    sp = tmp_path / "s-ux.jsonl"
    sp.write_text("\n".join(json.dumps(x, ensure_ascii=False) for x in sess_lines) + "\n", encoding="utf-8")
    r_single = _report(sp, "s-ux", "全量打磨验收")

    win = _right_click_open(app, root, monkeypatch, [r_single])
    assert _ready(root, win)

    # 1) 单会话隐藏 < 1/1 > 导航框
    assert not win._nav_box.winfo_manager()

    # 2) 步骤行 ⏺ -> ●，且 step_d0 强制单行不断轨
    doc = _doc(win)
    assert "⏺" not in doc and "●" in doc
    assert str(win._txt.tag_cget("step_d0", "wrap")) == "none"

    # 3) 富表格：保留 md_tbl_code 芯片，状态符规整为 ✓ / ⚠ / ✗，且 md_trule 只有 1 条（表头下）
    assert win._txt.tag_ranges("md_tbl_code")
    assert "✓" in doc and "⚠" in doc and "✗" in doc
    assert len(win._txt.tag_ranges("md_trule")) == 2   # 单个连续区间 = (start, end) 2 个端点

    # 4) 本地真实存在的 CLAUDE.md 获得 md_code_path 高亮
    assert win._txt.tag_ranges("md_code_path")

    # 5) 围栏代码：语法高亮生效 + 超 80 行折叠并可点击展开/收起
    assert win._txt.tag_ranges("syn_kw") and win._txt.tag_ranges("syn_fn")
    assert "fn_54" not in doc and "展开全部代码" in doc
    win._toggle_text("cb:t1:2", 0)
    root.update()
    assert "fn_54" in _doc(win) and "收起代码" in _doc(win)
    win._toggle_text("cb:t1:2", 0)
    root.update()
    assert "fn_54" not in _doc(win)

    # 6) 单轮「复制提问」与「复制回复」：点击触发 act_ok 绿标反馈，且搜索「复制」不会把 UI 按钮算进 find_hit
    q_act = next(n for n in win._txt.mark_names()
                 if n.startswith("a:") and "tn:0" in win._txt.tag_names(n)
                 and "q_act" in win._txt.tag_names(n)
                 and win._txt.get(n, f"{n}+2c") == "复制")
    win._acts[q_act]()
    assert root.clipboard_get() == "请执行全量升级并输出验证表"
    assert win._txt.tag_ranges("act_ok")
    ai_act = next(n for n in win._txt.mark_names()
                  if n.startswith("a:") and "tn:0" in win._txt.tag_names(n)
                  and "ai_head" in win._txt.tag_names(n)
                  and win._txt.get(n, f"{n}+4c") == "复制回复")
    win._acts[ai_act]()
    assert root.clipboard_get() == reply_md

    # 工具栏精简：不重复排布展开/收起/导出快捷图标，保留显示选项与查找
    assert not win._expand_btn.winfo_manager()
    assert not win._collapse_btn.winfo_manager()
    assert not win._export_btn.winfo_manager()
    assert win._opt_btn.winfo_manager() == "pack" and win._find_btn.winfo_manager() == "pack"

    win._apply_query("复制")
    root.update()
    assert not win._txt.tag_ranges("find_hit")        # 原文不含「复制」，UI 按钮被 ui_chrome 排除
    win._apply_query("")

    # 7) 对话目录整卡零死角点击与选中锁定 + 异常徽标循环跳错
    from types import SimpleNamespace
    assert "1 轮异常" in win._outline._err_lbl.cget("text")
    y0_1, y1_1 = win._outline._y[1]
    win._outline._on_click(SimpleNamespace(y=int((y0_1 + y1_1) // 2)))
    root.update()
    assert win._outline._cur == 1
    y0_0, _ = win._outline._y[0]
    win._outline._on_click(SimpleNamespace(y=int(y0_0 + 4)))
    root.update()
    assert win._outline._cur == 0
    win._goto_turn(1, flash=False)
    root.update()
    win._outline._step_error()
    root.update()
    assert win._outline._cur == 0
    assert "(1/1)" in win._outline._err_lbl.cget("text")
    assert win._txt.tag_ranges("err_flash") and not win._txt.tag_ranges("flash")
    assert "异常轮次 1/1" in win._status.cget("text")

    # 8) 文本选区 sel_vis 与纯净复制：绝不选中换行符 \n、间距空行与 ui_chrome（「复制回复」等）
    win._txt.tag_add("sel", "1.0", "25.0")
    win._on_selection_changed()
    vis_ranges = win._txt.tag_ranges("sel_vis")
    assert vis_ranges
    for vi in range(0, len(vis_ranges), 2):
        seg = win._txt.get(vis_ranges[vi], vis_ranges[vi + 1])
        assert "\n" not in seg
        assert "复制回复" not in seg
    win._copy_selection()
    copied = root.clipboard_get()
    assert "已更新 CLAUDE.md" in copied and "复制回复" not in copied
    win._txt.tag_remove("sel", "1.0", "end")
    win._on_selection_changed()

    # 8) 切到多会话序列时，< 1/2 > 导航框自动恢复显示
    p2 = _write_session(tmp_path / "other", "sess-2", n_filler=1)
    r2 = _report(p2, "sess-2", "第二个")
    _right_click_open(app, root, monkeypatch, [r_single, r2], which=0)
    assert _ready(root, win)
    assert win._nav_box.winfo_manager() == "pack"


def test_six_product_enhancements_end_to_end(root, app, tmp_path, monkeypatch):
    """端到端验收六项深度增强：
    1) 单轮遥测胶囊（模型 · Token · 成本 · +增 −删）及点击查看输入/输出/缓存明细；
    2) 对话目录卡片右侧 +增 −删 徽标与热点 ◆ / 上下文切换 ◈ 标记；
    3) 「改动」视图顶部交互式「涉及文件汇总」卡片（统计返工次数、点击跳到并展开首个 Diff 步骤）；
    4) 超长思考与超长工具参数按需懒加载展开全文 / 收起；
    5) Diff 块「复制新代码」「复制 Diff」及选区剔除行号与 +/- 符号（仅复制纯净代码）；
    6) 滚动条左侧多色语义概览标尺（_MarkerRail：绿/紫/青/蓝/黄/红，普通单步工具报错不标红）。"""
    from types import SimpleNamespace
    from tcer.core import transcript as T

    proj_dir = tmp_path / "proj"
    proj_dir.mkdir()
    long_think = "\n".join(f"Thinking line {i}: evaluating invariant #{i}" for i in range(95))
    big_payload = "x" * 2600

    sess_lines = [
        {"type": "ai-title", "aiTitle": "六项增强验收", "sessionId": "s-six"},
        # 第 1 轮：含超长思考、超长 MCP 参数、同一文件 3 次 Edit（返工）+ 普通单步工具报错（不应触发标尺 abort 红标）
        {"type": "user", "uuid": "u0", "timestamp": _ts(0), "sessionId": "s-six",
         "cwd": str(proj_dir), "message": {"role": "user", "content": "重构 auth.py 并校验"}},
        {"type": "assistant", "uuid": "a1", "timestamp": _ts(2), "sessionId": "s-six",
         "cwd": str(proj_dir),
         "message": {"role": "assistant", "id": "m1", "model": "claude-opus-4-8",
                     "content": [
                         {"type": "thinking", "thinking": long_think},
                         _tu("c_mcp", "mcp__probe__query", {"payload": big_payload}),
                         _tu("c_e1", "Edit", {"file_path": str(proj_dir / "auth.py"),
                                              "old_string": "a = 1\n", "new_string": "a = 2\nb = 3\n"}),
                         _tu("c_e2", "Edit", {"file_path": str(proj_dir / "auth.py"),
                                              "old_string": "b = 3\n", "new_string": "b = 4\n"}),
                         _tu("c_e3", "Edit", {"file_path": str(proj_dir / "auth.py"),
                                              "old_string": "b = 4\n", "new_string": "b = 5\nc = 6\n"}),
                         _tu("c_err", "Bash", {"command": "pytest -q"}),
                     ],
                     "usage": {"input_tokens": 120_000, "output_tokens": 18_000,
                               "cache_read_input_tokens": 300_000}}},
        _cr("c_mcp", "ok", 3),
        _cr("c_e1", "updated", 4),
        _cr("c_e2", "updated", 5),
        _cr("c_e3", "updated", 6),
        _cr("c_err", "1 failed", 7, is_error=True),
        {"type": "assistant", "uuid": "a2", "timestamp": _ts(9), "sessionId": "s-six",
         "cwd": str(proj_dir),
         "message": {"role": "assistant", "id": "m2", "model": "claude-opus-4-8",
                     "content": [{"type": "text", "text": "已完成三次迭代重构。"}],
                     "usage": {"input_tokens": 10_000, "output_tokens": 2_000}}},
        # 第 2 轮：切换模型为 claude-sonnet-4-8（触发上下文演进 context 标记），轻量问答
        {"type": "user", "uuid": "u1", "timestamp": _ts(20), "sessionId": "s-six",
         "cwd": str(proj_dir), "message": {"role": "user", "content": "确认状态"}},
        {"type": "assistant", "uuid": "a3", "timestamp": _ts(22), "sessionId": "s-six",
         "cwd": str(proj_dir),
         "message": {"role": "assistant", "id": "m3", "model": "claude-sonnet-4-8",
                     "content": [{"type": "text", "text": "状态正常。"}],
                     "usage": {"input_tokens": 1_500, "output_tokens": 300}}},
    ]
    sp = tmp_path / "s-six.jsonl"
    sp.write_text("\n".join(json.dumps(x, ensure_ascii=False) for x in sess_lines) + "\n", encoding="utf-8")
    win = _right_click_open(app, root, monkeypatch, [_report(sp, "s-six", "六项增强验收")])
    assert _ready(root, win)

    # 1) 单轮遥测数据与胶囊展示：第 1 轮为热点轮次（▲），带 +5 −3 代码增删徽标，点击弹出输入/输出/缓存明细
    tels = win._telemetries
    assert len(tels) == 2
    assert tels[0].is_hotspot and tels[0].has_code_change
    assert tels[0].added == 5 and tels[0].removed == 3
    assert tels[1].has_context_shift and not tels[1].is_hotspot
    doc = _doc(win)
    assert "▲" in doc and "tok" in doc and "+5" in doc and "−3" in doc
    assert win._txt.tag_ranges("ai_loc_add") and win._txt.tag_ranges("ai_loc_del")
    win._show_turn_telemetry(tels[0])
    assert "输入" in win._status.cget("text") and "缓存读" in win._status.cget("text")

    # 2) 对话目录卡片：右侧绘制 +5 / −3 增删徽标与 ◆ 热点 / ◈ 上下文切换标记
    ol_texts = [
        win._outline.canvas.itemcget(it, "text")
        for it in win._outline.canvas.find_all()
        if win._outline.canvas.type(it) == "text"
    ]
    assert "+5" in ol_texts and "−3" in ol_texts
    assert "◆" in ol_texts and "◈" in ol_texts

    # 3) 多色语义概览标尺（_MarkerRail）：含 change(绿)、hotspot(紫)、context(青)，且第 1 轮虽有单步工具报错但不标 abort(红)
    ticks_by_turn = {turn_idx: tags for _f0, _f1, turn_idx, tags, _tip in win._marker_rail._ticks}
    assert "change" in ticks_by_turn[0] and "hotspot" in ticks_by_turn[0]
    assert "abort" not in ticks_by_turn[0]
    assert "context" in ticks_by_turn[1]
    # 查找命中时动态叠加 search(金黄) 刻度
    win._apply_query("状态正常")
    root.update()
    ticks_search = {turn_idx: tags for _f0, _f1, turn_idx, tags, _tip in win._marker_rail._ticks}
    assert "search" in ticks_search[1]
    win._apply_query("")
    root.update()
    # 点击标尺跳转到对应轮次
    win._marker_rail._on_click(SimpleNamespace(y=max(1, win._marker_rail.canvas.winfo_height() - 2)))
    root.update()

    # 4) 超长思考与超长工具参数按需展开全文
    win._toggle_turn(win._turns[0].key, False, 0)
    root.update()
    think_step = next(s for s in win._turns[0].steps if s.kind == "thinking")
    win._toggle_step(think_step.id, False, 0)
    root.update()
    assert "Thinking line 85" not in _doc(win) and "展开完整思考" in _doc(win)
    win._show_all_thinking(think_step, think_step.block, 0)
    assert _pump(root, lambda: "Thinking line 85" in _doc(win) and "收起思考" in _doc(win))

    mcp_step = next(s for s in win._turns[0].steps if s.kind == "tool" and s.call.id == "c_mcp")
    win._toggle_step(mcp_step.id, False, 0)
    root.update()
    assert "展开完整参数" in _doc(win)
    win._show_all_input(mcp_step.call, 0)
    assert _pump(root, lambda: ("x" * 2400) in _doc(win) and "收起参数" in _doc(win))

    # 5) Diff 块「复制新代码」「复制 Diff」及纯净代码选区（自动剥离行号与 +/-）
    first_edit = next(s for s in win._turns[0].steps if s.kind == "tool" and s.call.id == "c_e1")
    win._toggle_step(first_edit.id, False, 0)
    root.update()
    assert "复制新代码" in _doc(win) and "复制 Diff" in _doc(win)
    copy_new_act = next(
        n for n in win._txt.mark_names()
        if n.startswith("a:") and "dt_file" in win._txt.tag_names(n)
        and win._txt.get(n, f"{n}+5c") == "复制新代码"
    )
    win._acts[copy_new_act]()
    assert root.clipboard_get() == "a = 2\nb = 3"
    # 选中 Diff 新增行时，sel_vis 与 _clean_selected_text 自动剥离行号与 '+' 号
    add_rng = win._txt.tag_ranges("dt_add")
    assert add_rng
    win._txt.tag_remove("sel", "1.0", "end")
    win._txt.tag_add("sel", str(add_rng[0]), str(add_rng[1]))
    win._on_selection_changed()
    cleaned_diff = win._clean_selected_text()
    assert "a = 2" in cleaned_diff and "+" not in cleaned_diff
    win._txt.tag_remove("sel", "1.0", "end")
    win._on_selection_changed()

    # 6) 「改动」视图顶部「涉及文件汇总」卡片：显示 auth.py、+5 −3、返工 3 次，点击跳到并展开首个改动步骤
    win._set_view(T.VIEW_CHANGES)
    assert _pump(root, lambda: win._render_done)
    chg_doc = _doc(win)
    assert "涉及文件汇总 · 1 个文件" in chg_doc
    assert "auth.py" in chg_doc and "返工 3 次" in chg_doc
    assert win._txt.tag_ranges("chg_head") and win._txt.tag_ranges("chg_row")
    win._step_ov.clear()
    win._jump_to_change_step(0, first_edit.id)
    root.update()
    assert win._step_ov.get(first_edit.id) is True
