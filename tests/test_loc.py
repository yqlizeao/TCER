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
        ("Edit", {"file_path": "notes.txt", "old_string": "p",         # non-code → skipped
                  "new_string": "q\nr\ns"}),
        ("MultiEdit", {"file_path": "a.py", "edits": [
            {"old_string": "A", "new_string": "A1\nA2"},               # +1
            {"old_string": "B\nC", "new_string": ""},                  # -2
        ]}),
    ])
    added, deleted = loc.session_loc(f)
    assert added == 8
    assert deleted == 2
    assert loc.net_loc(f) == 6


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


def test_tree_loc_counts_code_skips_excluded(tmp_path):
    (tmp_path / "a.py").write_text("1\n2\n3\n", encoding="utf-8")
    (tmp_path / "b.md").write_text("x\ny\n", encoding="utf-8")
    (tmp_path / "data.bin").write_text("ignored\nbinary\n", encoding="utf-8")  # non-code suffix
    sub = tmp_path / "pkg"
    sub.mkdir()
    (sub / "c.py").write_text("one\n", encoding="utf-8")
    excluded = tmp_path / "__pycache__"
    excluded.mkdir()
    (excluded / "junk.py").write_text("should\nnot\ncount\n", encoding="utf-8")

    # a.py(3) + b.md(2) + pkg/c.py(1) = 6; data.bin and __pycache__ excluded
    assert loc.tree_loc(tmp_path) == 6


def test_tree_loc_none_for_missing_dir(tmp_path):
    assert loc.tree_loc(tmp_path / "does-not-exist") is None
