"""术语库 GUI 端到端闭环测试（完整 TcerGui + 真实调用链,全离线 mock）。

背景:v1.9.4 首版交付时自测 740 项全绿,但术语考古入口调用了不存在的
``llm_prefs.is_enabled()``、remote 路径调用 ``llm_client.chat_completion``、
``build_ambiguity_payload`` 参数顺序颠倒、worker 线程裸跨线程 ``after``——
全部入口级崩溃。根因:仅组件级测试,从未构建完整 TcerGui 走真实调用链。
本文件钉死这些路径:GUI 接线、CRUD 落盘、三个功能的 remote(mock 引擎)与
离线降级全链。网络层一律 mock,CI 零联网;无显示环境自动 skip。
"""
from __future__ import annotations

import json
import time
from pathlib import Path
from unittest import mock

import pytest

tk = pytest.importorskip("tkinter")

from tcer.core import llm_prefs, llm_reports, ui_prefs  # noqa: E402


def _drain(root, seconds: float = 3.0, until=None):
    """泵事件循环消化 worker 回填（60ms 轮询器在主线程执行）。"""
    deadline = time.time() + seconds
    while time.time() < deadline:
        root.update()
        time.sleep(0.02)
        if until is not None and until():
            break


@pytest.fixture()
def isolated(tmp_path, monkeypatch):
    """全部偏好/报告/词条库重定向到 tmp,并确保「未配置任何 key」。"""
    monkeypatch.setattr(ui_prefs, "_prefs_path", lambda: tmp_path / "tcer_ui.json")
    monkeypatch.setattr(llm_prefs, "_prefs_path", lambda: tmp_path / "tcer_llm.json")
    monkeypatch.setattr(llm_reports, "_path", lambda: tmp_path / "tcer_llm_reports.json")
    tb_path = tmp_path / "termbase.json"
    ui_prefs.set_termbase_path(str(tb_path))
    llm_prefs.save({})
    return tb_path


@pytest.fixture()
def app(root, isolated, monkeypatch):
    from tcer.gui.app import TcerGui
    monkeypatch.setattr(TcerGui, "refresh_projects", lambda self: None)
    gui = TcerGui(root)
    yield gui
    for w in list(root.winfo_children()):
        try:
            w.destroy()
        except tk.TclError:
            pass


@pytest.fixture()
def report():
    from tcer.core import metrics
    from tcer.core.models import SessionMeta, TokenUsage
    meta = SessionMeta(session_id="tb-gui-1", cwd="C:/x", title="术语闭环",
                       path=Path("C:/x/tb-gui-1.jsonl"), is_subagent=False)
    u = TokenUsage(input_tokens=1000, output_tokens=500, models={"m"},
                   assistant_msgs=2, user_msgs=2)
    return metrics.compute(meta, u, net_loc=10, task_type="feature")


def _msg_box(monkeypatch):
    import tkinter.messagebox as mb
    monkeypatch.setattr(mb, "askyesno", lambda *a, **k: True)
    monkeypatch.setattr(mb, "showinfo", lambda *a, **k: None)
    monkeypatch.setattr(mb, "showwarning", lambda *a, **k: None)
    monkeypatch.setattr(mb, "showerror", lambda *a, **k: None)
    return mb


# ---------------------------------------------------------------- 接线与 CRUD

def test_terms_report_empty_termbase_guidance():
    """空词条库 + 离线降级时报告必须给出可行动指引(否则通篇「命中 0/跳过」
    等于白跑,用户实测「处理完了但没结果」)。"""
    from tcer.core import llm_prompts
    text, _ = llm_prompts.format_terms_report(
        [], None, total_messages_sampled=3, is_degraded=True,
        degraded_reason="未配置通用 LLM 或 TypeSafe API Key，已跳过新词自动挖掘",
        has_terms=False)
    assert "词条库为空" in text
    assert "重新考古" in text


def test_tab_switch_and_crud_roundtrip(root, app, isolated):
    """第 7 页签接线 + 新建→编辑→保存→落盘→重载→搜索 全闭环。"""
    app._nb.select(6)
    root.update()
    assert app._nb.index(app._nb.select()) == 6
    view = app.termbase_view
    assert view._termbase is not None

    view._create_new_term()
    root.update()
    view._f_pref.delete(0, "end"); view._f_pref.insert(0, "客户端预测")
    view._f_en.delete(0, "end"); view._f_en.insert(0, "client-side prediction")
    view._f_alts.delete(0, "end"); view._f_alts.insert(0, "预表现, 抢跑")
    view._f_def.insert("1.0", "本地先按假设模拟并立刻呈现,服务器权威结果到达后校正。")
    assert view._save_current_entry() is True

    data = json.loads(isolated.read_text(encoding="utf-8"))
    assert any(t["pref_label"] == "客户端预测" for t in data["terms"])

    view.on_show()
    root.update()
    assert any(t.pref_label == "客户端预测" for t in view._termbase.terms)

    view._search_var.set("客户端")
    view._on_search_changed()
    root.update()
    assert len(view._cards) == 1


def test_switch_to_termbase_navigation(root, app):
    app.switch_to_termbase()
    root.update()
    assert app._nb.index(app._nb.select()) == 6


def test_import_popup_single_entry(root, app, isolated, monkeypatch):
    """导入弹窗:单词条 JSON → 确认 → 落盘(钉 save_termbase 路径一致性)。"""
    from tcer.gui.popups import TermImportPopup
    _msg_box(monkeypatch)
    frag = {"slug": "door-problem", "pref_label": "门的问题",
            "definition": "同一扇门,各职能看到完全不同的工作项。", "status": "active"}
    p = TermImportPopup(root, on_imported=None)
    p._text.insert("1.0", json.dumps(frag, ensure_ascii=False))
    p._do_import()
    root.update()
    data = json.loads(isolated.read_text(encoding="utf-8"))
    assert any(t["slug"] == "door-problem" for t in data["terms"])


def test_import_from_report_extracts_all_blocks(root, app, isolated, monkeypatch):
    """考古报告合入必须全量提取多个 ```json 提案块——曾用 re.search 非贪婪
    只取第一块,12 份提案只载入 1 份(用户实测「怎么就生成了一个词汇」);
    且 load() 新→旧排序应取 [0] 最新报告(曾取 [-1] 最旧)。"""
    import tkinter.messagebox as mb
    from tcer.core import llm_reports
    from tcer.gui.popups import TermImportPopup

    def report(kind, created_at, blocks):
        body = "# 术语考古分析报告\n\n"
        for b in blocks:
            body += "```json\n" + json.dumps(b, ensure_ascii=False) + "\n```\n"
        return {"id": f"r{created_at}", "kind": kind, "title": f"报告{created_at}",
                "text": body, "created_at": created_at}

    # 两份 terms 报告:[0] 最新(2 块)、[-1] 最旧(1 块)——提取必须来自最新
    monkeypatch.setattr(llm_reports, "load", lambda: [
        report("terms", 2000, [
            {"slug": "vgspatial", "pref_label": "VgSpatial", "definition": "d1", "status": "draft"},
            {"slug": "vgdev", "pref_label": "VgDev", "definition": "d2", "status": "draft"},
        ]),
        report("terms", 1000, [
            {"slug": "old-term", "pref_label": "旧词", "definition": "old", "status": "draft"},
        ]),
    ])
    _msg_box(monkeypatch)
    p = TermImportPopup(root, on_imported=None)
    p._load_from_latest_report()
    content = p._text.get("1.0", "end")
    p._win.destroy()
    assert "vgspatial" in content and "vgdev" in content, "多块提案未全量载入"
    assert "old-term" not in content, "载入了最旧报告而非最新"

    # 导入确认清单 → 两条全部落盘
    p2 = TermImportPopup(root, on_imported=None)
    p2._load_from_latest_report()
    p2._do_import()
    root.update()
    p2._win.destroy()
    data = json.loads(isolated.read_text(encoding="utf-8"))
    slugs = {t["slug"] for t in data["terms"]}
    assert {"vgspatial", "vgdev"} <= slugs, f"合入不全: {slugs}"


# ---------------------------------------------------------------- F3 反向查询

def test_lookup_local_hit(root, app):
    from tcer.core.termbase import TermEntry
    from tcer.gui.popups import TermLookupPopup
    terms = [TermEntry(slug="cp", pref_label="客户端预测", definition="d", status="active")]
    tb = type("T", (), {"terms": terms, "errors": [],
                        "active_terms": lambda s: terms,
                        "by_slug": lambda s, k: terms[0] if k == "cp" else None})()
    p = TermLookupPopup(root, controller=None, termbase=tb)
    p._query_var.set("客户端预测是什么意思")
    p.do_search()
    root.update()
    status = p._status_lbl.cget("text") or ""
    p._win.destroy()
    assert "命中" in status
    assert status  # 本地命中不走网络


def test_lookup_remote_mocked(root, app, isolated, monkeypatch):
    """remote 兜底全链(mock evaluate):钉 evaluate 必传 api_key/base_url/model、
    worker 队列回填(裸跨线程 after 在无 mainloop 时抛 RuntimeError 静默死线程)。"""
    import tkinter.messagebox as mb
    from tcer.core import typesafe_client
    from tcer.core.termbase import TermEntry
    from tcer.gui.popups import TermLookupPopup

    llm_prefs.save({"base_url": "https://x.example", "model": "g-test",
                    "typesafe_key": "k", "typesafe_base_url": "https://ts.example",
                    "typesafe_model": "jev-test"})
    seen = {}

    def fake_eval(state, questions, *, api_key, base_url, model, **kw):
        seen.update(api_key=api_key, base_url=base_url, model=model)
        return {"answers": {"is_term": {"noul": 0.9},
                            "which_term": {"choice": "cp", "confidence": 0.82,
                                           "probabilities": {"cp": 0.82}}}}

    monkeypatch.setattr(typesafe_client, "evaluate", fake_eval)
    monkeypatch.setattr(mb, "askyesno", lambda *a, **k: True)

    terms = [TermEntry(slug="cp", pref_label="客户端预测", definition="d", status="active")]
    tb = type("T", (), {"terms": terms, "errors": [],
                        "active_terms": lambda s: terms,
                        "by_slug": lambda s, k: terms[0] if k == "cp" else None})()
    p = TermLookupPopup(root, controller=None, termbase=tb)
    p._query_var.set("完全不存在的黑话zzz")
    p.do_search()
    _drain(root, 4.0, lambda: p._btn_search.cget("state") == "normal")
    status = p._status_lbl.cget("text") or ""
    p._win.destroy()

    assert seen.get("api_key") == "k" and seen.get("model") == "jev-test", \
        "evaluate 调用形状错误(曾缺必填 api_key 导致 TypeError)"
    assert seen.get("base_url") == "https://ts.example"
    assert "吻合" in status, f"remote 回填未到达(曾因跨线程 after 静默死线程): {status}"


# ---------------------------------------------------------------- F4 歧义检测

def _tb_with_misconceptions():
    from tcer.core.termbase import TermEntry
    terms = [TermEntry(slug="cp", pref_label="客户端预测", definition="d", status="active",
                       misconceptions=[{"role": "art", "wrong": "回跳是动画问题",
                                        "actual": "回滚阈值改动"}])]
    return type("T", (), {"terms": terms, "errors": [],
                          "active_terms": lambda s: terms,
                          "by_slug": lambda s, k: terms[0] if k == "cp" else None})()


def test_ambiguity_degraded_offline(root, app, monkeypatch):
    """未配置 key + 词条含误解 → 本地降级报告渲染(钉 ui_icon NameError)。"""
    from tcer.gui.popups import AmbiguityDetectPopup
    _msg_box(monkeypatch)
    p = AmbiguityDetectPopup(root, controller=None, termbase=_tb_with_misconceptions())
    p._text_box.insert("1.0", "客户端预测的手感回跳是不是动画问题")
    p._on_run()
    root.update()
    status = p._status_lbl.cget("text") or ""
    n = len(p._content.winfo_children())
    p._win.destroy()
    assert "降级" in status
    assert n > 0, "降级报告未渲染(曾因 _on_jev_success 缺 ui_icon import 崩溃)"


def test_ambiguity_remote_mocked(root, app, isolated, monkeypatch):
    """remote 全链(mock evaluate):钉 build_ambiguity_payload(text, hits) 参数序、
    format_ambiguity_report(text, hits, answers) 签名、报告入库 kind=ambiguity。"""
    import tkinter.messagebox as mb
    from tcer.core import typesafe_client
    from tcer.gui.popups import AmbiguityDetectPopup

    llm_prefs.save({"base_url": "https://x.example", "model": "g-test",
                    "typesafe_key": "k", "typesafe_base_url": "https://ts.example",
                    "typesafe_model": "jev-test"})
    seen = {}

    def fake_eval(state, questions, *, api_key, base_url, model, **kw):
        seen.update(state_keys=sorted(state.keys()) if isinstance(state, dict) else None)
        return {"answers": {"misread_0": {"noul": 0.82}}}

    monkeypatch.setattr(typesafe_client, "evaluate", fake_eval)
    monkeypatch.setattr(mb, "askyesno", lambda *a, **k: True)

    p = AmbiguityDetectPopup(root, controller=None, termbase=_tb_with_misconceptions())
    p._text_box.insert("1.0", "客户端预测的手感回跳")
    p._on_run()
    _drain(root, 4.0, lambda: p._run_btn.cget("state") == "normal")
    status = p._status_lbl.cget("text") or ""
    n = len(p._content.winfo_children())
    p._win.destroy()

    assert "text" in (seen.get("state_keys") or []), \
        "state 应含原文 text 字段(曾因参数顺序颠倒把词条列表传成了 text)"
    assert "探测完成" in status and n > 0
    saved = [r for r in llm_reports.load() if r["kind"] == "ambiguity"]
    assert saved and saved[0]["text"]


# ---------------------------------------------------------------- F2 术语考古

def test_archaeology_offline_degraded(root, app, report, isolated, monkeypatch):
    """离线降级全链:采样→热力图→报告入库 kind=terms(钉 llm_prefs.enabled 幽灵 API)。"""
    from tcer.core.termbase import TermEntry, Termbase, save_termbase
    from tcer.gui.app import TcerGui
    # 词条库先落一条已知词条,热力图才有命中可言
    save_termbase(Termbase(version=1, terms=[
        TermEntry(slug="cp", pref_label="客户端预测", definition="d", status="active")],
        errors=[]), str(isolated))
    _msg_box(monkeypatch)
    app._current = type("P", (), {"reports": [report]})()
    app._selected_session_id = report.meta.session_id
    app._session_report = lambda sid: report
    # _load_user_messages 返回 (messages, label) 元组——形状错了整个采样链路全废
    monkeypatch.setattr(
        TcerGui, "_load_user_messages",
        staticmethod(lambda r, reports=None:
                     (["把客户端预测改成插值方案", "你好"], "术语闭环")))
    app.run_terms_archaeology_current()
    _drain(root, 6.0, lambda: not app._llm_tasks)
    saved = [r for r in llm_reports.load() if r["kind"] == "terms"]
    assert saved, "离线考古报告未入库(曾因 llm_prefs.is_enabled 幽灵 API 必崩)"
    assert "客户端预测" in saved[0]["text"], "热力图缺已知词条命中"
    assert not app._llm_tasks
    # 完成后必须切到「LLM 报告」页签并选中该条——右键发起后用户停在项目
    # 列表页,仅状态栏一闪等于「处理完了但没结果」(用户实测打回)
    assert app._nb.index("current") == app._nb.index(app._llm_tab), \
        "考古完成后未跳转 LLM 报告页签"
    assert app.llm_reports_view._selected_id == saved[0]["id"], "报告未被选中"


def test_archaeology_remote_error_path(root, app, report, isolated, monkeypatch):
    """remote 失败路径:Jev 抛异常 → 状态栏失败提示 + 任务表必清。

    曾双重坑:①answers 键名漂移(worker 取 cand_0,真实回包是 choice_1)致
    route 内 AttributeError;②worker except 的 lambda 延迟引用已被隐式删除
    的异常变量 e → NameError 被 drain 吞 → 错误回填从未到达、任务表永不
    清理、状态栏永远停在「正在进行」(用户实测两次打回)。
    """
    import tkinter.messagebox as mb
    from tcer.core import llm_client, typesafe_client
    from tcer.gui.app import TcerGui

    llm_prefs.save({"base_url": "https://x.example", "model": "g-test",
                    "api_key": "llm-k",
                    "typesafe_key": "k", "typesafe_base_url": "https://ts.example",
                    "typesafe_model": "jev-test"})

    def boom(state, questions, *, api_key, base_url, model, **kw):
        raise typesafe_client.TypesafeError("模拟 Jev 端点故障")

    monkeypatch.setattr(llm_client, "chat", lambda **kw: json.dumps(
        {"candidates": [{"term": "x", "context": "c"}]}))
    monkeypatch.setattr(typesafe_client, "evaluate", boom)
    _msg_box(monkeypatch)
    app._current = type("P", (), {"reports": [report]})()
    app._selected_session_id = report.meta.session_id
    app._session_report = lambda sid: report
    monkeypatch.setattr(
        TcerGui, "_load_user_messages",
        staticmethod(lambda r, reports=None: (["消息"], "术语闭环")))

    app.run_terms_archaeology_current()
    _drain(root, 6.0, lambda: not app._llm_tasks)
    assert not app._llm_tasks, "失败路径任务表未清理(except 变量 e 延迟引用 NameError 被吞)"
    assert not [r for r in llm_reports.load() if r["kind"] == "terms"]


def test_archaeology_remote_mocked(root, app, report, isolated, monkeypatch):
    """remote 全链(mock chat + evaluate):钉 llm_client.chat 真实签名
    (曾调不存在的 chat_completion)、LLM 提名→Jev 归属→报告入库。"""
    import tkinter.messagebox as mb
    from tcer.core import llm_client, typesafe_client
    from tcer.gui.app import TcerGui

    llm_prefs.save({"base_url": "https://x.example", "model": "g-test",
                    "api_key": "llm-k",
                    "typesafe_key": "k", "typesafe_base_url": "https://ts.example",
                    "typesafe_model": "jev-test"})
    chat_kw = {}

    def fake_chat(**kw):
        chat_kw.update(kw)
        return json.dumps({"candidates": [
            {"term": "水床效应", "context": "ctx", "rationale": "r",
             "suggested_definition": "d"}]})

    def fake_eval(state, questions, *, api_key, base_url, model, **kw):
        # 键名从 payload 构建器机械提取——mock 与 build_archaeology_jev_payload
        # 任何一侧键名漂移,本测试立即红(曾两侧同错 cand_0 假绿:真实 Jev 回包
        # 键是 choice_1/noul_1,worker 取 None 后在 route 内 AttributeError)
        answers = {}
        for k in questions:
            if k.startswith("choice_"):
                answers[k] = {"choice": "new_term", "confidence": 0.9,
                              "probabilities": {"new_term": 0.7}}
            elif k.startswith("noul_"):
                answers[k] = {"noul": 0.9}
        return {"answers": answers}

    monkeypatch.setattr(llm_client, "chat", fake_chat)
    monkeypatch.setattr(typesafe_client, "evaluate", fake_eval)
    _msg_box(monkeypatch)
    app._current = type("P", (), {"reports": [report]})()
    app._selected_session_id = report.meta.session_id
    app._session_report = lambda sid: report
    monkeypatch.setattr(
        TcerGui, "_load_user_messages",
        staticmethod(lambda r, reports=None: (["这个水床效应很明显"], "术语闭环")))

    app.run_terms_archaeology_current()
    _drain(root, 8.0, lambda: not app._llm_tasks)

    assert set(chat_kw) >= {"system", "user", "model", "base_url", "api_key"}, \
        "chat 调用形状错误(曾调不存在的 chat_completion)"
    saved = [r for r in llm_reports.load() if r["kind"] == "terms"]
    assert saved and "水床效应" in saved[0]["text"]
    assert not app._llm_tasks, "任务表未清空(失败路径曾静默泄漏)"
