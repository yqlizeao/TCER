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
    # Default disk_prior=False: first Write to each path is F1-exposed (unseen).
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


def test_disk_prior_corrects_write_overwrite_of_existing_file(tmp_path):
    """F1: Write to an existing on-disk file nets against disk lines, not full content."""
    target = tmp_path / "existing.py"
    target.write_text("a\nb\nc\nd\ne\n", encoding="utf-8")  # 5 lines on disk
    f = _write_jsonl(tmp_path / "s.jsonl", [
        ("Write", {"file_path": str(target), "content": "x\ny"}),  # 5 → 2
    ])
    sl = loc.session_loc_full(f, disk_prior=True)
    assert sl.added == 0
    assert sl.deleted == 3
    assert sl.unseen_writes == 0
    assert sl.added - sl.deleted == -3
    assert sl.rework_deleted == 0  # deleted pre-existing, not self-rework


def test_disk_prior_relative_path_needs_cwd(tmp_path):
    (tmp_path / "rel.py").write_text("1\n2\n3\n", encoding="utf-8")
    f = _write_jsonl(tmp_path / "s.jsonl", [
        ("Write", {"file_path": "rel.py", "content": "only"}),  # 3 → 1
    ])
    # Without cwd: cannot resolve → classic F1 (assume 0, full add, unseen).
    sl0 = loc.session_loc_full(f, cwd=None, disk_prior=True)
    assert sl0.added == 1 and sl0.deleted == 0 and sl0.unseen_writes == 1
    # With cwd: correct net.
    sl1 = loc.session_loc_full(f, cwd=tmp_path, disk_prior=True)
    assert sl1.added == 0 and sl1.deleted == 2 and sl1.unseen_writes == 0


def test_disk_prior_new_file_is_not_unseen(tmp_path):
    """Missing target after resolve is a true new file — not F1 exposure."""
    missing = tmp_path / "brand_new.py"
    assert not missing.exists()
    f = _write_jsonl(tmp_path / "s.jsonl", [
        ("Write", {"file_path": str(missing), "content": "a\nb"}),
    ])
    sl = loc.session_loc_full(f, disk_prior=True)
    assert sl.added == 2 and sl.deleted == 0 and sl.unseen_writes == 0


def test_disk_prior_post_session_match_counts_full_write(tmp_path):
    """After a real session, disk still holds Write content — must not zero net.

    Live failure mode: disk_prior seeded post-session line count ≈ Write
    payload → added=0 for Write-created .py files that still exist.
    """
    content = "def main():\n    return 1\n"
    target = tmp_path / "created.py"
    target.write_text(content, encoding="utf-8")  # post-session disk
    f = _write_jsonl(tmp_path / "s.jsonl", [
        ("Write", {"file_path": str(target), "content": content}),
    ])
    sl = loc.session_loc_full(f, disk_prior=True)
    assert sl.added == 2 and sl.deleted == 0
    assert sl.unseen_writes == 0
    assert sl.added - sl.deleted == 2


def test_disk_prior_disabled_keeps_legacy_f1(tmp_path):
    target = tmp_path / "e.py"
    target.write_text("1\n2\n3\n", encoding="utf-8")
    f = _write_jsonl(tmp_path / "s.jsonl", [
        ("Write", {"file_path": str(target), "content": "x"}),
    ])
    sl = loc.session_loc_full(f, disk_prior=False)
    assert sl.added == 1 and sl.unseen_writes == 1


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


def test_tree_loc_skips_build_and_dep_trees(tmp_path):
    """Rust `target/`, JS build caches, and vendored dep trees must be skipped —
    they hold generated/vendored files (not hand-written source) and the big
    ones froze tree_loc for minutes on large repos (hmai regression)."""
    (tmp_path / "real.py").write_text("a\nb\n", encoding="utf-8")  # 2 lines counted
    for excluded in ("target", ".next", "Pods", "DerivedData", ".gradle",
                     ".dart_tool", "bower_components", ".caches", "coverage"):
        d = tmp_path / excluded
        d.mkdir()
        # each would add 3 lines if NOT excluded
        (d / "gen.rs").write_text("x\ny\nz\n", encoding="utf-8")
    # only real.py(2) counts; the 9 generated trees are skipped at any depth
    assert loc.tree_loc(tmp_path) == 2


def test_tree_loc_none_for_missing_dir(tmp_path):
    assert loc.tree_loc(tmp_path / "does-not-exist") is None
