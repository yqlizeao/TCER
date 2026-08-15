"""Tests for loc.py — git-free LOC from tool calls + working-tree scan."""
from __future__ import annotations

import json

from tcer.core import loc


def _write_jsonl(path, tool_calls):
    """Write a session jsonl where each entry is one assistant tool_use call."""
    lines = []
    for name, inp in tool_calls:
        lines.append(json.dumps({
            "type": "assistant",
            "message": {"role": "assistant",
                        "content": [{"type": "tool_use", "name": name, "input": inp}]},
        }))
    path.write_text("\n".join(lines), encoding="utf-8")
    return path


def test_session_loc_write_edit_multiedit(tmp_path):
    f = _write_jsonl(tmp_path / "s.jsonl", [
        ("Write", {"file_path": "a.py", "content": "a\nb\nc"}),          # +3
        ("Edit", {"file_path": "a.py", "old_string": "a\nb",            # 2 → 4
                  "new_string": "A\nB\nC\nD"}),                          # +2
        ("Write", {"file_path": "README.md", "content": "x\ny"}),       # +2
        ("Edit", {"file_path": "notes.txt", "old_string": "p",         # text → now counts (+2)
                  "new_string": "q\nr\ns"}),
        ("MultiEdit", {"file_path": "a.py", "edits": [
            {"old_string": "A", "new_string": "A1\nA2"},               # +1
            {"old_string": "B\nC", "new_string": ""},                  # -2
        ]}),
    ])
    added, deleted = loc.session_loc(f)
    assert added == 10   # notes.txt (+2) now counts as planner-text output
    assert deleted == 2
    assert loc.net_loc(f) == 8
    # First Write to each path is F1-exposed (unseen) until originalFile arrives.
    sl = loc.session_loc_full(f)
    assert sl.unseen_writes == 2  # a.py + README.md


def test_session_loc_write_overwrite_tracks_prior(tmp_path):
    # Overwriting a file written earlier in the SAME session nets the difference.
    f = _write_jsonl(tmp_path / "s.jsonl", [
        ("Write", {"file_path": "a.py", "content": "1\n2\n3\n4\n5"}),  # +5
        ("Write", {"file_path": "a.py", "content": "1\n2"}),          # 5 → 2 = -3
    ])
    added, deleted = loc.session_loc(f)
    assert added == 5
    assert deleted == 3
    assert loc.net_loc(f) == 2


def test_session_loc_ignores_non_edit_tools(tmp_path):
    f = _write_jsonl(tmp_path / "s.jsonl", [
        ("Bash", {"command": "ls"}),
        ("Read", {"file_path": "a.py"}),
        ("Write", {"file_path": "a.py", "content": "only\nthis\ncounts"}),  # +3
    ])
    assert loc.session_loc(f) == (3, 0)


def test_session_loc_notebookedit(tmp_path):
    f = _write_jsonl(tmp_path / "s.jsonl", [
        ("NotebookEdit", {"notebook_path": "nb.py", "new_source": "x\ny\nz", "edit_mode": "insert"}),
        ("NotebookEdit", {"notebook_path": "nb.py", "new_source": "gone", "edit_mode": "delete"}),
    ])
    assert loc.session_loc(f) == (3, 1)


def test_rework_excludes_editing_preexisting_code(tmp_path):
    """Editing code the session never wrote (pre-existing) is NOT self-rework."""
    f = _write_jsonl(tmp_path / "s.jsonl", [
        # First touch of a.py is an Edit → these old lines existed before the
        # session, so deleting them is a normal edit, not rework.
        ("Edit", {"file_path": "a.py", "old_string": "p\nq\nr", "new_string": "X"}),
    ])
    sl = loc.session_loc_full(f)
    assert sl.deleted == 2      # net deletions (3 old − 1 new) still counted
    assert sl.rework_deleted == 0  # but none of it is the session's own rework


def test_rework_counts_deleting_own_written_lines(tmp_path):
    """Writing lines then deleting them within the session IS self-rework."""
    f = _write_jsonl(tmp_path / "s.jsonl", [
        ("Write", {"file_path": "a.py", "content": "1\n2\n3\n4\n5"}),  # session authors 5 lines
        ("Edit", {"file_path": "a.py", "old_string": "1\n2\n3",        # net -2 of its own
                  "new_string": "one"}),
    ])
    sl = loc.session_loc_full(f)
    assert sl.rework_deleted == 2   # the 2 net-deleted lines were session-authored


def test_rework_capped_at_authored(tmp_path):
    """A delete larger than what the session wrote only counts up to authored."""
    f = _write_jsonl(tmp_path / "s.jsonl", [
        ("Write", {"file_path": "a.py", "content": "a\nb"}),           # authors 2
        ("Edit", {"file_path": "a.py", "old_string": "a\nb\nPRE\nPRE", # net -4
                  "new_string": ""}),
    ])
    sl = loc.session_loc_full(f)
    assert sl.rework_deleted == 2   # only the 2 it authored count as rework


def test_original_file_corrects_f1_overwrite(tmp_path):
    """toolUseResult.originalFile:覆写既有文件时按真实原行数修正 F1 高估。"""
    import json

    from tcer.core import reader as reader_mod
    lines = [
        # Write 10 行到「未见过」的文件
        {"type": "assistant",
         "message": {"role": "assistant", "id": "m1",
                     "usage": {"input_tokens": 10, "output_tokens": 5,
                               "cache_creation_input_tokens": 0,
                               "cache_read_input_tokens": 0},
                     "content": [{"type": "tool_use", "id": "t1", "name": "Write",
                                  "input": {"file_path": "a.py",
                                            "content": "\n".join(f"l{i}" for i in range(10))}}]}},
        # 结果行:原文件其实有 6 行(覆写)
        {"type": "user", "toolUseResult": {
            "filePath": "a.py",
            "originalFile": "\n".join(f"o{i}" for i in range(6)),
            "userModified": False},
         "message": {"role": "user",
                     "content": [{"type": "tool_result", "tool_use_id": "t1"}]}},
    ]
    p = tmp_path / "s.jsonl"
    p.write_text("\n".join(json.dumps(x) for x in lines) + "\n", encoding="utf-8")
    # scan 与 session_loc_full 两条路径一致
    _, sl = reader_mod.scan_session(p, with_loc=True, include_user_texts=False)
    sl2 = loc.session_loc_full(p)
    for s in (sl, sl2):
        assert s.added == 4          # 10 - 6,不再是 10
        assert s.deleted == 0
        assert s.unseen_writes == 0  # 先验已知,不再计 F1 暴露


def test_original_file_new_file_clears_unseen_only(tmp_path):
    """originalFile 为空串 = 确认新文件:added 保持全文,unseen 归零。"""
    import json

    from tcer.core import reader as reader_mod
    lines = [
        {"type": "assistant",
         "message": {"role": "assistant", "id": "m1",
                     "usage": {"input_tokens": 10, "output_tokens": 5,
                               "cache_creation_input_tokens": 0,
                               "cache_read_input_tokens": 0},
                     "content": [{"type": "tool_use", "id": "t1", "name": "Write",
                                  "input": {"file_path": "b.py", "content": "x\ny\nz"}}]}},
        {"type": "user", "toolUseResult": {"filePath": "b.py", "originalFile": ""},
         "message": {"role": "user",
                     "content": [{"type": "tool_result", "tool_use_id": "t1"}]}},
    ]
    p = tmp_path / "s.jsonl"
    p.write_text("\n".join(json.dumps(x) for x in lines) + "\n", encoding="utf-8")
    _, sl = reader_mod.scan_session(p, with_loc=True, include_user_texts=False)
    assert (sl.added, sl.deleted, sl.unseen_writes) == (3, 0, 0)


def test_user_modified_counted(tmp_path):
    import json

    from tcer.core import reader as reader_mod
    lines = [
        {"type": "assistant",
         "message": {"role": "assistant", "id": "m1",
                     "usage": {"input_tokens": 10, "output_tokens": 5,
                               "cache_creation_input_tokens": 0,
                               "cache_read_input_tokens": 0},
                     "content": [{"type": "tool_use", "id": "t1", "name": "Edit",
                                  "input": {"file_path": "c.py", "old_string": "a",
                                            "new_string": "b"}}]}},
        {"type": "user", "toolUseResult": {"filePath": "c.py", "userModified": True},
         "message": {"role": "user",
                     "content": [{"type": "tool_result", "tool_use_id": "t1"}]}},
    ]
    p = tmp_path / "s.jsonl"
    p.write_text("\n".join(json.dumps(x) for x in lines) + "\n", encoding="utf-8")
    u = reader_mod.aggregate_usage(p)
    assert u.user_modified_count == 1


def test_failed_tool_calls_excluded_from_loc(tmp_path):
    """is_error 的 Edit/Write 不产生行增量、不污染 file_lines 基线。

    场景：失败 Edit（old_string 不匹配）→ 重试成功净增 1 行；被用户拒绝的
    Write（权限拒绝）。真实产出 = 1 行。
    """
    import json

    from tcer.core import reader as reader_mod
    lines = [
        # 失败的 Edit：old_string 不匹配（is_error），不改变文件
        {"type": "assistant",
         "message": {"role": "assistant", "id": "m1",
                     "usage": {"input_tokens": 10, "output_tokens": 5,
                               "cache_creation_input_tokens": 0,
                               "cache_read_input_tokens": 0},
                     "content": [{"type": "tool_use", "id": "t1", "name": "Edit",
                                  "input": {"file_path": "a.py", "old_string": "x\ny",
                                            "new_string": "X\nY\nZ\nW"}}]}},
        {"type": "user",
         "message": {"role": "user",
                     "content": [{"type": "tool_result", "tool_use_id": "t1",
                                  "is_error": True,
                                  "content": "String to replace not found"}]}},
        # 被用户拒绝的 Write：不产生任何行
        {"type": "assistant",
         "message": {"role": "assistant", "id": "m2",
                     "usage": {"input_tokens": 10, "output_tokens": 5,
                               "cache_creation_input_tokens": 0,
                               "cache_read_input_tokens": 0},
                     "content": [{"type": "tool_use", "id": "t2", "name": "Write",
                                  "input": {"file_path": "b.py",
                                            "content": "p\nq\nr"}}]}},
        {"type": "user",
         "message": {"role": "user",
                     "content": [{"type": "tool_result", "tool_use_id": "t2",
                                  "is_error": True,
                                  "content": "The user doesn't want to proceed"}]}},
        # 重试成功的 Edit：净增 1 行
        {"type": "assistant",
         "message": {"role": "assistant", "id": "m3",
                     "usage": {"input_tokens": 10, "output_tokens": 5,
                               "cache_creation_input_tokens": 0,
                               "cache_read_input_tokens": 0},
                     "content": [{"type": "tool_use", "id": "t3", "name": "Edit",
                                  "input": {"file_path": "a.py", "old_string": "a",
                                            "new_string": "a\nb"}}]}},
        {"type": "user",
         "message": {"role": "user",
                     "content": [{"type": "tool_result", "tool_use_id": "t3"}]}},
    ]
    p = tmp_path / "s.jsonl"
    p.write_text("\n".join(json.dumps(x) for x in lines) + "\n", encoding="utf-8")
    # scan 与 session_loc_full 两条路径一致
    _, sl = reader_mod.scan_session(p, with_loc=True, include_user_texts=False)
    sl2 = loc.session_loc_full(p)
    for s in (sl, sl2):
        assert s.added == 1            # 只有成功 Edit 的 +1，无幻影行
        assert s.deleted == 0
        assert s.unseen_writes == 0    # 被拒的 Write 不计 F1 暴露
        assert "a.py" in s.file_edit_counts
        assert "b.py" not in s.file_edit_counts  # 被拒调用不算编辑次数
    # 工具错误照常计数（不影响错误统计口径）
    u = reader_mod.aggregate_usage(p)
    assert u.tool_errors == 2


def test_truncated_session_edits_flushed(tmp_path):
    """结果未到的编辑调用（截断/进行中会话）仍按成功回放（旧行为兼容）。"""
    import json

    from tcer.core import reader as reader_mod
    lines = [
        {"type": "assistant",
         "message": {"role": "assistant", "id": "m1",
                     "usage": {"input_tokens": 10, "output_tokens": 5,
                               "cache_creation_input_tokens": 0,
                               "cache_read_input_tokens": 0},
                     "content": [{"type": "tool_use", "id": "t1", "name": "Write",
                                  "input": {"file_path": "a.py",
                                            "content": "x\ny\nz"}}]}},
        # 文件到此截断：没有 tool_result 行
    ]
    p = tmp_path / "s.jsonl"
    p.write_text("\n".join(json.dumps(x) for x in lines) + "\n", encoding="utf-8")
    _, sl = reader_mod.scan_session(p, with_loc=True, include_user_texts=False)
    sl2 = loc.session_loc_full(p)
    for s in (sl, sl2):
        assert (s.added, s.deleted, s.unseen_writes) == (3, 0, 1)


def test_resent_tool_use_deduped_in_loc(tmp_path):
    """Claude Code 2.1.227+ rewind/重发：同 tool_use id 的历史对只回放一次
    （scan_session 与 session_loc_full 两路径同口径）。"""
    import json

    from tcer.core import reader as R

    def _asst(i, cid, content):
        return {"type": "assistant", "timestamp": 1_770_000_000_000 + i * 1000,
                "message": {"role": "assistant", "id": f"m{i}",
                            "usage": {"input_tokens": 10, "output_tokens": 5,
                                      "cache_creation_input_tokens": 0,
                                      "cache_read_input_tokens": 0},
                            "content": content}}

    tool_block = {"type": "tool_use", "id": "t1", "name": "Edit",
                  "input": {"file_path": "a.py", "old_string": "x",
                            "new_string": "x\ny\nz"}}
    lines = [
        _asst(0, "t1", [tool_block]),
        {"type": "user",
         "message": {"role": "user",
                     "content": [{"type": "tool_result", "tool_use_id": "t1"}]}},
        # rewind 后整段重发（同 message.id 语义的重复历史，tool_use id 相同）
        _asst(1, "t1", [tool_block]),
        {"type": "user",
         "message": {"role": "user",
                     "content": [{"type": "tool_result", "tool_use_id": "t1"}]}},
    ]
    p = tmp_path / "s.jsonl"
    p.write_text("\n".join(json.dumps(x) for x in lines) + "\n", encoding="utf-8")
    u, sloc = R.scan_session(p, with_loc=True, include_user_texts=False)
    sloc2 = loc.session_loc_full(p)
    for s in (sloc, sloc2):
        assert s.added == 2            # 只回放一次（+2），不因重发变 +4
    assert u.tool_calls.get("Edit") == 1
