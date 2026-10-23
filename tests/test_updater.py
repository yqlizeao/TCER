"""updater / update_check 补充测试。全程离线：mock urlopen / Popen / subprocess，
不启动进程、不改真实文件（均在 tmp_path 内）。"""
from __future__ import annotations

import io
import json
import os
import sys
from pathlib import Path
from unittest import mock

import pytest

from tcer.core import update_check, updater


@pytest.fixture(autouse=True)
def _frozen(monkeypatch):
    """自更新只在打包发布版（sys.frozen）可用；默认模拟发布版。"""
    monkeypatch.setattr(sys, "frozen", True, raising=False)


# ------------------------------ 公共假对象 ------------------------------------

class _Headers(dict):
    def get(self, k, d=None):
        return super().get(k, d)


class _StreamResp:
    def __init__(self, data: bytes, length="auto"):
        self._buf = io.BytesIO(data)
        h = _Headers()
        if length == "auto":
            h["Content-Length"] = str(len(data))
        elif length is not None:
            h["Content-Length"] = str(length)
        self.headers = h

    def read(self, n=-1):
        return self._buf.read(n)

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


class _PopenRec:
    def __init__(self):
        self.calls = []

    def __call__(self, *args, **kw):
        self.calls.append((args, kw))
        return mock.Mock()


# ---------------------------- asset_for_current_platform ----------------------

WIN = ("TCER-windows-x64.exe", "https://x/win.exe")
MAC = ("TCER-macos-arm64.app.zip", "https://x/mac.zip")
MAC_X64 = ("TCER-macos-x64.app.zip", "https://x/macx.zip")
LNX = ("TCER-linux.tar.gz", "https://x/linux.tgz")


def _fake_os(osname):
    import types
    return types.SimpleNamespace(name=osname, chmod=os.chmod, environ=os.environ)


def _plat(monkeypatch, osname, platform):
    # 只替换 updater 模块内的 os 视图；全局改 os.name 会弄坏 pathlib
    monkeypatch.setattr(updater, "os", _fake_os(osname))
    monkeypatch.setattr(sys, "platform", platform)


def test_asset_windows_picks_exe(monkeypatch):
    _plat(monkeypatch, "nt", "win32")
    assert updater.asset_for_current_platform({"assets": [MAC, WIN, LNX]}) == WIN


def test_asset_windows_case_insensitive_and_first_of_many(monkeypatch):
    _plat(monkeypatch, "nt", "win32")
    a = ("A.EXE", "u1")
    b = ("B.exe", "u2")
    assert updater.asset_for_current_platform({"assets": [a, b]}) == a


def test_asset_mac_requires_macos_arm64(monkeypatch):
    _plat(monkeypatch, "posix", "darwin")
    assert updater.asset_for_current_platform({"assets": [WIN, MAC_X64, MAC]}) == MAC
    assert updater.asset_for_current_platform({"assets": [WIN, MAC_X64]}) is None


def test_asset_missing_or_empty_returns_none(monkeypatch):
    _plat(monkeypatch, "nt", "win32")
    assert updater.asset_for_current_platform({"assets": [MAC, LNX]}) is None
    assert updater.asset_for_current_platform({"assets": []}) is None
    assert updater.asset_for_current_platform({}) is None


def test_asset_linux_always_none(monkeypatch):
    _plat(monkeypatch, "posix", "linux")
    assert updater.asset_for_current_platform({"assets": [WIN, MAC, LNX]}) is None


# ------------------------------ download_target / current_exe -----------------

def test_download_target_windows_beside_exe(monkeypatch, tmp_path):
    exe = tmp_path / "TCER.exe"
    exe.write_bytes(b"")
    monkeypatch.setattr(sys, "executable", str(exe))
    _plat(monkeypatch, "nt", "win32")
    assert updater.current_exe() == exe.resolve()
    assert updater.download_target() == exe.resolve().parent / "TCER.update.exe"


def test_download_target_posix_in_tempdir(monkeypatch, tmp_path):
    monkeypatch.setattr(sys, "executable", str(tmp_path / "x"))
    _plat(monkeypatch, "posix", "linux")
    t = updater.download_target()
    assert t.name == "TCER.update.zip" and t.parent != tmp_path


def test_clean_launch_env_strips_pyinstaller_vars(monkeypatch):
    monkeypatch.setenv("_MEI123", "1")
    monkeypatch.setenv("_PYI_APPLICATION_HOME_DIR", "x")
    monkeypatch.setenv("KEEP_ME", "y")
    env = updater._clean_launch_env()
    assert "KEEP_ME" in env and "_MEI123" not in env
    assert not any(k.startswith("_PYI") for k in env)


# ---------------------------------- download ----------------------------------

def test_download_writes_file_with_progress_and_headers(tmp_path):
    data = b"x" * 100
    dest = tmp_path / "out.bin"
    seen = []
    with mock.patch("urllib.request.urlopen", return_value=_StreamResp(data)) as m:
        updater.download("https://h/a", dest, progress_cb=lambda d, t: seen.append((d, t)),
                         chunk=40)
    req = m.call_args[0][0]
    assert dest.read_bytes() == data
    assert seen == [(40, 100), (80, 100), (100, 100)]
    assert "TCER-Updater" in req.get_header("User-agent")
    assert req.get_header("Accept") == "application/octet-stream"


def test_download_without_content_length_reports_none_total(tmp_path):
    dest = tmp_path / "o"
    seen = []
    with mock.patch("urllib.request.urlopen",
                    return_value=_StreamResp(b"abc", length=None)):
        updater.download("https://h/u", dest, progress_cb=lambda d, t: seen.append((d, t)))
    assert seen == [(3, None)] and dest.read_bytes() == b"abc"


def test_download_network_error_propagates(tmp_path):
    import urllib.error
    with mock.patch("urllib.request.urlopen",
                    side_effect=urllib.error.URLError("down")):
        with pytest.raises(urllib.error.URLError):
            updater.download("https://h/u", tmp_path / "o")
    assert not (tmp_path / "o").exists()


def test_download_truncated_should_raise(tmp_path):
    dest = tmp_path / "o"
    with mock.patch("urllib.request.urlopen",
                    return_value=_StreamResp(b"x" * 10, length=100)):
        with pytest.raises(Exception):
            updater.download("https://h/u", dest)


def test_download_mid_stream_failure_leaves_no_partial(tmp_path):
    class Boom(_StreamResp):
        def read(self, n=-1):
            if self._buf.tell() > 0:
                raise OSError("connection reset")
            return super().read(5)
    dest = tmp_path / "o"
    with mock.patch("urllib.request.urlopen",
                    return_value=Boom(b"x" * 100)):
        with pytest.raises(OSError):
            updater.download("https://h/u", dest)
    assert not dest.exists()


def test_download_truncated_leaves_no_file(tmp_path):
    """截断下载报错，dest 与 .part 都不留（残包绝不能被拿去替换 exe）。"""
    dest = tmp_path / "o"
    with mock.patch("urllib.request.urlopen",
                    return_value=_StreamResp(b"x" * 10, length=100)):
        with pytest.raises(OSError):
            updater.download("https://h/u", dest)
    assert not dest.exists()
    assert not (tmp_path / "o.part").exists()


# --------------------------- Windows bat 生成 ---------------------------------

def _win_apply(monkeypatch, tmp_path, dirname):
    d = tmp_path / dirname
    d.mkdir()
    exe = d / "TCER.exe"
    exe.write_bytes(b"old")
    new = d / "TCER.update.exe"
    new.write_bytes(b"new")
    rec = _PopenRec()
    monkeypatch.setattr(sys, "executable", str(exe))
    _plat(monkeypatch, "nt", "win32")
    monkeypatch.setattr(subprocess_mod(), "Popen", rec)
    updater.apply_and_restart(new)
    bat = exe.resolve().parent / "_tcer_updater.bat"
    return exe.resolve(), new, bat, rec


def subprocess_mod():
    return updater.subprocess


needs_win = pytest.mark.skipif(os.name != "nt", reason="mbcs 编码仅 Windows")


@needs_win
def test_windows_bat_structure_and_popen(monkeypatch, tmp_path):
    exe, new, bat, rec = _win_apply(monkeypatch, tmp_path, "plain")
    text = bat.read_bytes().decode("mbcs")
    assert f'set "EXE={exe}"' in text and f'set "NEW={new}"' in text
    assert 'del "%EXE%"' in text and 'move /Y "%NEW%" "%EXE%"' in text
    assert 'start "" "%EXE%"' in text and 'del "%~f0"' in text
    assert "\r\n" in text and "\n" not in text.replace("\r\n", "")
    (args, kw), = rec.calls
    assert args[0] == ["cmd", "/c", str(bat)]
    assert kw["close_fds"] is True
    assert not any(k.startswith(("_MEI", "_PYI")) for k in kw["env"])
    # 不应真的动 exe / new
    assert exe.read_bytes() == b"old" and new.read_bytes() == b"new"


@needs_win
@pytest.mark.parametrize("dirname", ["with space", "a&b", "中文目录", "x^y(z)", "q!w"])
def test_windows_bat_special_chars_inside_quotes(monkeypatch, tmp_path, dirname):
    exe, new, bat, _ = _win_apply(monkeypatch, tmp_path, dirname)
    text = bat.read_bytes().decode("mbcs")
    # set "VAR=..." 整体在引号内，& ^ ( ) 空格对 cmd 无副作用；路径原样出现
    assert f'set "EXE={exe}"' in text and f'set "NEW={new}"' in text


@needs_win
def test_windows_bat_escapes_percent(monkeypatch, tmp_path):
    """路径含 % 时必须写成 %%，否则 cmd 会按变量展开（如 %PATH%）导致路径错误。"""
    exe, new, bat, _ = _win_apply(monkeypatch, tmp_path, "100%PATH%x")
    text = bat.read_bytes().decode("mbcs")
    assert 'set "EXE=' + str(exe).replace("%", "%%") + '"' in text
    assert 'set "NEW=' + str(new).replace("%", "%%") + '"' in text
    # 变量引用本身（%EXE% / %NEW% / %~f0）不能被误转义
    assert '"%EXE%"' in text and '"%NEW%"' in text and '"%~f0"' in text
    assert "%%EXE%%" not in text


# ------------------------------ apply_and_restart 分派 ------------------------

def test_apply_unsupported_platform_raises(monkeypatch, tmp_path):
    _plat(monkeypatch, "posix", "linux")
    with pytest.raises(RuntimeError, match="unsupported"):
        updater.apply_and_restart(tmp_path / "n")


def test_apply_dispatches_by_platform(monkeypatch, tmp_path):
    calls = []
    monkeypatch.setattr(updater, "_windows_replace", lambda e, n: calls.append("win"))
    monkeypatch.setattr(updater, "_mac_replace", lambda e, n: calls.append("mac"))
    _plat(monkeypatch, "nt", "win32")
    updater.apply_and_restart(tmp_path / "n")
    _plat(monkeypatch, "posix", "darwin")
    updater.apply_and_restart(tmp_path / "n")
    assert calls == ["win", "mac"]


def test_apply_refuses_when_not_frozen(monkeypatch, tmp_path):
    monkeypatch.delattr(sys, "frozen", raising=False)
    monkeypatch.setattr(updater, "_windows_replace", lambda e, n: pytest.fail("不应执行"))
    monkeypatch.setattr(updater, "_mac_replace", lambda e, n: pytest.fail("不应执行"))
    with pytest.raises(RuntimeError):
        updater.apply_and_restart(tmp_path / "n")


# --------------------------------- mac 分支 -----------------------------------

def test_mac_bare_binary_copies_chmods_and_launches(monkeypatch, tmp_path):
    exe = tmp_path / "bin" / "TCER"
    exe.parent.mkdir()
    exe.write_bytes(b"old")
    new = tmp_path / "new.bin"
    new.write_bytes(b"new")
    run, popen = mock.Mock(), _PopenRec()
    monkeypatch.setattr(updater.subprocess, "run", run)
    monkeypatch.setattr(updater.subprocess, "Popen", popen)
    updater._mac_replace(exe, new)
    assert exe.read_bytes() == b"new"
    if os.name != "nt":
        assert exe.stat().st_mode & 0o777 == 0o755
    assert run.call_args[0][0][:3] == ["xattr", "-d", "com.apple.quarantine"]
    (args, kw), = popen.calls
    assert args[0] == [str(exe)] and kw["close_fds"] is True


def test_mac_xattr_missing_is_tolerated(monkeypatch, tmp_path):
    exe = tmp_path / "q"
    exe.write_bytes(b"o")
    new = tmp_path / "n"
    new.write_bytes(b"n")

    def no_xattr(*a, **k):
        raise FileNotFoundError("xattr")
    monkeypatch.setattr(updater.subprocess, "run", no_xattr)
    popen = _PopenRec()
    monkeypatch.setattr(updater.subprocess, "Popen", popen)
    updater._mac_replace(exe, new)
    assert len(popen.calls) == 1


def _bundle(tmp_path):
    app = tmp_path / "Apps" / "TCER.app"
    macos = app / "Contents" / "MacOS"
    macos.mkdir(parents=True)
    exe = macos / "TCER"
    exe.write_bytes(b"old")
    z = tmp_path / "TCER.update.zip"
    z.write_bytes(b"zip")
    return app, exe, z


def test_mac_bundle_replace_flow(monkeypatch, tmp_path):
    app, exe, z = _bundle(tmp_path)
    (app.parent / "TCER.app.old").mkdir()           # 残留 .old 应被清理
    (app.parent / "TCER.app.old" / "junk").write_text("j")
    runs = []

    def fake_run(cmd, **kw):
        runs.append(cmd)
        if cmd[0] == "ditto":  # 模拟解压：在临时目录放一个新 .app
            out = Path(cmd[-1]) / "TCER.app" / "Contents" / "MacOS"
            out.mkdir(parents=True)
            (out / "TCER").write_bytes(b"NEW")
        return mock.Mock()
    popen = _PopenRec()
    monkeypatch.setattr(updater.subprocess, "run", fake_run)
    monkeypatch.setattr(updater.subprocess, "Popen", popen)
    updater._mac_replace(exe.resolve(), z)
    assert (app / "Contents" / "MacOS" / "TCER").read_bytes() == b"NEW"
    old = app.parent / "TCER.app.old"
    assert (old / "Contents" / "MacOS" / "TCER").read_bytes() == b"old"
    assert not (old / "junk").exists()
    assert runs[0][:3] == ["ditto", "-x", "-k"]
    assert runs[1][:3] == ["xattr", "-dr", "com.apple.quarantine"]
    (args, _), = popen.calls
    assert args[0][:2] == ["open", "-n"]


def test_mac_bundle_zip_without_app_raises_and_keeps_old(monkeypatch, tmp_path):
    app, exe, z = _bundle(tmp_path)
    monkeypatch.setattr(updater.subprocess, "run", lambda *a, **k: mock.Mock())
    monkeypatch.setattr(updater.subprocess, "Popen", _PopenRec())
    with pytest.raises(RuntimeError, match="未找到"):
        updater._mac_replace(exe.resolve(), z)
    assert exe.read_bytes() == b"old"


def test_mac_in_bundle_non_zip_falls_back_to_bare_copy(monkeypatch, tmp_path):
    app, exe, _ = _bundle(tmp_path)
    new = tmp_path / "raw.bin"
    new.write_bytes(b"RAW")
    monkeypatch.setattr(updater.subprocess, "run", lambda *a, **k: mock.Mock())
    monkeypatch.setattr(updater.subprocess, "Popen", _PopenRec())
    updater._mac_replace(exe.resolve(), new)
    assert exe.read_bytes() == b"RAW"


# ------------------------------ update_check 补充 -----------------------------

class _Resp:
    def __init__(self, payload):
        self._p = payload if isinstance(payload, bytes) else payload.encode()

    def read(self):
        return self._p

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


def test_latest_release_request_headers_and_timeout():
    with mock.patch("urllib.request.urlopen",
                    return_value=_Resp(json.dumps({"tag_name": "v9"}))) as m:
        r = update_check.latest_release()
    req = m.call_args[0][0]
    assert req.full_url.startswith("https://api.github.com/repos/")
    assert req.get_header("User-agent").startswith("TCER/")
    assert m.call_args[1]["timeout"] == update_check._TIMEOUT
    assert r["assets"] == [] and r["notes"] == "" and r["version"] == (9,)


@pytest.mark.parametrize("effect", [
    OSError("down"), TimeoutError("t"), ValueError("v"),
])
def test_latest_release_swallows_network_errors(effect):
    with mock.patch("urllib.request.urlopen", side_effect=effect):
        assert update_check.latest_release() is None


@pytest.mark.parametrize("body", [b"<html>", b"[]", b'{"tag_name": "  "}', b"{}"])
def test_latest_release_bad_bodies(body):
    """非 JSON / 缺 tag 返回 None。注意 b"[]" 会触发 AttributeError（见报告）。"""
    with mock.patch("urllib.request.urlopen", return_value=_Resp(body)):
        try:
            assert update_check.latest_release() is None
        except AttributeError:
            pytest.fail("顶层非 dict 的 JSON 响应必须静默返回 None")


def test_latest_release_null_fields_tolerated():
    body = json.dumps({"tag_name": "v1", "html_url": None, "body": None,
                       "assets": [{"name": None, "browser_download_url": None}]})
    with mock.patch("urllib.request.urlopen", return_value=_Resp(body)):
        r = update_check.latest_release()
    assert r["url"] == "" and r["notes"] == "" and r["assets"] == [("", "")]


def test_render_notes_basic_markdown():
    md = "## 标题\n\n- **粗** 项 `code`\n* 星\n> 引用\n\n\n\n正文 *斜*"
    out = update_check.render_notes(md)
    assert out == "标题\n\n• 粗 项 code\n• 星\n引用\n\n正文 斜"
    assert update_check.render_notes("") == "" and update_check.render_notes(None) == ""


def test_is_newer_default_local_uses_package_version(monkeypatch):
    monkeypatch.setattr(update_check, "__version__", "1.0.0")
    assert update_check.is_newer("1.0.1") is True
    assert update_check.is_newer("1.0.0") is False
