"""WorkBuddy / CodeBuddy session reader for TCER Server.

Thin wrapper delegating to ``tcer.core.workbuddy_reader`` for single source of truth.
"""
from __future__ import annotations

from tcer.core.workbuddy_reader import (
    _WORKBUDDY_TOOL_MAP,
    aggregate_usage,
    clean_user_text,
    derive_project_name,
    discover_sessions,
    extract_project_dir_from_session,
    has_loc_signal,
    iter_records,
    list_project_refs,
    parse_session_file,
    read_conversation,
    read_session_meta,
    read_user_messages,
    resolve_project,
    session_loc_full,
    sessions_for_project,
    workbuddy_dir,
    workbuddy_roots,
    workbuddy_sessions_dir,
)

__all__ = [
    "_WORKBUDDY_TOOL_MAP",
    "aggregate_usage",
    "clean_user_text",
    "derive_project_name",
    "discover_sessions",
    "extract_project_dir_from_session",
    "has_loc_signal",
    "iter_records",
    "list_project_refs",
    "parse_session_file",
    "read_conversation",
    "read_session_meta",
    "read_user_messages",
    "resolve_project",
    "session_loc_full",
    "sessions_for_project",
    "workbuddy_dir",
    "workbuddy_roots",
    "workbuddy_sessions_dir",
]
