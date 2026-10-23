"""「用户提问」口径 SSOT（core/user_input）测试。规则来自 cc-switch + TCER IDE 补充。"""
from __future__ import annotations

import json

import pytest

from tcer.core import reader, user_input as ui


def _rec(content, **kw):
    return {"type": "user", "message": {"role": "user", "content": content}, **kw}


def _blocks(*texts):
    return [{"type": "text", "text": t} for t in texts]


@pytest.mark.parametrize("content,kind,text", [
    ("请帮我修 bug", ui.QUESTION, "请帮我修 bug"),
    # IDE 上下文是独立块：剥掉后提问保留（旧口径整条丢弃，本机 112 条真人提问漏计）
    (_blocks("<ide_opened_file>The user opened the file a.py</ide_opened_file>", "为什么报错"),
     ui.QUESTION, "为什么报错"),
    (_blocks("<ide_selection>The user selected the lines 1 to 2</ide_selection>"),
     ui.INJECTED, None),
    # system-reminder 片段剥除，其余文字保留
    ("<system-reminder>be brief</system-reminder>\n真正的问题", ui.QUESTION, "真正的问题"),
    ("<system-reminder>only reminder</system-reminder>", ui.INJECTED, None),
    ("<system-reminder>未闭合直到末尾", ui.INJECTED, None),
    # 用户粘贴的 HTML/XML 原样保留（旧口径会剥标签）
    ("看下 <div class='x'>这里</div>", ui.QUESTION, "看下 <div class='x'>这里</div>"),
    ("<task-notification>done</task-notification>", ui.INJECTED, None),
    ("<local-command-stdout>ok</local-command-stdout>", ui.INJECTED, None),
    ("<local-command-caveat>x</local-command-caveat>", ui.INJECTED, None),
    ("[Request interrupted by user]", ui.ABORTED, None),
    ("<command-name>/model</command-name><command-args>opus</command-args>",
     ui.SLASH, "/model opus"),
    # 新版 command-message 在前：仍识别为斜杠命令
    ("<command-message>review</command-message>\n<command-name>/review</command-name>",
     ui.SLASH, "/review"),
    ("/clear", ui.SLASH, "clear"),
    ("<bash-input>railway login</bash-input>", ui.BASH, "railway login"),
    ("<bash-stdout>ok</bash-stdout><bash-stderr></bash-stderr>", ui.BASH_OUTPUT, None),
    ("This session is being continued from a previous conversation that ran out",
     ui.COMPACT, None),
])
def test_classify(content, kind, text):
    got = ui.classify_claude_record(_rec(content))
    assert got.kind == kind
    if text is not None:
        assert got.text == text


def test_sidechain_and_compact_flags():
    assert ui.classify_claude_record(_rec("派发 prompt", isSidechain=True)).kind == ui.SIDECHAIN
    assert ui.classify_claude_record(_rec("摘要", isCompactSummary=True)).kind == ui.COMPACT


def test_image_only_is_question_and_tool_result_is_not():
    img = {"type": "image", "source": {"type": "base64", "data": "x"}}
    got = ui.classify_claude_record(_rec([img]))
    assert got.is_question and got.images == 1
    tr = {"type": "tool_result", "tool_use_id": "t1", "content": "out"}
    assert ui.classify_claude_record(_rec([tr])).kind == ui.TOOL_RESULT
    # 工具结果旁的注入文字不算提问
    assert ui.classify_claude_record(
        _rec([tr, {"type": "text", "text": "<task-notification>x</task-notification>"}])
    ).kind == ui.TOOL_RESULT


def test_non_user_returns_none():
    assert ui.classify_claude_record({"type": "assistant",
                                      "message": {"role": "assistant"}}) is None


def _write(p, recs):
    p.write_text("\n".join(json.dumps(r, ensure_ascii=False) for r in recs) + "\n",
                 encoding="utf-8")


def test_metrics_popup_and_title_share_one_policy(tmp_path):
    """scan 指标 / 弹窗 / 标题回退 / LLM 对话三处同一口径。"""
    recs = [
        _rec(_blocks("<ide_opened_file>opened x</ide_opened_file>", "第一个问题")),
        _rec("<command-name>/model</command-name>"),
        _rec("<bash-input>ls</bash-input>"),
        _rec("[Request interrupted by user]"),
        _rec("不对，应该改成 B"),
        _rec("<task-notification>bg</task-notification>"),
    ]
    p = tmp_path / "s.jsonl"
    _write(p, recs)
    u = reader.aggregate_usage(p)
    assert u.user_msgs == 2
    assert u.slash_command_count == 1
    assert u.correction_msg_count == 1
    assert u.first_prompt_chars == len("第一个问题")
    assert reader.read_user_messages(p) == ["第一个问题", "不对，应该改成 B"]
    assert reader.read_session_meta(p).title == "第一个问题"
    assert [l for l in reader.read_dialogue(p) if l.startswith("[用户]")] == [
        "[用户] 第一个问题", "[用户] 不对，应该改成 B"]


def test_dispatcher_reads_main_session_only(tmp_path):
    proj = tmp_path / "hash"
    proj.mkdir()
    main = proj / "sid.jsonl"
    _write(main, [_rec("真人消息")])
    sub_dir = proj / "sid" / "subagents"
    sub_dir.mkdir(parents=True)
    sub = sub_dir / "agent-a.jsonl"
    _write(sub, [_rec("You are researching…", isSidechain=True)])
    assert ui.read_user_messages("claude", main) == ["真人消息"]
    assert ui.read_user_messages("claude", sub) == ["真人消息"]
    assert ui.read_user_messages(None, None) == []
    assert ui.read_user_messages("opencode", main, None) == []


def test_omp_user_messages_skip_subagent_dispatch(tmp_path):
    from tcer.core import omp_reader

    def msg(role, text):
        return {"type": "message", "message": {"role": role,
                "content": [{"type": "text", "text": text}]}}
    main = tmp_path / "2026_abc.jsonl"
    _write(main, [{"type": "session", "id": "abc", "cwd": "c:/x"}, msg("user", "真人")])
    sub_dir = tmp_path / "2026_abc"
    sub_dir.mkdir()
    _write(sub_dir / "SubAgent.jsonl", [msg("user", "派发 prompt")])
    assert omp_reader.read_user_messages(main) == ["真人"]
    assert ui.read_user_messages("omp", main) == ["真人"]
