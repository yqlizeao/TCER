"""Tests for paths.py — hash encoding (CLAUDE.md spec)."""
from __future__ import annotations

import json

from tcer.core import paths


def test_encode_hash_windows_path():
    assert paths.encode_hash(r"c:\GitHub\TCER") == "c--GitHub-TCER"


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
