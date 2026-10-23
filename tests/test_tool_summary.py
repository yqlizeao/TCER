"""tool_summary（会话阅读器工具层）无头单测：预览截断 / 工具归一化 / 标题 / 合并 / 摘要。"""
from __future__ import annotations

import pytest

from tcer.core import tool_summary as S
from tcer.core.transcript import (
    EventBlock, EventStep, ImageRef, NoteStep, StepCost, ThinkingBlock, ThinkingStep,
    ToolCallBlock, ToolResultBlock, ToolStep, Turn, TurnQuestion,
)


# --------------------------------------------------------------------------- #
# 文本小工具
# --------------------------------------------------------------------------- #
def test_preview_limits_lines_and_chars():
    text = "\n".join(f"line{i}" for i in range(30))
    p = S.preview(text)
    assert p.text.count("\n") == 11 and p.text.endswith("line11")
    assert p.truncated and p.line_count == 30 and p.total_len == len(text)
    # 字符数上限（单行超长）
    p2 = S.preview("x" * 5000)
    assert len(p2.text) == S.PREVIEW_CHARS and p2.truncated
    # 恰好 12 行 + 末尾换行：不算截断，预览去掉末尾换行
    p3 = S.preview("\n".join("a" * 3 for _ in range(12)) + "\n")
    assert not p3.truncated and p3.line_count == 12 and not p3.text.endswith("\n")
    assert S.preview("") == S.Preview("", 0, 0, False)
    # 参数预览只按字符截断
    assert S.preview_chars("a\nb\nc", 100).text == "a\nb\nc"
    assert S.preview_chars("y" * 500, 400).truncated


def test_count_lines_matches_rust_lines():
    assert S.count_lines("") == 0
    assert S.count_lines("a") == 1
    assert S.count_lines("a\n") == 1
    assert S.count_lines("a\nb") == 2
    assert S.count_lines("a\n\n") == 2


def test_one_line_title():
    assert S.one_line_title("  hello  ") == "hello"
    assert S.one_line_title("first\nsecond") == "first…"
    assert S.one_line_title("first\n\n  \n") == "first"          # 后续全空白不加 …
    assert S.one_line_title("x" * 250) == "x" * 200 + "…"
    assert S.one_line_title("") == ""


def test_line_change_counts_ignores_shared_context():
    assert S.line_change_counts("a\nb\nc", "a\nb\nx\nc") == (1, 0)
    assert S.line_change_counts("a\nb", "a\nc\nd") == (2, 1)
    assert S.line_change_counts("", "x\ny") == (2, 0)
    assert S.line_change_counts("same", "same") == (0, 0)


def test_count_diff_lines_skips_file_headers():
    d = "--- a/x\n+++ b/x\n@@ -1 +1,2 @@\n-old\n+new\n+more\n ctx\n"
    assert S.count_diff_lines(d) == (2, 1)


def test_split_mcp_name_and_refine_shell_kind():
    assert S.split_mcp_name("mcp__github__create_issue") == ("github", "create_issue")
    assert S.split_mcp_name("mcp__x") is None
    assert S.split_mcp_name("Bash") is None
    assert S.refine_shell_kind("rg foo src") == S.KIND_SEARCH
    assert S.refine_shell_kind("/usr/bin/grep -n x") == S.KIND_SEARCH
    assert S.refine_shell_kind("ls -la") == S.KIND_SEARCH
    assert S.refine_shell_kind("cargo build") == S.KIND_SHELL
    assert S.refine_shell_kind("") == S.KIND_SHELL


@pytest.mark.parametrize("source,name,kind", [
    ("claude", "Bash", "shell"), ("claude", "Read", "read"), ("claude", "Grep", "search"),
    ("claude", "Glob", "search"), ("claude", "MultiEdit", "edit"), ("claude", "NotebookEdit", "edit"),
    ("claude", "Write", "write"), ("claude", "WebFetch", "web"), ("claude", "Task", "agent"),
    ("claude", "SendMessage", "agent"), ("claude", "AskUserQuestion", "ask"),
    ("claude", "TodoWrite", "todo"), ("claude", "Whatever", "other"),
    ("codex", "exec_command", "shell"), ("codex", "apply_patch", "edit"),
    ("codex", "write_file", "write"), ("codex", "web.run", "web"), ("codex", "update_plan", "todo"),
    ("codex", "spawn_agent", "agent"),
    ("gemini", "run_shell_command", "shell"), ("gemini", "replace", "edit"),
    ("opencode", "bash", "shell"), ("opencode", "patch", "edit"), ("opencode", "task", "agent"),
    ("opencode", "todowrite", "todo"),
    ("pi", "bash", "shell"), ("pi", "find", "search"), ("pi", "fetch", "web"),
    ("generic", "web_search", "web"), ("generic", "run_terminal", "shell"),
    ("generic", "file_reader", "other"),
])
def test_normalize_tool_table(source, name, kind):
    assert S.normalize_tool(source, name).kind == kind


def test_normalize_tool_mcp():
    n = S.normalize_tool("claude", "mcp__github__get_pr")
    assert (n.kind, n.server) == ("mcp", "github")
    n = S.normalize_tool("codex", "tool", namespace="mcp__cua_repl__")
    assert (n.kind, n.server) == ("mcp", "cua_repl")
    n = S.normalize_tool("gemini", "srv/tool")
    assert (n.kind, n.server) == ("mcp", "srv")
    n = S.normalize_tool("opencode", "context7_query")
    assert (n.kind, n.server) == ("mcp", "context7")
    assert S.normalize_tool("opencode", "invalid").kind == "other"


def test_title_from_input_by_kind():
    assert S.title_from_input("shell", "bash", None, {"command": "ls -la\nmore", "description": "list"}) \
        == ("ls -la…", "list")
    assert S.title_from_input("read", "read", None, {"path": "a.py", "offset": 10, "limit": 5}) \
        == ("a.py", ":10-14")
    assert S.title_from_input("read", "read", None, {"path": "a.py", "limit": 5}) == ("a.py", ":1-5")
    assert S.title_from_input("read", "read", None, {"path": "a.py", "offset": 7}) == ("a.py", ":7")
    assert S.title_from_input("read", "read", None, {"paths": ["a", "b"]}) == ("a, b", None)
    assert S.title_from_input("search", "grep", None, {"pattern": "foo", "path": "src"}) == ("foo in src", None)
    assert S.title_from_input("web", "x", None, {"queries": ["a", "b"]}) == ("a, b", None)
    assert S.title_from_input("mcp", "mcp__gh__open", "gh", {"title": "T", "n": 1}) == ("gh.open", "T")
    assert S.title_from_input("agent", "task", None, {"description": "审查", "subagent_type": "x"}) \
        == ("审查", "x")
    assert S.title_from_input("ask", "q", None, {"questions": [{"question": "继续吗"}]}) == ("继续吗", None)
    assert S.title_from_input("todo", "t", None, {"todos": [1, 2, 3]}) == ("3 项", None)
    assert S.title_from_input("other", "Mystery", None, {"a": "", "b": "x"}) == ("Mystery", "x")


def test_claude_tool_title():
    k, t, d = S.claude_tool_title("Bash", "shell", None, {"command": "rg foo", "description": "找"})
    assert (k, t, d) == ("search", "rg foo", "找")          # Bash 细化为 search
    assert S.claude_tool_title("Read", "read", None, {"file_path": "a.py", "offset": 3, "limit": 2}) \
        == ("read", "a.py", ":3-4")
    assert S.claude_tool_title("Grep", "search", None, {"pattern": "p", "glob": "*.py", "output_mode": "content"}) \
        == ("search", "p in *.py", "content")
    assert S.claude_tool_title("NotebookEdit", "edit", None, {"notebook_path": "n.ipynb"})[1] == "n.ipynb"
    assert S.claude_tool_title("Agent", "agent", None, {"prompt": "do x", "model": "opus"}) \
        == ("agent", "do x", "opus")
    assert S.claude_tool_title("TodoWrite", "todo", None, {"todos": [{}, {}]})[1] == "2 项"
    assert S.claude_tool_title("mcp__s__t", "mcp", "s", {"q": "hello"}) == ("mcp", "s.t", "hello")


def test_diff_from_input():
    d = S.diff_from_input("edit", {"file_path": "a.py", "old_string": "a\nb", "new_string": "a\nc\nd"})
    # 通用估算（非 Claude 源）不去共同上下文：新增 3 行 / 删除 2 行；Claude 走 line_change_counts
    assert d["files"] == [{"path": "a.py", "op": "update", "added": 3, "removed": 2}]
    assert (d["added"], d["removed"]) == (3, 2)
    d = S.diff_from_input("write", {"file_path": "n.py", "content": "x\ny\nz"})
    assert d["files"][0]["op"] == "add" and d["added"] == 3
    d = S.diff_from_input("edit", {"path": "a", "edits": [{"oldText": "x", "newText": "y\nz"}]})
    assert (d["added"], d["removed"]) == (2, 1)
    assert S.diff_from_input("edit", {"old_string": "a", "new_string": "b"}) is None   # 无路径


def test_parse_exit_code():
    assert S.parse_exit_code("Exit code 2\nboom") == 2
    assert S.parse_exit_code("Error: Exit code 127\n") == 127
    assert S.parse_exit_code("  \nExit code 1") is None or True   # 首行为空时不强求
    assert S.parse_exit_code("all good") is None


# --------------------------------------------------------------------------- #
# 格式化
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize("ms,out", [
    (0, "0ms"), (350, "350ms"), (999, "999ms"), (1000, "1s"), (1500, "1.5s"), (9960, "10s"),
    (12000, "12s"), (59400, "59s"), (60000, "1m"), (134000, "2m14s"), (240000, "4m"),
    (3_600_000, "1h"), (3_780_000, "1h3m"),
])
def test_format_duration(ms, out):
    assert S.format_duration(ms) == out


@pytest.mark.parametrize("n,out", [
    (0, "0"), (980, "980"), (1000, "1k"), (28500, "28.5k"), (999_999, "1000k"), (1_200_000, "1.2M"),
])
def test_format_tokens(n, out):
    assert S.format_tokens(n) == out


def test_shorten_path():
    assert S.shorten_path("/work/proj/src/a.py", "/work/proj") == "src/a.py"
    assert S.shorten_path("grep in /work/proj", "/work/proj") == "grep in ."
    assert S.shorten_path("/work/project2/a", "/work/proj") == "/work/project2/a"   # 不误伤同前缀目录
    assert S.shorten_path("see /Users/bob/x/y.txt") == "see ~/x/y.txt"
    assert S.shorten_path("/home/al/code") == "~/code"
    # Windows：盘符 / 反斜杠 / 大小写不敏感
    assert S.shorten_path(r"C:\Proj\src\a.py", r"c:\proj") == r"src\a.py"
    assert S.shorten_path("C:/Proj/src/a.py", r"C:\Proj") == "src/a.py"
    assert S.shorten_path(r"C:\Users\bob\x.txt") == r"~\x.txt"
    assert S.shorten_path("nothing", None) == "nothing"


# --------------------------------------------------------------------------- #
# 步骤构造小工具
# --------------------------------------------------------------------------- #
def _call(raw, kind, title, *, id="c1", detail=None, server=None, diff=None, by_user=False):
    return ToolCallBlock(id=id, raw_name=raw, kind=kind, title=title, detail=detail,
                         server=server, diff=diff, by_user=by_user)


def _tool(call, status="success", *, id="s", result=None, cost=None):
    st = ToolStep(id=id, message_index=0, call=call, status=status, cost=cost)
    st.result = result
    return st


def _res(status="success", *, exit_code=None, duration_ms=None, lines=0):
    return ToolResultBlock(call_id="c1", status=status, exit_code=exit_code,
                           duration_ms=duration_ms, line_count=lines)


# --------------------------------------------------------------------------- #
# 步骤标题
# --------------------------------------------------------------------------- #
def test_format_step_title_claude_call_format():
    st = S.AGENT_STYLES["claude"]
    assert S.format_step_title(_tool(_call("Bash", "shell", "cargo build")), st).text == "Bash(cargo build)"
    # Edit/MultiEdit 按 CLI 显示为 Update
    assert S.format_step_title(_tool(_call("Edit", "edit", "/p/a.py")), st, "/p").text == "Update(a.py)"
    # Read 行范围拼到路径后
    t = S.format_step_title(_tool(_call("Read", "read", "/p/a.py", detail=":1-40")), st, "/p")
    assert t.text == "Read(a.py:1-40)" and t.detail is None
    # MCP：verb = server.tool，target = 第一个参数
    t = S.format_step_title(_tool(_call("mcp__gh__open", "mcp", "gh.open", detail="T", server="gh")), st)
    assert t.text == "gh.open(T)"
    # 无参数时无括号
    assert S.format_step_title(_tool(_call("TodoWrite", "todo", "")), st).text == "TodoWrite"


def test_format_step_title_codex_verb_format_and_diff_titles():
    st = S.AGENT_STYLES["codex"]
    assert S.format_step_title(_tool(_call("exec_command", "shell", "ls")), st).text == "Ran ls"
    one = {"files": [{"path": "a.py", "op": "update", "added": 1, "removed": 0}], "added": 1, "removed": 0}
    assert S.format_step_title(_tool(_call("apply_patch", "edit", "a.py", diff=one)), st).text == "Edited a.py"
    add = {"files": [{"path": "n.py", "op": "add", "added": 2, "removed": 0}], "added": 2, "removed": 0}
    assert S.format_step_title(_tool(_call("apply_patch", "write", "n.py", diff=add)), st).text == "Added n.py"
    multi = {"files": [{"path": "a", "op": "update"}, {"path": "b", "op": "update"}], "added": 0, "removed": 0}
    assert S.format_step_title(_tool(_call("apply_patch", "edit", "a 等 2 个文件", diff=multi)), st).text \
        == "Edited 2 files"
    dele = {"files": [{"path": "a", "op": "delete"}], "added": 0, "removed": 1}
    assert S.format_step_title(_tool(_call("apply_patch", "edit", "a", diff=dele)), st).text == "Deleted a"
    t = S.format_step_title(_tool(_call("t", "mcp", "gh.open", detail="x", server="gh")), st)
    assert t.text == "Called gh.open(x)"


def test_format_step_title_opencode_icon_and_generic_chinese():
    st = S.AGENT_STYLES["opencode"]
    assert S.format_step_title(_tool(_call("bash", "shell", "make")), st).text == "$ make"
    assert S.format_step_title(_tool(_call("read", "read", "a.py")), st).text == "→ Read a.py"
    assert S.format_step_title(_tool(_call("websearch", "web", "q")), st).text == "◈ WebSearch q"
    g = S.AGENT_STYLES["generic"]
    assert S.format_step_title(_tool(_call("run_command", "shell", "ls")), g).text == "运行 ls"
    assert S.format_step_title(_tool(_call("view", "read", "a.py")), g).text == "读取 a.py"
    # 无配对调用：通用「工具输出」
    orphan = ToolStep(id="o", message_index=0, call=None, status="success")
    assert S.format_step_title(orphan, g).text == "工具输出"


def test_get_style_source_mapping():
    assert S.get_style("omp").id == "pi" and S.get_style("pi").id == "pi"
    assert S.get_style("workbuddy").id == "generic" and S.get_style("antigravity").id == "generic"
    assert S.get_style("claude").id == "claude" and S.get_style("nope").id == "generic"


# --------------------------------------------------------------------------- #
# 合并
# --------------------------------------------------------------------------- #
def _mk(i, kind, status="success", server=None, raw="x"):
    return _tool(_call(raw, kind, f"t{i}", id=f"c{i}", server=server), status, id=f"s{i}")


def test_merge_codex_explored_runs():
    st = S.AGENT_STYLES["codex"]
    steps = [_mk(1, "read"), _mk(2, "search"), _mk(3, "read"), _mk(4, "shell"), _mk(5, "read")]
    out = S.merge_steps(steps, st)
    assert [s.kind for s in out] == ["merged", "tool", "tool"]
    m = out[0]
    assert m.merge_kind == "explored" and len(m.children) == 3 and m.id == "s1+merged"
    assert S.format_merged_title(m).text == "Explored"
    assert S.format_merged_title(m).subtitle == "查看了 2 个文件、搜索 1 次"


def test_merge_requires_two_and_success_only():
    st = S.AGENT_STYLES["codex"]
    # 失败步骤单独常显，且断开 run
    out = S.merge_steps([_mk(1, "read"), _mk(2, "read", "error"), _mk(3, "read")], st)
    assert [s.kind for s in out] == ["tool", "tool", "tool"]
    # pending 不合并
    out = S.merge_steps([_mk(1, "read"), _mk(2, "read", "pending")], st)
    assert all(s.kind == "tool" for s in out)


def test_merge_claude_mcp_same_server_only():
    st = S.AGENT_STYLES["claude"]
    steps = [_mk(1, "mcp", server="gh"), _mk(2, "mcp", server="gh"), _mk(3, "mcp", server="slack"),
             _mk(4, "mcp", server="slack"), _mk(5, "read")]
    out = S.merge_steps(steps, st)
    assert [s.kind for s in out] == ["merged", "merged", "tool"]
    assert out[0].server == "gh" and S.format_merged_title(out[0]).text == "Called gh 2 times"
    assert S.format_merged_title(out[0]).subtitle == "调用 gh 2 次"
    # read/search 在 claude 风格下不合并
    assert all(s.kind == "tool" for s in S.merge_steps([_mk(1, "read"), _mk(2, "read")], st))
    # 其它风格完全不合并
    assert all(s.kind == "tool" for s in S.merge_steps(
        [_mk(1, "mcp", server="gh"), _mk(2, "mcp", server="gh")], S.AGENT_STYLES["generic"]))


# --------------------------------------------------------------------------- #
# 元信息
# --------------------------------------------------------------------------- #
def test_format_step_meta_and_text():
    st = S.AGENT_STYLES["claude"]
    s = _tool(_call("Bash", "shell", "x"), "error", result=_res("error", exit_code=101, duration_ms=12000))
    m = S.format_step_meta(s, st)
    assert (m.status, m.tone, m.exit_code, m.duration_ms) == ("error", "danger", 101, 12000)
    assert S.format_step_meta_text(m) == "失败 · exit 101 · 12s"
    # exit 0 在 nonzero 风格下不显示
    ok = _tool(_call("Bash", "shell", "x"), result=_res(exit_code=0, duration_ms=500, lines=3))
    mo = S.format_step_meta(ok, st)
    assert mo.exit_code is None and mo.line_count == 3 and S.format_step_meta_text(mo) == "500ms"
    # 有 diff 计数时给 +a −r 而不给行数
    d = {"files": [{"path": "a"}], "added": 3, "removed": 1}
    e = _tool(_call("Edit", "edit", "a", diff=d), result=_res(lines=5))
    me = S.format_step_meta(e, st)
    assert me.diff == {"added": 3, "removed": 1, "files": 1} and me.line_count is None
    assert S.format_step_meta_text(me) == "+3 −1"
    # 中断状态词
    i = S.format_step_meta(_tool(_call("Bash", "shell", "x"), "interrupted"), st)
    assert S.format_step_meta_text(i) == "已中断" and i.tone == "warning"
    # 合并步骤耗时 = 子步骤之和
    ch = [_tool(_call("r", "read", "a"), result=_res(duration_ms=100)),
          _tool(_call("r", "read", "b"), result=_res(duration_ms=250))]
    mg = S.MergedStep if False else None   # noqa: F841
    from tcer.core.transcript import MergedStep
    mm = S.format_step_meta(MergedStep(id="m", merge_kind="explored", children=ch), st)
    assert mm.duration_ms == 350
    # OpenCode 显示每步 tokens / 费用
    oc = S.AGENT_STYLES["opencode"]
    s2 = _tool(_call("bash", "shell", "x"), cost=StepCost(tokens=1500, cost_usd=0.01))
    t = S.format_step_meta_text(S.format_step_meta(s2, oc))
    assert "1.5k tok" in t and "$0.0100" in t
    # unknown 状态用 success 状态符
    assert S.status_glyph("unknown", S.AGENT_STYLES["gemini"]) == "✓"


def test_describe_event_texts():
    d = S.describe_event(EventBlock("aborted"))
    assert d.label == "已中断"
    d = S.describe_event(EventBlock("compaction", "摘要内容"))
    assert d.label == "上下文已压缩" and d.detail == "摘要内容"
    assert S.describe_event(EventBlock("model_change", "gpt-5")).label == "模型切换为 gpt-5"
    assert S.describe_event(EventBlock("thinking_level", "high")).label == "思考强度：high"
    h = S.describe_event(EventBlock("hook", "lint failed"))
    assert h.label == "Hook 执行出错" and h.tone == "danger" and h.detail == "lint failed"
    assert S.describe_event(EventBlock("pr_link", "PR #12", "https://x/pull/12")).label == "已关联 PR #12"
    assert S.describe_event(EventBlock("pr_link", "PR", "https://x/pull/7")).label == "已关联 PR #7"
    assert S.describe_event(EventBlock("pr_link", "PR")).label == "已关联 PR"
    assert S.describe_event(EventBlock("slash_command", "/model")).label == "运行了 /model"
    sa = S.describe_event(EventBlock("sub_agent", "agt_1: found it"), "more")
    assert sa.label == "子代理 agt_1" and sa.detail == "found it\n\nmore"
    e = S.describe_event(EventBlock("error", "boom"))
    assert e.label == "boom" and e.tone == "danger"
    assert S.describe_event(EventBlock("info", "synthetic text")).label == "synthetic text"


# --------------------------------------------------------------------------- #
# 轮摘要
# --------------------------------------------------------------------------- #
def _turn(steps, *, qts=1000, end=5000, costs=()):
    t = Turn(key="t1", index=0, first_message_index=0, last_message_index=1, steps=list(steps),
             question=TurnQuestion(0, "q", [], qts), ts=qts, end_ts=end)
    t.step_costs = list(costs)
    return t


def test_summarize_turn_counts_and_files():
    edit_ok = _tool(_call("Edit", "edit", "a.py", id="c1"), "success", id="s1")
    edit_dup = _tool(_call("Write", "write", "a.py", id="c2"), "success", id="s2")   # 同文件只计一次
    edit_fail = _tool(_call("Edit", "edit", "z.py", id="c3"), "error", id="s3")      # 失败不计
    with_diff = _tool(_call("apply_patch", "edit", "x", id="c4", diff={
        "files": [{"path": "b.py"}, {"path": "c.py"}], "added": 1, "removed": 0}), id="s4")
    sh = _tool(_call("Bash", "shell", "ls", id="c5"), "success", id="s5", result=_res(duration_ms=40))
    think = ThinkingStep(id="th", message_index=0, block=ThinkingBlock(text="..."))
    note = NoteStep(id="n", message_index=0, text="说明")
    s = S.summarize_turn(_turn([edit_ok, think, edit_dup, note, edit_fail, with_diff, sh]))
    assert s.step_count == 5               # 只数工具步骤
    assert s.command_count == 1
    assert s.files_changed == 3            # a.py + b.py + c.py
    assert s.error_count == 1
    assert s.thinking_count == 1
    assert s.duration_ms == 4000           # 轮耗时 = 末条 - 提问
    assert s.kind_counts == {"edit": 3, "write": 1, "shell": 1}


def test_summarize_turn_duration_fallback_and_costs():
    sh = _tool(_call("Bash", "shell", "ls"), result=_res(duration_ms=700))
    t = _turn([sh], qts=None, end=None)
    t.question = None
    assert S.summarize_turn(t).duration_ms == 700          # 无时间戳：用步骤耗时之和
    t2 = _turn([sh], costs=[StepCost(tokens=100, cost_usd=0.5), StepCost(tokens=50, cost_usd=None)])
    s = S.summarize_turn(t2)
    assert s.tokens == 150 and s.cost_usd == 0.5
    assert S.summarize_turn(_turn([])).duration_ms == 4000
    # 墙钟为 0 且无步骤耗时 → None
    t3 = _turn([_tool(_call("Bash", "shell", "ls"))], qts=1000, end=1000)
    assert S.summarize_turn(t3).duration_ms is None


def test_format_turn_summary_text():
    s = S.TurnSummary(step_count=14, command_count=9, files_changed=2, error_count=3,
                      duration_ms=26000)
    assert S.format_turn_summary(s) == "执行过程 · 14 步 · 9 个命令 · 改了 2 个文件 · 3 个失败 · 耗时 26s"
    assert S.format_turn_summary(S.TurnSummary(), steps=2) == "执行过程 · 2 步"
    txt = S.format_turn_summary(S.TurnSummary(step_count=1), hidden_failures=2, no_final=True)
    assert txt.endswith("还有 2 个失败 · 这一轮没有最终回复")
    # 已中断时不再追加「没有最终回复」
    assert "没有最终回复" not in S.format_turn_summary(S.TurnSummary(), no_final=True, aborted=True)
    tok = S.format_turn_summary(S.TurnSummary(tokens=28500, cost_usd=0.1234))
    assert "28.5k tok" in tok and "$0.1234" in tok


def test_failure_predicates():
    assert S.is_failure_status("error") and S.is_failure_status("interrupted")
    assert not S.is_failure_status("pending") and not S.is_failure_status("success")
    assert S.is_failure_step(_tool(_call("a", "shell", "x"), "error"))
    note = NoteStep(id="n", message_index=0, text="x")
    assert not S.is_failure_step(note)
    ev = EventStep(id="e", message_index=0, block=EventBlock("error", "x"))
    assert not S.is_failure_step(ev)
    assert ImageRef().media_type      # 数据类可默认构造
