"""Tests for paths.py — hash encoding (CLAUDE.md spec)."""
from __future__ import annotations

from tcer import paths


def test_encode_hash_windows_path():
    assert paths.encode_hash(r"c:\GitHub\TCER") == "c--GitHub-TCER"


def test_encode_hash_unix_path():
    assert paths.encode_hash("/home/user/my.project") == "-home-user-my-project"


def test_encode_hash_idempotent_on_clean_name():
    assert paths.encode_hash("plain") == "plain"
