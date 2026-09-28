"""概念对齐模块的表结构（文档 §4）。由 ``db.init_db`` 在主 schema 之后调用。

所有派生表（utterance / term_usage / misread_event / …）都能从 ``uploads.raw_json``
+ ``concept`` 表完整重建，撤回授权时按 ``origin_ref`` 直接删除即可。
"""
from __future__ import annotations

import sqlite3

SCHEMA = """
-- ============ 身份与权限 ============
CREATE TABLE IF NOT EXISTS person_profile (
    person          TEXT PRIMARY KEY,          -- 归一后的 person（canonical_person）
    username        TEXT,                      -- 绑定的登录账号
    function        TEXT,                      -- 职能 key（termbase.ROLE_LABELS）
    team            TEXT,
    function_source TEXT NOT NULL DEFAULT 'self',  -- imported | self | feishu | inferred
    personal_opt_out INTEGER NOT NULL DEFAULT 0,
    updated_at      INTEGER NOT NULL
);

CREATE TABLE IF NOT EXISTS user_role (
    username TEXT NOT NULL,
    role     TEXT NOT NULL,
    PRIMARY KEY (username, role)
);

CREATE TABLE IF NOT EXISTS person_grant (       -- 本人主动授权他人查看个人视图
    owner    TEXT NOT NULL,                     -- 授权人（登录名）
    grantee  TEXT NOT NULL,                     -- 被授权人（登录名）
    created_at INTEGER NOT NULL,
    PRIMARY KEY (owner, grantee)
);

CREATE TABLE IF NOT EXISTS view_access_log (
    id       INTEGER PRIMARY KEY AUTOINCREMENT,
    viewer   TEXT NOT NULL,
    subject  TEXT,                              -- 被查看者登录名（个人视图时）
    view     TEXT NOT NULL,
    at       INTEGER NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_val_subject ON view_access_log(subject, at);

-- ============ 语料与用法 ============
CREATE TABLE IF NOT EXISTS utterance (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    origin      TEXT NOT NULL,                  -- upload | doc
    origin_ref  TEXT NOT NULL,                  -- uploads.id / source_document.id
    block_idx   INTEGER NOT NULL,
    session_key TEXT,
    owner       TEXT,                           -- uploads.uploaded_by（登录名，个人视图鉴权）
    person      TEXT,                           -- canonical person
    project     TEXT,                           -- canonical project
    speaker     TEXT NOT NULL,                  -- user | assistant | subagent_prompt | doc
    user_turn   INTEGER,                        -- 所属用户轮次（0-based）
    ts          INTEGER,
    public      INTEGER NOT NULL DEFAULT 0,     -- 源会话 visibility=public（证据可公开展示）
    text        TEXT NOT NULL,
    UNIQUE (origin, origin_ref, block_idx)
);
CREATE INDEX IF NOT EXISTS idx_utt_origin  ON utterance(origin, origin_ref);
CREATE INDEX IF NOT EXISTS idx_utt_person  ON utterance(person);

CREATE TABLE IF NOT EXISTS term_usage (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    utterance_id INTEGER NOT NULL,
    term_slug    TEXT NOT NULL,
    surface      TEXT NOT NULL,
    char_start   INTEGER NOT NULL,
    char_end     INTEGER NOT NULL,
    sense        TEXT,                          -- P3 语义层填充：baseline | misc:<i> | other
    sense_prob   REAL,
    sense_by     TEXT,
    UNIQUE (utterance_id, term_slug, char_start)
);
CREATE INDEX IF NOT EXISTS idx_tu_slug ON term_usage(term_slug);
CREATE INDEX IF NOT EXISTS idx_tu_utt  ON term_usage(utterance_id);

CREATE TABLE IF NOT EXISTS session_facet (
    session_key TEXT NOT NULL,
    origin_ref  TEXT NOT NULL,
    name        TEXT NOT NULL,
    value       TEXT,
    PRIMARY KEY (session_key, name)
);

CREATE TABLE IF NOT EXISTS term_candidate (
    surface      TEXT PRIMARY KEY,
    g2           REAL,
    freq         INTEGER NOT NULL,
    n_persons    INTEGER NOT NULL,
    n_projects   INTEGER NOT NULL,
    persons_json TEXT NOT NULL,                 -- 使用者列表（仅供披露层判断 / 本人视图）
    nearest_slug TEXT,
    status       TEXT NOT NULL DEFAULT 'new',   -- new | nominated | rejected
    updated_at   INTEGER NOT NULL
);

CREATE TABLE IF NOT EXISTS candidate_decision (   -- 候选人工决策（重建时保留）
    surface    TEXT PRIMARY KEY,
    status     TEXT NOT NULL,                   -- nominated | rejected
    decided_by TEXT NOT NULL,
    decided_at INTEGER NOT NULL
);

CREATE TABLE IF NOT EXISTS candidate_ignore (      -- 「只有我在用的词」本人标记无需收录
    owner   TEXT NOT NULL,
    surface TEXT NOT NULL,
    PRIMARY KEY (owner, surface)
);

-- ============ 信号 ============
CREATE TABLE IF NOT EXISTS misread_event (
    id                INTEGER PRIMARY KEY AUTOINCREMENT,
    origin_ref        TEXT NOT NULL,
    session_key       TEXT,
    owner             TEXT,
    person            TEXT,
    project           TEXT,
    term_slug         TEXT NOT NULL,
    usage_utt_id      INTEGER NOT NULL,         -- 触发的 user 侧用法所在 utterance
    correction_utt_id INTEGER NOT NULL,
    user_turns_between INTEGER NOT NULL,
    ai_blocks_between INTEGER NOT NULL,
    cost_usd_est      REAL,                     -- 估算代价（会话成本按 AI 块占比分摊）
    tokens_est        INTEGER,
    evidence          TEXT NOT NULL,            -- rementioned | edited
    confirmed_by      TEXT NOT NULL DEFAULT 'regex',
    confirm_prob      REAL
);
CREATE INDEX IF NOT EXISTS idx_me_slug ON misread_event(term_slug);
CREATE INDEX IF NOT EXISTS idx_me_origin ON misread_event(origin_ref);

CREATE TABLE IF NOT EXISTS substitution_event (   -- AI 换词：用户说 X，AI 改用 Y
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    origin_ref  TEXT NOT NULL,
    owner       TEXT, person TEXT, project TEXT,
    from_slug   TEXT NOT NULL,
    to_slug     TEXT NOT NULL,
    user_utt_id INTEGER NOT NULL,
    ai_utt_id   INTEGER NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_se_origin ON substitution_event(origin_ref);

CREATE TABLE IF NOT EXISTS fidelity_event (       -- AI↔AI：子代理派发 prompt 术语保真
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    origin_ref  TEXT NOT NULL,
    owner       TEXT, person TEXT, project TEXT,
    utt_id      INTEGER NOT NULL,
    expected_json TEXT NOT NULL,                -- 近 2 个用户轮次出现的术语
    kept_json   TEXT NOT NULL                   -- 派发 prompt 里保留下来的
);
CREATE INDEX IF NOT EXISTS idx_fe_origin ON fidelity_event(origin_ref);

-- ============ 治理 ============
CREATE TABLE IF NOT EXISTS concept (
    slug        TEXT PRIMARY KEY,
    entry_json  TEXT NOT NULL,                  -- 与客户端 TermEntry.to_dict() 同构
    status      TEXT NOT NULL,                  -- draft | reviewing | active | deprecated
    version     INTEGER NOT NULL,
    owner       TEXT,
    created_by  TEXT,
    approved_by TEXT,
    approved_at INTEGER,
    updated_at  INTEGER NOT NULL
);

CREATE TABLE IF NOT EXISTS concept_history (
    slug       TEXT NOT NULL,
    version    INTEGER NOT NULL,
    entry_json TEXT NOT NULL,
    status     TEXT NOT NULL,
    changed_by TEXT NOT NULL,
    changed_at INTEGER NOT NULL,
    reason     TEXT,
    PRIMARY KEY (slug, version)
);

CREATE TABLE IF NOT EXISTS concept_dispute (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    slug        TEXT NOT NULL,
    raised_by   TEXT NOT NULL,
    body        TEXT NOT NULL,
    status      TEXT NOT NULL DEFAULT 'open',   -- open | accepted | rejected
    reply       TEXT,
    resolved_by TEXT,
    created_at  INTEGER NOT NULL,
    resolved_at INTEGER
);

CREATE TABLE IF NOT EXISTS source_document (      -- web 预留入口：Wiki / 术语表 / PRD / IM 导出
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    kind        TEXT NOT NULL,                  -- glossary | wiki | prd | onboarding | im | other
    title       TEXT NOT NULL,
    uploaded_by TEXT NOT NULL,
    visibility  TEXT NOT NULL DEFAULT 'public',
    text        TEXT NOT NULL,
    uploaded_at INTEGER NOT NULL
);

-- ============ 调度与审计 ============
CREATE TABLE IF NOT EXISTS concept_meta (
    key   TEXT PRIMARY KEY,
    value TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS concept_processed (    -- 增量处理登记
    origin      TEXT NOT NULL,
    origin_ref  TEXT NOT NULL,
    content_hash TEXT NOT NULL,
    lexicon_version INTEGER NOT NULL,
    processed_at INTEGER NOT NULL,
    PRIMARY KEY (origin, origin_ref)
);

CREATE TABLE IF NOT EXISTS model_call_log (
    id             INTEGER PRIMARY KEY AUTOINCREMENT,
    task           TEXT NOT NULL,
    provider       TEXT NOT NULL,
    model          TEXT,
    prompt_version TEXT,
    request_hash   TEXT,
    input_tokens   INTEGER,
    output_tokens  INTEGER,
    cost_usd       REAL,
    cached         INTEGER NOT NULL DEFAULT 0,
    at             INTEGER NOT NULL
);
"""

# 追加到既有 ``uploads`` 表的列（语义分析授权，逐行记录，见文档 §8.2）。
UPLOAD_COLUMNS = {
    "semantic_consent": "INTEGER NOT NULL DEFAULT 0",
}


def init(conn: sqlite3.Connection) -> None:
    conn.executescript(SCHEMA)
    existing = {r[1] for r in conn.execute("PRAGMA table_info(uploads)")}
    for name, decl in UPLOAD_COLUMNS.items():
        if name not in existing:
            conn.execute(f"ALTER TABLE uploads ADD COLUMN {name} {decl}")
    # 首次启用：默认 admin 账号自动获得 admin 角色（与 server 首启 admin/admin 引导一致）。
    if conn.execute("SELECT COUNT(*) FROM user_role").fetchone()[0] == 0:
        if conn.execute("SELECT 1 FROM users WHERE username='admin'").fetchone():
            conn.execute("INSERT OR IGNORE INTO user_role(username, role) VALUES('admin','admin')")


def get_meta(conn: sqlite3.Connection, key: str, default: str = "") -> str:
    r = conn.execute("SELECT value FROM concept_meta WHERE key=?", (key,)).fetchone()
    return r[0] if r else default


def set_meta(conn: sqlite3.Connection, key: str, value: str) -> None:
    conn.execute(
        "INSERT INTO concept_meta(key, value) VALUES(?,?) "
        "ON CONFLICT(key) DO UPDATE SET value=excluded.value", (key, str(value)))


def lexicon_version(conn: sqlite3.Connection) -> int:
    try:
        return int(get_meta(conn, "lexicon_version", "1"))
    except ValueError:
        return 1


def bump_lexicon_version(conn: sqlite3.Connection) -> int:
    """术语库任何一次生效变更都递增版本，增量处理据此重算术语层。"""
    v = lexicon_version(conn) + 1
    set_meta(conn, "lexicon_version", str(v))
    return v
