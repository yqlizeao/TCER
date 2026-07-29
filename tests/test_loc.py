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
