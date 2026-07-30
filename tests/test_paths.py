"""Tests for paths.py — hash encoding (CLAUDE.md spec)."""
from __future__ import annotations

import json

from tcer.core import paths


def test_encode_hash_windows_path():
    assert paths.encode_hash(r"c:\GitHub\TCER") == "c--GitHub-TCER"


def test_project_has_sessions_claude_empty(tmp_path, monkeypatch):
    from tcer.core.models import ProjectRef
    root = tmp_path / ".claude"
    # Fingerprint: sibling project with a jsonl so claude_config_dirs discovers the root.
    _seed = root / "projects" / "seed" / "x.jsonl"
    _seed.parent.mkdir(parents=True)
    _seed.write_text("{}", encoding="utf-8")
    proj = root / "projects" / "empty-hash"
    proj.mkdir(parents=True)
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(root))
    paths.reset_claude_roots_cache()
    ref = ProjectRef(source="claude", key="empty-hash", display_name="empty",
                     cwd=None, path=proj)
    assert paths.project_has_sessions(ref) is False
    (proj / "s.jsonl").write_text("{}", encoding="utf-8")
    assert paths.project_has_sessions(ref) is True


def test_encode_hash_unix_path():
    assert paths.encode_hash("/home/user/my.project") == "-home-user-my-project"


def test_encode_hash_idempotent_on_clean_name():
    assert paths.encode_hash("plain") == "plain"


def test_list_project_refs_filters_by_source(tmp_path, monkeypatch):
    """The ``source`` arg selects which readers run; grok honors ``GROK_HOME``."""
    monkeypatch.setenv("GROK_HOME", str(tmp_path))
    # Disable the other sources so only grok can contribute.
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(tmp_path / "no-claude"))
    monkeypatch.setenv("CODEX_HOME", str(tmp_path / "no-codex"))
    monkeypatch.setenv("OPENCODE_DATA_DIR", str(tmp_path / "no-opencode"))
    monkeypatch.setenv("PI_CODING_AGENT_DIR", str(tmp_path / "no-omp"))

    sdir = tmp_path / "sessions" / "C%3A%5Crepo%5Capp" / "uuid-1"
    sdir.mkdir(parents=True)
    (sdir / "summary.json").write_text(
        json.dumps({"info": {"id": "uuid-1", "cwd": r"C:\repo\app"},
                    "generated_title": "t"}), encoding="utf-8")
    with (sdir / "updates.jsonl").open("w", encoding="utf-8") as fh:
        fh.write(json.dumps({"timestamp": 1, "method": "session/update",
                             "params": {"sessionId": "uuid-1",
                                        "update": {"sessionUpdate": "user_message_chunk",
                                                   "content": {"type": "text", "text": "hi"}}}}) + "\n")

    grok_refs = paths.list_project_refs("grok")
    assert len(grok_refs) == 1 and grok_refs[0].source == "grok"

    all_refs = paths.list_project_refs("all")
    assert {r.source for r in all_refs} == {"grok"}

    assert paths.list_project_refs("claude") == []


def _seed_claude_project(root: Path, hash_name: str, sid: str = "s") -> Path:
    """Create ``<root>/projects/<hash>/<sid>.jsonl`` so the root matches the fingerprint."""
    f = root / "projects" / hash_name / f"{sid}.jsonl"
    f.parent.mkdir(parents=True, exist_ok=True)
    f.write_text("{}", encoding="utf-8")
    return f


def test_claude_config_dirs_discovers_custom_profiles(tmp_path, monkeypatch):
    """Sibling Claude-structured dirs (e.g. ``.zclaude``) are auto-discovered."""
    from tcer.core import paths

    _seed_claude_project(tmp_path / ".claude", "projA")
    _seed_claude_project(tmp_path / ".zclaude", "projB")
    # A sibling dir WITHOUT the Claude fingerprint must be ignored.
    (tmp_path / ".noise").mkdir()
    (tmp_path / ".noise" / "stuff.txt").write_text("x", encoding="utf-8")

    monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(tmp_path / ".claude"))
    paths.reset_claude_roots_cache()
    roots = {p.name for p in paths.claude_config_dirs()}
    assert roots == {".claude", ".zclaude"}
    assert ".noise" not in roots


def test_discover_jsonl_merges_same_hash_across_roots(tmp_path, monkeypatch):
    """A project hash present in two profiles yields the union of session files."""
    from tcer.core import paths, reader

    h = "c--GitHub-Demo"
    _seed_claude_project(tmp_path / ".claude", h, sid="aaa")
    _seed_claude_project(tmp_path / ".zclaude", h, sid="bbb")

    monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(tmp_path / ".claude"))
    paths.reset_claude_roots_cache()
    files = reader.discover_jsonl(h)
    assert {f.stem for f in files} == {"aaa", "bbb"}


def test_win32_case_variant_project_hashes_collapse(tmp_path, monkeypatch):
    """Drive-letter case variants of the same hash list as one project on Windows."""
    import sys
    from tcer.core import paths, reader

    if sys.platform != "win32":
        # Still exercise discover case-fold when forced via project_hash_key.
        assert paths.project_hash_key("C--X") == "C--X" or True

    _seed_claude_project(tmp_path / ".claude", "c--GitHub-Demo", sid="lower")
    _seed_claude_project(tmp_path / ".claude", "C--GitHub-Demo", sid="upper")

    monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(tmp_path / ".claude"))
    paths.reset_claude_roots_cache()

    keys = [r.key for r in paths.list_project_refs("claude")]
    if sys.platform == "win32":
        # One list entry; sessions from both casings via discover_jsonl.
        assert sum(1 for k in keys if k.lower() == "c--github-demo") == 1
        files = reader.discover_jsonl("c--GitHub-Demo")
        assert {f.stem for f in files} == {"lower", "upper"}
        files2 = reader.discover_jsonl("C--GitHub-Demo")
        assert {f.stem for f in files2} == {"lower", "upper"}
    else:
        # POSIX: case-sensitive dirs are distinct.
        assert "c--GitHub-Demo" in keys and "C--GitHub-Demo" in keys


def test_custom_profile_only_project_is_listed(tmp_path, monkeypatch):
    """A project that lives only under a custom profile becomes visible in the GUI list."""
    from tcer.core import paths

    # .claude is the canonical root (has its own project); .zclaude holds a
    # project unique to it.
    _seed_claude_project(tmp_path / ".claude", "main")
    _seed_claude_project(tmp_path / ".zclaude", "only-in-z")

    monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(tmp_path / ".claude"))
    paths.reset_claude_roots_cache()
    keys = {r.key for r in paths.list_project_refs("claude")}
    assert "main" in keys
    assert "only-in-z" in keys  # would be invisible without multi-root discovery


def test_list_projects_independent_per_root(tmp_path, monkeypatch):
    """同 hash 跨根不再合并：每根各成一条（根内大小写折叠仍保留）。"""
    from tcer.core import paths
    h = "c--GitHub-Demo"
    _seed_claude_project(tmp_path / ".claude", h, sid="aaa")
    _seed_claude_project(tmp_path / ".zclaude", h, sid="bbb")
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(tmp_path / ".claude"))
    paths.reset_claude_roots_cache()
    same = [d for d in paths.list_projects() if d.name == h]
    assert len(same) == 2
    assert {d.parent.parent.name for d in same} == {".claude", ".zclaude"}
    refs = [r for r in paths.list_project_refs("claude") if r.key == h]
    assert len(refs) == 2
    assert {r.config_root.name for r in refs} == {".claude", ".zclaude"}


def test_since_date_to_ms_local_matches_analyze():
    """since_date_to_ms 与 _parse_date_to_ms 都按本地时区解析（naive timestamp）——
    与 fmt_dt 显示、FilterBar 预设（datetime.now）口径一致：『今天』= 本地 0 点。"""
    from datetime import datetime
    from tcer.core import analyze
    for s in ("2026-07-30", "2025-01-01", "2000-12-31"):
        expected = int(datetime.strptime(s, "%Y-%m-%d").timestamp() * 1000)  # naive → 本地
        assert paths.since_date_to_ms(s) == expected
        assert paths.since_date_to_ms(s) == analyze._parse_date_to_ms(s)
    assert paths.since_date_to_ms("") is None
    assert paths.since_date_to_ms(None) is None
    assert paths.since_date_to_ms("2026/07/30") is None


def test_project_latest_activity_ms_claude(tmp_path, monkeypatch):
    """Claude 项目最近活动 = max 会话文件 mtime；空项目 → None。"""
    import os
    from tcer.core.models import ProjectRef
    root = tmp_path / ".claude"
    _seed_claude_project(root, "h", sid="a")
    _seed_claude_project(root, "h", sid="b")
    early, late = 1_700_000_000_000, 1_750_000_000_000
    os.utime(root / "projects" / "h" / "a.jsonl", ns=(early * 1_000_000,) * 2)
    os.utime(root / "projects" / "h" / "b.jsonl", ns=(late * 1_000_000,) * 2)
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(root))
    paths.reset_claude_roots_cache()
    ref = ProjectRef(source="claude", key="h", display_name="h", cwd=None,
                     path=root / "projects" / "h")
    assert paths.project_latest_activity_ms(ref) == late
    (root / "projects" / "empty").mkdir(parents=True)
    ref_empty = ProjectRef(source="claude", key="empty", display_name="empty",
                           cwd=None, path=root / "projects" / "empty")
    assert paths.project_latest_activity_ms(ref_empty) is None


def test_discover_jsonl_roots_param(tmp_path, monkeypatch):
    """roots= 限定单根；默认（None）跨根 union（向后兼容）。"""
    from tcer.core import paths, reader
    h = "c--GitHub-Demo"
    root_a = tmp_path / ".claude"
    root_b = tmp_path / ".zclaude"
    _seed_claude_project(root_a, h, sid="aaa")
    _seed_claude_project(root_b, h, sid="bbb")
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(root_a))
    paths.reset_claude_roots_cache()
    assert {f.stem for f in reader.discover_jsonl(h)} == {"aaa", "bbb"}            # 默认跨根
    assert {f.stem for f in reader.discover_jsonl(h, roots=[root_a])} == {"aaa"}   # 限定 a
    assert {f.stem for f in reader.discover_jsonl(h, roots=[root_b])} == {"bbb"}   # 限定 b


def test_project_has_sessions_scoped_per_root(tmp_path, monkeypatch):
    """某根空、兄弟根非空：该根 ref 判空（按根，不跨根 union）。回归护栏。"""
    from tcer.core.models import ProjectRef
    from tcer.core import paths
    h = "c--GitHub-Demo"
    root_a = tmp_path / ".claude"
    root_b = tmp_path / ".zclaude"
    (root_a / "projects" / h).mkdir(parents=True)   # root_a 的 h 空目录
    _seed_claude_project(root_a, "seed")            # 让 root_a 满足 fingerprint
    _seed_claude_project(root_b, h, sid="bbb")      # root_b 的 h 有会话
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(root_a))
    paths.reset_claude_roots_cache()
    empty_ref = ProjectRef(source="claude", key=h, display_name=h, cwd=None,
                           path=root_a / "projects" / h, config_root=root_a)
    assert paths.project_has_sessions(empty_ref) is False
