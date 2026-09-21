"""UploadDialog 界面组件测试：目录分组、agent 图标展示、自动上传勾选与交互。"""
from __future__ import annotations

from pathlib import Path
from unittest.mock import MagicMock

import pytest

from tcer.core.models import ProjectRef
from tcer.gui import popups

tk = pytest.importorskip("tkinter")


def test_upload_dialog_grouped_by_directory_and_icons(root):
    """验证 UploadDialog：同目录多个 agent 分在同一组，中括号文字转为图标展示。"""
    # 构造两个工作目录，其中 dir_a 下有 2 个 agent (Claude 和 Codex)，dir_b 下有 1 个 agent (OpenCode)
    p1 = ProjectRef(source="claude", key="claude_proj_1", display_name="proj_alpha",
                    cwd="D:/repos/my_app", path=Path("D:/repos/my_app"))
    p2 = ProjectRef(source="codex", key="codex_proj_1", display_name="proj_alpha",
                    cwd="D:/repos/my_app", path=Path("D:/repos/my_app"))
    p3 = ProjectRef(source="opencode", key="opencode_proj_2", display_name="proj_beta",
                    cwd="D:/repos/other_app", path=Path("D:/repos/other_app"))

    projects = [p1, p2, p3]

    on_upload = MagicMock()
    on_save_prefs = MagicMock()
    on_save_config = MagicMock()

    config = {
        "url": "https://tcer-server.test",
        "auth_token": "secret_tok",
        "detail": True,
        "auto_upload": False,
        "default_url": "",
    }
    prefs = {"last_projects": ["claude:claude_proj_1"]}

    dlg = popups.UploadDialog(
        root,
        prefs=prefs,
        projects=projects,
        default_project=None,
        on_upload=on_upload,
        on_save_prefs=on_save_prefs,
        on_save_config=on_save_config,
        config=config,
    )

    try:
        # 1. 验证目录分组：共有 2 个目录分组
        assert len(dlg._groups) == 2

        # 第 1 组包含 2 个 agent（Claude 与 Codex）
        g1 = dlg._groups[0]
        assert len(g1["items"]) == 2
        g1_sources = [it["source_label"] for it in g1["items"]]
        assert "Claude" in g1_sources
        assert "Codex" in g1_sources

        # 检查图标 key：不再使用裸中括号文字，而是映射到 source_icon key
        assert g1["items"][0]["icon_key"] in ("claude", "ccswitch")
        assert g1["items"][1]["icon_key"] == "codex"

        # 第 2 组包含 1 个 agent
        g2 = dlg._groups[1]
        assert len(g2["items"]) == 1
        assert g2["items"][0]["source_label"] == "OpenCode"
        assert g2["items"][0]["icon_key"] == "opencode"

        # 验证默认收起状态
        assert g1["is_expanded"] is False
        assert g2["is_expanded"] is False
        assert not g1["items_box"].winfo_ismapped()

        # 验证展开与收起切换
        dlg._toggle_collapse(g1)
        assert g1["is_expanded"] is True
        dlg._toggle_collapse(g1)
        assert g1["is_expanded"] is False

        # 2. 验证自动上传勾选框
        assert hasattr(dlg, "_auto_upload_var")
        assert hasattr(dlg, "_auto_chk")
        assert dlg._auto_upload_var.get() is False

        # 切换自动上传勾选框
        dlg._auto_upload_var.set(True)
        dlg._on_toggle_auto_upload()
        # 验证触发了配置与项目保存
        on_save_config.assert_called()
        call_kwargs = on_save_config.call_args[1]
        assert call_kwargs["auto_upload"] is True
        assert call_kwargs["detail"] is True
        assert call_kwargs["url"] == "https://tcer-server.test"

        # 3. 验证全选与清空
        dlg._select_all()
        collected = dlg._collect()["last_projects"]
        assert len(collected) == 3

        dlg._clear_all()
        collected = dlg._collect()["last_projects"]
        assert len(collected) == 0

        # 4. 验证选择单组
        # 4. 验证选择单组与按钮高亮切换
        g1_uids = [it["uid"] for it in g1["items"]]
        # 未选中时，按钮显示为「选本组」
        assert g1["select_btn"].cget("text") == "选本组"
        dlg._toggle_group(g1_uids)
        collected = dlg._collect()["last_projects"]
        assert len(collected) == 2
        assert set(collected) == set(g1_uids)
        # 选中后，按钮切换为「已选」并高亮
        assert g1["select_btn"].cget("text") == "已选"
        from tcer.gui import theme
        assert g1["select_btn"].cget("bg") == theme.ACCENT
        assert g1["select_btn"].cget("fg") == theme.FG_WHITE

        # 再次点击已选组，取消全选，按钮恢复为「选本组」
        dlg._toggle_group(g1_uids)
        assert g1["select_btn"].cget("text") == "选本组"
        # 重新选上
        dlg._toggle_group(g1_uids)

        # 验证路径截断
        long_p = "D:\\very_long_path_prefix\\sub_dir_one\\sub_dir_two\\excessive_project_name_final"
        trunc = dlg._truncate_path(long_p, max_chars=40)
        assert "..." in trunc
        assert len(trunc) <= 40

        # 5. 验证隐藏目录与【显示隐藏】按钮
        assert hasattr(dlg, "_show_hidden_btn")
        # 初始未选中时，按钮样式为普通面板底色
        assert dlg._show_hidden is False
        assert dlg._show_hidden_btn.cget("bg") == theme.PANEL

        # 隐藏 g1 目录
        dlg._hide_group(g1["key"])
        assert g1["key"] in dlg._hidden_keys
        assert not g1["grp_box"].winfo_ismapped()
        assert g1["key"] in dlg._collect()["hidden_dirs"]

        # 点击【显示隐藏】按钮：按钮切换为 ACCENT 高亮，被隐藏的卡片重新显现并标注（已隐藏）
        dlg._toggle_show_hidden()
        assert dlg._show_hidden is True
        assert dlg._show_hidden_btn.cget("bg") == theme.ACCENT
        assert dlg._show_hidden_btn.cget("fg") == theme.FG_WHITE
        assert g1["grp_box"].winfo_ismapped()
        assert g1["hidden_tag"].cget("text") == "（已隐藏）"

        # 取消隐藏 g1
        dlg._unhide_group(g1["key"])
        assert g1["key"] not in dlg._hidden_keys
        assert g1["hidden_tag"].cget("text") == ""

        # 恢复【显示隐藏】状态
        dlg._toggle_show_hidden()
        assert dlg._show_hidden is False
        assert dlg._show_hidden_btn.cget("bg") == theme.PANEL
        # 5. 验证立即上传调用
        dlg._do_upload()
        on_upload.assert_called_once()
        uploaded_prefs = on_upload.call_args[0][0]
        assert set(uploaded_prefs["last_projects"]) == set(g1_uids)
    finally:
        dlg._win.destroy()


def test_upload_dialog_legacy_tuple_format_compatibility(root):
    """验证 UploadDialog 向后兼容旧格式 list[tuple[str, str]]。"""
    projects = [
        ("claude::proj1", "[Claude] proj1"),
        ("codex::proj2", "[Codex] proj2"),
    ]
    dlg = popups.UploadDialog(
        root,
        prefs={"last_projects": ["claude::proj1"]},
        projects=projects,
        default_project=None,
        on_upload=MagicMock(),
        on_save_prefs=MagicMock(),
        on_save_config=MagicMock(),
        config={"url": "", "auth_token": "", "detail": True, "auto_upload": True},
    )
    try:
        assert len(dlg._groups) == 2
        # 验证中括号文本被解析提取出图标 key
        assert dlg._groups[0]["items"][0]["icon_key"] == "claude"
        assert dlg._groups[1]["items"][0]["icon_key"] == "codex"
        assert dlg._auto_upload_var.get() is True
        collected = dlg._collect()["last_projects"]
        assert collected == ["claude::proj1"]
    finally:
        dlg._win.destroy()


def test_upload_dialog_hidden_dirs_retained_on_reopen(root):
    """验证隐藏的目录在重新打开对话框时依然保持隐藏状态。"""
    p1 = ProjectRef(source="claude", key="p1", display_name="p1", cwd="D:/repos/dir_a", path=Path("D:/repos/dir_a"))
    p2 = ProjectRef(source="codex", key="p2", display_name="p2", cwd="D:/repos/dir_b", path=Path("D:/repos/dir_b"))
    projects = [p1, p2]

    saved_prefs = {}
    def on_save_prefs(p):
        saved_prefs.update(p)

    # 1. 打开对话框，并隐藏 dir_a
    dlg1 = popups.UploadDialog(
        root,
        prefs={"last_projects": [], "hidden_dirs": []},
        projects=projects,
        default_project=None,
        on_upload=MagicMock(),
        on_save_prefs=on_save_prefs,
        on_save_config=MagicMock(),
        config={"url": "", "auth_token": "", "detail": True},
    )
    try:
        g1 = dlg1._groups[0]
        dir_a_key = g1["key"]
        dlg1._hide_group(dir_a_key)
        # 验证已触发保存
        assert dir_a_key in saved_prefs["hidden_dirs"]
    finally:
        dlg1._win.destroy()

    # 2. 模拟重启客户端，以保存的 prefs 再次打开
    dlg2 = popups.UploadDialog(
        root,
        prefs=saved_prefs,
        projects=projects,
        default_project=None,
        on_upload=MagicMock(),
        on_save_prefs=MagicMock(),
        on_save_config=MagicMock(),
        config={"url": "", "auth_token": "", "detail": True},
    )
    try:
        # 验证 dir_a 依然处于 hidden_keys，并且 grp_box 默认未 mapped
        assert dir_a_key in dlg2._hidden_keys
        assert not dlg2._groups[0]["grp_box"].winfo_ismapped()
        # 未被隐藏的 dir_b 依然正常显示
        assert dlg2._groups[1]["grp_box"].winfo_ismapped()
    finally:
        dlg2._win.destroy()
