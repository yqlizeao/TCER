"""客户端自动上传与新会话增量筛选测试。"""
from __future__ import annotations

import time
from pathlib import Path
from unittest.mock import MagicMock

import pytest

from tcer.core import metrics, ui_prefs, upload_config
from tcer.core.models import ProjectRef, SessionMeta, SessionReport, TokenUsage
from tcer.gui.app import TcerGui


def _point_prefs(tmp_path, monkeypatch):
    monkeypatch.setattr(ui_prefs, "_prefs_path", lambda: tmp_path / "tcer_ui.json")


def _make_report(sid: str, started_at: int, ended_at: int) -> SessionReport:
    meta = SessionMeta(session_id=sid, cwd="/repo", title=sid, path=Path(f"/repo/{sid}.jsonl"), is_subagent=False)
    u = TokenUsage(started_at=started_at, ended_at=ended_at, input_tokens=100, output_tokens=50)
    return metrics.compute(meta, u, net_loc=10, task_type="code_creation")

def test_report_activity_ms_prefers_usage_timestamps():
    r1 = _make_report("s1", started_at=1_000_000, ended_at=2_000_000)
    assert TcerGui._report_activity_ms(r1) == 2_000_000

    # 2. usage 仅有 started_at
    r2 = _make_report("s2", started_at=1_500_000, ended_at=0)
    assert TcerGui._report_activity_ms(r2) == 1_500_000

    # 3. usage 无时间戳，从 meta 回退
    meta = SessionMeta(session_id="s3", cwd="/repo", title="s3", path=None, is_subagent=False)
    meta.ended_at = 3_000_000  # type: ignore[attr-defined]
    r3 = metrics.compute(meta, TokenUsage(), net_loc=0, task_type="code_creation")
    assert TcerGui._report_activity_ms(r3) == 3_000_000


def test_auto_upload_filters_sessions_by_timestamp(tmp_path, monkeypatch):
    """自动上传按记录的上传时间戳只筛选新会话。"""
    _point_prefs(tmp_path, monkeypatch)

    # 模拟项目中有 3 条会话
    r_old = _make_report("s_old", started_at=1_700_000_000_000, ended_at=1_700_000_100_000)
    r_new1 = _make_report("s_new1", started_at=1_700_003_600_000, ended_at=1_700_003_700_000)
    r_new2 = _make_report("s_new2", started_at=1_700_007_200_000, ended_at=1_700_007_300_000)

    reports = [r_old, r_new1, r_new2]

    last_upload_ts = 1_700_001_000_000
    new_reports = [r for r in reports if TcerGui._report_activity_ms(r) > last_upload_ts]
    assert len(new_reports) == 2
    assert [r.meta.session_id for r in new_reports] == ["s_new1", "s_new2"]
    # 若所有会话都早于 last_upload_ts，则筛选结果为空
    newer_ts = 1_700_010_000_000
    assert [r for r in reports if TcerGui._report_activity_ms(r) > newer_ts] == []


def test_check_auto_upload_triggers_only_when_conditions_met(tmp_path, monkeypatch):
    """验证 _check_auto_upload 的门控逻辑（开启开关、服务器存在、每小时周期）。"""
    _point_prefs(tmp_path, monkeypatch)

    # 1. auto_upload 关闭时不触发
    upload_config.save(url="https://srv", auth_token="tok", detail=True, auto_upload=False)
    app = MagicMock()
    app._auto_upload_running = False
    TcerGui._check_auto_upload(app)
    app._trigger_auto_upload.assert_not_called()

    # 2. 开启 auto_upload，但 server_url 为空时不触发
    upload_config.save(url="", auth_token="tok", detail=True, auto_upload=True)
    TcerGui._check_auto_upload(app)
    app._trigger_auto_upload.assert_not_called()

    # 3. 开启且有 URL，未满 1 小时不触发
    upload_config.save(url="https://srv", auth_token="tok", detail=True, auto_upload=True)
    now = time.time()
    # 模拟上次检查在 10 分钟前
    app._last_auto_check_ts = now - 600
    upload_config.set_last_upload_ts(int((now - 600) * 1000))

    TcerGui._check_auto_upload(app)
    app._trigger_auto_upload.assert_not_called()

    # 4. 满 1 小时（>= 3600 秒）时触发
    app._last_auto_check_ts = now - 3650
    upload_config.set_last_upload_ts(int((now - 3650) * 1000))

    TcerGui._check_auto_upload(app)
    app._trigger_auto_upload.assert_called_once()



def test_auto_upload_polling_only_when_enabled(tmp_path, monkeypatch):
    """确认：开启自动上传才有轮询；未开启时不启动，关闭时停止。"""
    _point_prefs(tmp_path, monkeypatch)

    app = MagicMock()
    app._auto_upload_job = None
    app.root = MagicMock()
    app.root.after.return_value = "after_job_42"

    # 1. 未开启自动上传时，调用 _start_auto_upload_polling 不会向 root 注册定时器
    upload_config.save(url="https://srv", auth_token="tok", detail=True, auto_upload=False)
    TcerGui._start_auto_upload_polling(app, 30_000)
    app.root.after.assert_not_called()
    assert app._auto_upload_job is None

    # 2. 开启自动上传时，注册定时器
    upload_config.save(url="https://srv", auth_token="tok", detail=True, auto_upload=True)
    TcerGui._start_auto_upload_polling(app, 30_000)
    app.root.after.assert_called_once_with(30_000, app._auto_upload_tick)
    assert app._auto_upload_job == "after_job_42"

    # 3. 关闭自动上传时，取消已调度的定时器
    TcerGui._stop_auto_upload_polling(app)
    app.root.after_cancel.assert_called_once_with("after_job_42")
    assert app._auto_upload_job is None


def test_upload_success_bubble_renders_and_dismisses(root):
    """验证自动上传成功提示气泡在指定按钮旁展示，并具备标题和总项共条副文本。"""
    from tcer.gui.popups import UploadSuccessBubble
    import tkinter as tk

    btn = tk.Frame(root, width=44, height=40)
    btn.pack()
    root.update_idletasks()

    bubble = UploadSuccessBubble.show(btn, "自动上传成功", "总 2 个项目，共 15 条", timeout_ms=5000)
    try:
        assert bubble is not None
        assert bubble.winfo_exists()
        # 检查子文本内容包含项目数和记录数
        texts = []
        for child in bubble.winfo_children():
            for sub in child.winfo_children():
                for leaf in sub.winfo_children():
                    if isinstance(leaf, tk.Label):
                        texts.append(leaf.cget("text"))
        assert "自动上传成功" in texts
        assert "总 2 个项目，共 15 条" in texts
    finally:
        if bubble and bubble.winfo_exists():
            bubble.destroy()
