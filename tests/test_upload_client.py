"""upload_client 隐私/安全语义测试。全程 mock urlopen，零联网。"""
from __future__ import annotations

import io
import json
import urllib.error
from pathlib import Path
from unittest import mock

import pytest

from tcer.core import metrics, upload_client as uc
from tcer.core.models import SessionMeta, TokenUsage

USER = "zhangsan_secret"
HOST = "DESKTOP-SECRETHOST"
HOME = r"C:\Users\zhangsan_secret"


def _report(sid="s1", *, source="claude", path=None, session_id=None, texts=None,
            title=None, git_repo=None):
    meta = SessionMeta(
        session_id=sid if session_id is None else session_id,
        cwd=HOME + r"\proj", title=title or f"修 {USER} 的 bug",
        path=Path(path or (HOME + rf"\.claude\projects\x\{sid}.jsonl")),
        is_subagent=False, source=source, git_repository=git_repo,
    )
    u = TokenUsage(input_tokens=1000, output_tokens=1000)
    if texts:
        u.user_message_texts = list(texts)
    return metrics.compute(meta, u, net_loc=100, task_type="feature")


class _Resp:
    def __init__(self, body=b"{}"):
        self._b = body

    def read(self):
        return self._b

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


def _http_error(code, body=b""):
    return urllib.error.HTTPError("http://x", code, "err", {}, io.BytesIO(body))


def _build(reports, **kw):
    agg = _report("agg")
    args = dict(aggregate=agg, reports=reports, n_sessions=len(reports),
                project="projA", user=USER, anonymous=True, detail=False)
    args.update(kw)
    return uc.build_payload(**args)


# --------------------------- anon_label / _anon_seed --------------------------

def test_anon_label_stable_prefixed_and_hides_name():
    a = uc.anon_label(USER)
    assert a.startswith("匿名-") and a == uc.anon_label(USER.upper())
    assert USER not in a
    assert uc.anon_label(None) == uc.anon_label("  ")


def test_anon_seed_fallbacks(monkeypatch):
    import getpass
    import socket

    def boom(*a):
        raise OSError()
    monkeypatch.setattr(getpass, "getuser", boom)
    monkeypatch.setattr(socket, "gethostname", lambda: "h")
    assert uc._anon_seed() == "h"
    monkeypatch.setattr(socket, "gethostname", boom)
    assert uc._anon_seed() == "anonymous"


# ------------------------------ _scrub_identifying ----------------------------

def test_scrub_replaces_title_path_cwd():
    row = {"session_id": "abc", "title": USER, "path": HOME, "cwd": HOME, "x": 1}
    uc._scrub_identifying(row, "P")
    assert row == {"session_id": "abc", "title": "P-abc", "path": "", "cwd": "", "x": 1}


def test_scrub_missing_session_id_uses_placeholder():
    row = {"session_id": None}
    uc._scrub_identifying(row, "P")
    assert row["title"] == "P-session"


# ------------------------------- build_payload --------------------------------

def test_anonymous_payload_has_no_identifying_strings(monkeypatch):
    monkeypatch.setattr(uc, "_lazy_user_messages",
                        lambda r: pytest.fail("detail=False 不应读消息"))
    monkeypatch.setattr(uc, "_read_conversation",
                        lambda r: pytest.fail("detail=False 不应读对话"))
    reports = [_report("s1"), _report("s2")]
    seed = f"{USER}@{HOST}"
    p = _build(reports, user=seed)
    assert p["user"].startswith("匿名-") and seed not in p["user"]
    assert p["anonymous"] is True and p["detail"] is False
    assert "user_messages" not in p["sessions"][0]
    assert "conversation" not in p["sessions"][0]
    blob = json.dumps(p, ensure_ascii=False)
    blob2 = json.dumps(p)  # ASCII 转义形态也要查
    for secret in (USER, HOST, "zhangsan_secret", "Users"):
        assert secret not in blob and secret not in blob2, secret
    for row in p["sessions"] + [p["aggregate"]]:
        assert row["path"] == "" and row["cwd"] == ""
        assert row["title"].startswith("projA-")


def test_anonymous_aggregate_row_scrubbed_and_counted():
    p = _build([_report("s1")], n_sessions=7)
    assert p["aggregate"]["sessions_counted"] == 7
    assert p["aggregate"]["title"] == "projA-agg"


def test_named_without_detail_keeps_local_fields():
    p = _build([_report("s1")], anonymous=False, detail=False)
    assert p["user"] == USER
    assert USER in p["sessions"][0]["title"]
    assert p["sessions"][0]["path"].endswith("s1.jsonl")


def test_detail_anonymous_does_not_scrub_documented_behavior(monkeypatch):
    """文档约定：detail=True 时匿名也不脱敏 title/path/cwd（路径含用户名会外发）。"""
    monkeypatch.setattr(uc, "_read_conversation", lambda r: [{"role": "user"}])
    p = _build([_report("s1", texts=["hi"])], anonymous=True, detail=True)
    row = p["sessions"][0]
    assert "zhangsan_secret" in row["path"] and "zhangsan_secret" in row["cwd"]
    assert USER in row["title"]
    assert row["user_messages"] == ["hi"] and row["conversation"] == [{"role": "user"}]
    assert p["user"].startswith("匿名-")


def test_git_repository_scrubbed_for_anonymous():
    """匿名（无 detail）上传：git 远端 URL / 分支同样脱敏（远端 URL 常含用户名）。"""
    r = _report("s1", git_repo=f"https://github.com/{USER}/secret.git")
    p = _build([r], anonymous=True, detail=False)
    assert USER not in json.dumps(p, ensure_ascii=False)
    assert p["sessions"][0]["git_repository"] == ""


def test_detail_falls_back_to_lazy_messages(monkeypatch):
    monkeypatch.setattr(uc, "_lazy_user_messages", lambda r: ["lazy"])
    monkeypatch.setattr(uc, "_read_conversation", lambda r: [])
    p = _build([_report("s1")], anonymous=False, detail=True)
    assert p["sessions"][0]["user_messages"] == ["lazy"]


def test_semantic_consent_only_named_and_detail(monkeypatch):
    monkeypatch.setattr(uc, "_lazy_user_messages", lambda r: [])
    monkeypatch.setattr(uc, "_read_conversation", lambda r: [])
    rs = [_report("s1")]

    def f(**kw):
        return _build(rs, semantic_consent=True, **kw)["semantic_consent"]
    assert f(anonymous=False, detail=True) is True
    assert f(anonymous=True, detail=True) is False
    assert f(anonymous=False, detail=False) is False


def test_no_reports_no_sessions_key():
    p = _build([])
    assert "sessions" not in p and "aggregate" in p


# ------------------------------- _post_json -----------------------------------

def test_post_json_token_only_in_header_and_no_log(capsys):
    with mock.patch("urllib.request.urlopen", return_value=_Resp(b'{"ok":1}')) as m:
        out = uc._post_json("http://h/api/x", {"a": "中文"}, token="TOK123")
    req = m.call_args[0][0]
    assert out == {"ok": 1}
    assert req.get_header("Authorization") == "Bearer TOK123"
    assert b"TOK123" not in req.data and "中文".encode() in req.data
    cap = capsys.readouterr()
    assert "TOK123" not in cap.out + cap.err
    assert m.call_args[1]["timeout"] == 30.0


def test_post_json_no_token_no_auth_header():
    with mock.patch("urllib.request.urlopen", return_value=_Resp()) as m:
        assert uc._post_json("http://h", {}) == {}
    assert m.call_args[0][0].get_header("Authorization") is None


def test_post_json_http_error_with_detail():
    err = _http_error(403, b'{"error": "forbidden"}')
    with mock.patch("urllib.request.urlopen", side_effect=err):
        with pytest.raises(uc.UploadError, match="HTTP 403: forbidden"):
            uc._post_json("http://h", {})


@pytest.mark.parametrize("code,body", [(500, b"<html>"), (401, b"")])
def test_post_json_http_error_without_json_body(code, body):
    with mock.patch("urllib.request.urlopen", side_effect=_http_error(code, body)):
        with pytest.raises(uc.UploadError) as ei:
            uc._post_json("http://h", {})
    assert str(ei.value) == f"HTTP {code}"


def test_post_json_url_error_and_timeout():
    with mock.patch("urllib.request.urlopen",
                    side_effect=urllib.error.URLError("refused")):
        with pytest.raises(uc.UploadError, match="无法连接服务器"):
            uc._post_json("http://h", {})
    with mock.patch("urllib.request.urlopen",
                    side_effect=urllib.error.URLError(TimeoutError("timed out"))):
        with pytest.raises(uc.UploadError, match="timed out"):
            uc._post_json("http://h", {})


def test_post_json_bare_timeout_translated():
    """读取阶段裸 TimeoutError（非 URLError）也转成 UploadError，GUI 只需接一种异常。"""
    with mock.patch("urllib.request.urlopen", side_effect=TimeoutError("read timeout")):
        with pytest.raises(uc.UploadError):
            uc._post_json("http://h", {})


def test_post_json_non_json_response():
    with mock.patch("urllib.request.urlopen", return_value=_Resp(b"<html>")):
        with pytest.raises(uc.UploadError, match="无法解析"):
            uc._post_json("http://h", {})


# ------------------------- login / upload / token_upload ----------------------

def test_login_ok_strips_trailing_slash():
    with mock.patch("urllib.request.urlopen",
                    return_value=_Resp(b'{"token": "T"}')) as m:
        assert uc.login("http://h/", "u", "p") == "T"
    assert m.call_args[0][0].full_url == "http://h/api/login"
    assert json.loads(m.call_args[0][0].data) == {"username": "u", "password": "p"}


def test_login_missing_token():
    with mock.patch("urllib.request.urlopen", return_value=_Resp(b"{}")):
        with pytest.raises(uc.UploadError, match="未返回令牌"):
            uc.login("http://h", "u", "p")


def test_upload_returns_inserted_and_defaults_zero():
    with mock.patch("urllib.request.urlopen",
                    return_value=_Resp(b'{"inserted": 3}')) as m:
        assert uc.upload("http://h", "T", {"a": 1}) == 3
    assert m.call_args[0][0].full_url == "http://h/api/upload"
    with mock.patch("urllib.request.urlopen", return_value=_Resp(b"{}")):
        assert uc.upload("http://h", "T", {}) == 0


def test_token_upload_with_token_is_named_user_none():
    with mock.patch("urllib.request.urlopen",
                    return_value=_Resp(b'{"inserted": 1}')) as m:
        n = uc.token_upload(server_url="http://h", auth_token="SECRET_TOK",
                            aggregate=_report("agg"), reports=[_report("s1")],
                            n_sessions=1, project="P", detail=False)
    req = m.call_args[0][0]
    body = json.loads(req.data)
    assert n == 1 and body["anonymous"] is False and body["user"] is None
    assert req.get_header("Authorization") == "Bearer SECRET_TOK"
    assert "SECRET_TOK" not in req.data.decode()


def test_token_upload_anonymous_no_auth_and_pseudonym(monkeypatch):
    monkeypatch.setattr(uc, "_anon_seed", lambda: f"{USER}@{HOST}")
    with mock.patch("urllib.request.urlopen",
                    return_value=_Resp(b'{"inserted": 2}')) as m:
        n = uc.token_upload(server_url="http://h", auth_token=None,
                            aggregate=_report("agg"), reports=[_report("s1")],
                            n_sessions=1, project="P", detail=False)
    req = m.call_args[0][0]
    raw = req.data.decode("utf-8")
    assert n == 2 and req.get_header("Authorization") is None
    assert json.loads(raw)["user"].startswith("匿名-")
    for secret in (USER, HOST, "zhangsan_secret"):
        assert secret not in raw


# ------------------- _read_conversation / _lazy_user_messages -----------------

def _patch_readers(monkeypatch):
    """每个 reader 返回可辨认标记，用以检查分派。"""
    from tcer.core import (antigravity_reader, codex_reader, omp_reader,
                           opencode_reader, pi_reader, reader, workbuddy_reader)
    calls = []
    for mod in (antigravity_reader, codex_reader, omp_reader, pi_reader,
                workbuddy_reader, reader):
        name = mod.__name__.rsplit(".", 1)[-1]
        monkeypatch.setattr(mod, "read_conversation",
                            lambda p, n=name: calls.append((n, Path(p))) or [{"src": n}])
        monkeypatch.setattr(mod, "read_user_messages",
                            lambda p, n=name: calls.append((n, Path(p))) or [f"um-{n}"])
    monkeypatch.setattr(opencode_reader, "read_conversation",
                        lambda db, sid: calls.append(("opencode", (Path(db), sid)))
                        or [{"src": "opencode"}])
    monkeypatch.setattr(opencode_reader, "read_user_messages",
                        lambda db, sid: calls.append(("opencode", (Path(db), sid)))
                        or ["um-opencode"])
    return calls


@pytest.mark.parametrize("source,mod", [
    ("codex", "codex_reader"), ("omp", "omp_reader"), ("pi", "pi_reader"),
    ("antigravity", "antigravity_reader"), ("workbuddy", "workbuddy_reader"),
    ("codebuddy", "workbuddy_reader"),
])
def test_dispatch_simple_sources(monkeypatch, tmp_path, source, mod):
    calls = _patch_readers(monkeypatch)
    f = tmp_path / "a.jsonl"
    f.write_text("")
    r = _report("s", source=source, path=str(f))
    assert uc._read_conversation(r) == [{"src": mod}]
    assert uc._lazy_user_messages(r) == [f"um-{mod}"]
    assert calls[0] == (mod, f)


def test_dispatch_opencode(monkeypatch, tmp_path):
    calls = _patch_readers(monkeypatch)
    db = tmp_path / "oc.db"
    r = _report("s", source="opencode", path=str(db), session_id="ses_1")
    assert uc._read_conversation(r) == [{"src": "opencode"}]
    assert uc._lazy_user_messages(r) == ["um-opencode"]
    assert calls[0] == ("opencode", (db, "ses_1"))


def test_opencode_without_session_id_returns_empty(monkeypatch, tmp_path):
    _patch_readers(monkeypatch)
    r = _report("s", source="opencode", path=str(tmp_path / "d"))
    r.meta.session_id = None
    assert uc._read_conversation(r) == []


def test_read_conversation_swallows_errors(monkeypatch, tmp_path):
    from tcer.core import codex_reader, opencode_reader

    def boom(*a):
        raise ValueError("bad")
    monkeypatch.setattr(codex_reader, "read_conversation", boom)
    monkeypatch.setattr(opencode_reader, "read_conversation", boom)
    monkeypatch.setattr(codex_reader, "read_user_messages", boom)
    assert uc._read_conversation(_report("s", source="codex", path=str(tmp_path / "a"))) == []
    assert uc._read_conversation(_report("s", source="opencode", path=str(tmp_path / "a"),
                                         session_id="x")) == []
    assert uc._lazy_user_messages(_report("s", source="codex", path=str(tmp_path / "a"))) == []


def test_claude_subagent_prompt_is_not_a_user_message(tmp_path):
    """子代理过程照常上传，但派发 prompt 不算用户消息（口径 SSOT user_input）。"""
    main = tmp_path / "sid1.jsonl"
    sub_dir = tmp_path / "sid1" / "subagents"
    sub_dir.mkdir(parents=True)

    def line(t, **kw):
        return json.dumps({"type": "user", "uuid": t, "sessionId": "sid1",
                           "message": {"role": "user", "content": t}, **kw})
    sub_reply = json.dumps({"type": "assistant", "isSidechain": True, "message": {
        "role": "assistant", "content": [{"type": "text", "text": "sub reply"}]}})
    main.write_text(line("main msg") + "\n", encoding="utf-8")
    (sub_dir / "agent-1.jsonl").write_text(
        line("sub prompt", isSidechain=True) + "\n" + sub_reply + "\n",
        encoding="utf-8")
    r = _report("sid1", path=str(main))
    r.meta.session_paths = [main, sub_dir / "agent-1.jsonl"]
    convo = uc._read_conversation(r)
    users = [b["text"] for b in convo if b["role"] == "user"]
    assert users == ["main msg"]
    assert any(b.get("text") == "sub reply" for b in convo)
    assert uc._lazy_user_messages(r) == ["main msg"]


def test_claude_conversation_uses_session_paths(monkeypatch, tmp_path):
    from tcer.core import reader
    seen = []
    monkeypatch.setattr(reader, "read_conversation",
                        lambda p: seen.append(Path(p)) or [{"p": str(p)}])
    a, b = tmp_path / "a.jsonl", tmp_path / "b.jsonl"
    r = _report("s", path=str(a))
    r.meta.session_paths = [a, b]
    assert len(uc._read_conversation(r)) == 2 and seen == [a, b]


def test_lazy_messages_none_path_returns_empty():
    r = _report("s")
    r.meta.path = None
    assert uc._lazy_user_messages(r) == []
