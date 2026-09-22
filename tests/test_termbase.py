"""TCER 术语库核心单测（P1 验收测试）。

测试全部本地离线运行，零网络依赖。
特别覆盖监督观察点：
1. find_terms_in_text 词边界测试（edit 不得命中 editor 等）；
2. CJK 子串与短黑话保护；
3. 加载容错与原子写入；
4. Markdown 导出。
"""
from __future__ import annotations

import json
from pathlib import Path

from tcer.core.termbase import (
    MIN_EN_LABEL_LEN,
    MIN_LABEL_LEN,
    ROLE_LABELS,
    Termbase,
    TermEntry,
    TermHit,
    build_alias_index,
    export_markdown,
    find_terms_in_text,
    load_termbase,
    save_termbase,
    validate_entry,
)


def _sample_term() -> TermEntry:
    return TermEntry(
        slug="client-prediction",
        pref_label="客户端预测",
        term_en="client-side prediction",
        alt_labels=["预表现", "抢跑"],
        mda_layer="mechanics",
        status="active",
        owner="张三",
        definition="本地先按假设模拟并立刻呈现，服务器权威结果到达后校正，不一致则回滚。",
        renderings={
            "art": "出手瞬间画面就动，服务器稍后盖章；被打断时画面回跳。",
            "eng": "Client simulates ahead; server reconciles on snapshot.",
        },
        misconceptions=[
            {"role": "art", "wrong": "以为手感回跳是动画资源问题", "actual": "预测回滚阈值改动导致"},
        ],
        notes="核心战斗手感依赖项",
    )


# --------------------------------------------------------------------------- #
# 1. 词边界与文本匹配测试（首要监督观察点）
# --------------------------------------------------------------------------- #

def test_find_terms_latin_word_boundary_strict():
    """验证拉丁词边界：严格杜绝 edit 命中 editor / edited / credits。"""
    tb = Termbase(
        terms=[
            TermEntry(slug="edit-cmd", pref_label="编辑命令", term_en="edit"),
        ]
    )

    # 1. 包含 editor、edited、credits 时不得命中 edit
    no_hit_text = "The editor opened an edited file with many credits."
    hits = find_terms_in_text(no_hit_text, tb)
    assert len(hits) == 0, f"意外命中了非独立词 edit: {hits}"

    # 2. 独立单词匹配：标点隔开、空格隔开、括号包围等
    hit_text = "Please edit this code now. (edit) fast-edit"
    hits = find_terms_in_text(hit_text, tb)
    assert len(hits) >= 2
    # 验证前两次命中位置精确
    assert hit_text[hits[0].start:hits[0].end].lower() == "edit"
    assert hit_text[hits[1].start:hits[1].end].lower() == "edit"


def test_find_terms_cjk_substring():
    """CJK 标签直接进行子串匹配。"""
    term = _sample_term()
    tb = Termbase(terms=[term])

    text = "当前战斗手感有问题，客户端预测和预表现逻辑似乎冲突了。"
    hits = find_terms_in_text(text, tb)
    assert len(hits) == 2
    labels = [h.matched_label for h in hits]
    assert "客户端预测" in labels
    assert "预表现" in labels
    assert all(h.term.slug == "client-prediction" for h in hits)


def test_find_terms_short_label_protection():
    """短黑话保护：标签长度 < 2 必须跳过；英文 term_en 长度 < 4 必须跳过。"""
    tb = Termbase(
        terms=[
            TermEntry(slug="short-cn", pref_label="飘", alt_labels=["X"]),
            TermEntry(slug="short-en", pref_label="视距", term_en="los"),  # len("los") == 3 < 4
            TermEntry(slug="valid-en", pref_label="延迟", term_en="ping"), # len("ping") == 4 >= 4
        ]
    )

    # "飘" 和 "X" 长度均 < 2，不应作为有效标签匹配；"los" 长度为 3 < 4，不匹配
    text = "视野 los 飘了，网络 ping 太高，连按 X 键测试。"
    hits = find_terms_in_text(text, tb)

    matched_labels = [h.matched_label for h in hits]
    assert "飘" not in matched_labels
    assert "X" not in matched_labels
    assert "los" not in matched_labels
    assert "ping" in matched_labels


def test_find_terms_deprecated_filtering():
    """已废弃词条默认不参与匹配；include_deprecated=True 时参与。"""
    tb = Termbase(
        terms=[
            TermEntry(slug="old-term", pref_label="老废弃功能", status="deprecated"),
        ]
    )
    text = "这里依然提到了老废弃功能。"
    assert len(find_terms_in_text(text, tb)) == 0
    assert len(find_terms_in_text(text, tb, include_deprecated=True)) == 1


# --------------------------------------------------------------------------- #
# 2. 别名索引
# --------------------------------------------------------------------------- #

def test_build_alias_index():
    tb = Termbase(terms=[_sample_term()])
    idx = build_alias_index(tb)
    assert "客户端预测" in idx
    assert "预表现" in idx
    assert "抢跑" in idx
    assert "client-side prediction" in idx
    assert idx["客户端预测"] == ["client-prediction"]


# --------------------------------------------------------------------------- #
# 3. 数据模型校验与容错
# --------------------------------------------------------------------------- #

def test_validate_entry_valid():
    entry = _sample_term()
    errs = validate_entry(entry)
    assert errs == []


def test_validate_entry_invalid_fields():
    # 非法 slug（含大写或空格）、非法 status、非法 role
    bad = {
        "slug": "Bad_Slug!",
        "pref_label": "",
        "status": "unknown_status",
        "mda_layer": "super_layer",
        "renderings": {"alien_role": "xyz"},
        "misconceptions": [{"role": "robot", "wrong": ""}],
    }
    errs = validate_entry(bad)
    assert any("slug" in e for e in errs)
    assert any("pref_label" in e for e in errs)
    assert any("status" in e for e in errs)
    assert any("mda_layer" in e for e in errs)
    assert any("alien_role" in e for e in errs)
    assert any("robot" in e for e in errs)
    assert any("wrong" in e for e in errs)


def test_validate_entry_duplicate_slug():
    entry = _sample_term()
    errs = validate_entry(entry, existing_slugs={"client-prediction"})
    assert any("已存在" in e for e in errs)


# --------------------------------------------------------------------------- #
# 4. 加载容错与原子保存
# --------------------------------------------------------------------------- #

def test_load_non_existent_file(tmp_path: Path):
    p = tmp_path / "missing_tb.json"
    tb = load_termbase(p)
    assert tb.version == 1
    assert tb.terms == []
    assert tb.errors == []


def test_load_tolerant_with_corrupt_entry(tmp_path: Path):
    """单条目损坏不导致整个文件崩溃，正常条目保留，错误存入 errors。"""
    p = tmp_path / "mixed.json"
    content = {
        "version": 1,
        "terms": [
            _sample_term().to_dict(),
            {"slug": "corrupted slug with spaces", "pref_label": "坏条目"},
        ],
    }
    p.write_text(json.dumps(content, ensure_ascii=False), encoding="utf-8")

    tb = load_termbase(p)
    assert tb.term_count() == 1
    assert tb.terms[0].slug == "client-prediction"
    assert len(tb.errors) >= 1
    assert any("坏条目" in e or "corrupted" in e for e in tb.errors)


def test_save_and_load_roundtrip(tmp_path: Path):
    """验证原子保存与加载完全等价，保留未知字段。"""
    p = tmp_path / "sub" / "tb.json"
    t1 = _sample_term()
    t1.extra = {"custom_meta": 42}
    tb = Termbase(version=2, terms=[t1])

    save_termbase(p, tb)
    loaded = load_termbase(p)

    assert loaded.version == 2
    assert loaded.term_count() == 1
    assert loaded.errors == []
    t_loaded = loaded.terms[0]
    assert t_loaded.slug == t1.slug
    assert t_loaded.pref_label == t1.pref_label
    assert t_loaded.alt_labels == ["预表现", "抢跑"]
    assert t_loaded.renderings["art"] == t1.renderings["art"]
    assert t_loaded.misconceptions[0]["wrong"] == t1.misconceptions[0]["wrong"]
    assert t_loaded.extra == {"custom_meta": 42}


# --------------------------------------------------------------------------- #
# 5. Markdown 导出
# --------------------------------------------------------------------------- #

def test_export_markdown():
    t_active = _sample_term()
    t_dep = TermEntry(
        slug="deprecated-one",
        pref_label="废弃机制",
        status="deprecated",
        definition="早期设计的废弃方案",
    )
    tb = Termbase(terms=[t_active, t_dep])
    md = export_markdown(tb)

    assert "# TCER 术语库导出 · 2 词条" in md
    assert "## 客户端预测（client-side prediction）" in md
    assert "**状态**: 活跃" in md
    assert "### 各职能理解" in md
    assert "**美术**:" in md
    assert "### 常见误解" in md
    assert "## 废弃机制 （已废弃）" in md


# --------------------------------------------------------------------------- #
# 6. P2 歧义检测（D3 误读探针）测试
# --------------------------------------------------------------------------- #

def test_build_ambiguity_payload_verbatim_prompt():
    """验证英文题面逐字稿与手册完全一致（防提示词漂移导致缓存与阈值失效）。"""
    from tcer.core.llm_prompts import build_ambiguity_payload

    term = _sample_term()
    hits = [TermHit(term=term, matched_label="客户端预测", start=0, end=5)]
    text = "需求描述：客户端预测如果失败立刻回退。"
    state, questions = build_ambiguity_payload(text, hits)

    assert state["text"] == text
    assert len(questions) == 1
    q = questions["misread_1"]
    assert q["type"] == "noul"

    # 监督观察点 #2：题面与 criteria 严格逐字匹配
    expected_instructions = (
        "Requirement text is in `text`. Would a reader whose expertise is art "
        "plausibly come away believing: '以为手感回跳是动画资源问题'? "
        "Judge only what the wording supports, not what a careful expert would eventually conclude. "
        "Answer true only if the wording genuinely invites this specific misreading."
    )
    assert q["instructions"] == expected_instructions
    assert q["criteria"] == {
        "true": "The wording genuinely supports this misreading",
        "false": "No plausible misreading of this kind",
    }


def test_format_ambiguity_report_max_gate_and_gray_zone():
    """验证 max 门：探针 P >= 0.5 触发高风险标红；0.40–0.60 进灰色地带；审计契约通过。"""
    from tcer.core.llm_prompts import (
        AMBIGUITY_FLAG_THRESHOLD,
        audit_warnings,
        format_ambiguity_report,
    )

    t1 = TermEntry(
        slug="t1",
        pref_label="机制一",
        definition="机制一的标准定义",
        renderings={"art": "美术理解", "eng": "程序理解"},
        misconceptions=[
            {"role": "art", "wrong": "误解1", "actual": "实际1"},
            {"role": "eng", "wrong": "误解2", "actual": "实际2"},
        ],
    )
    hits = [TermHit(term=t1, matched_label="机制一", start=10, end=13)]
    text = "我们在项目中使用了机制一，效果明显。"

    # 模拟 Jev 返回：misread_1 概率 0.72 (>= 0.5 标红)；misread_2 概率 0.45 (灰色地带)
    answers = {
        "misread_1": {"type": "noul", "noul": 0.72},
        "misread_2": {"type": "noul", "noul": 0.45},
    }

    report_md, data = format_ambiguity_report(text, hits, answers, model="jev-test")

    assert data["flagged_terms"] == 1
    assert data["gray_items"] == 1
    assert "🔴 [误读高风险]" in report_md
    assert "各职能正确理解参考" in report_md
    assert "美术理解" in report_md
    assert "建议改写方向" in report_md
    assert "## 灰色地带（建议人工复核）" in report_md
    assert "0.72" not in report_md  # 百分比呈现 72.0%
    assert "72.0%" in report_md
    assert "45.0%" in report_md

    # 验证机械审计警告：provider="ambiguity" 不得报转折锚点/归因缺失
    warns = audit_warnings(report_md, provider="ambiguity")
    assert warns == []


def test_format_ambiguity_report_degraded_offline():
    """验证未配置 key 时优雅降级模式：展示预设清单，不报错，通过审计。"""
    from tcer.core.llm_prompts import audit_warnings, format_ambiguity_report

    term = _sample_term()
    hits = [TermHit(term=term, matched_label="客户端预测", start=0, end=5)]
    text = "客户端预测文本。"

    report_md, data = format_ambiguity_report(text, hits, answers={}, is_degraded=True)

    assert data["is_degraded"] is True
    assert "未联网判定，以下为词条预设的误解清单" in report_md
    assert "执行摘要" in report_md
    assert "以为手感回跳是动画资源问题" in report_md

    warns = audit_warnings(report_md, provider="ambiguity")
    assert warns == []


# --------------------------------------------------------------------------- #
# 7. P3 反向查询（Jev 语义推断与路由）测试
# --------------------------------------------------------------------------- #

def test_build_lookup_payload_escape_hatch():
    """验证反向查询 payload 必须包含 none_of_the_above 逃生口。"""
    from tcer.core.llm_prompts import build_lookup_payload

    tb = Termbase(terms=[_sample_term()])
    state, questions = build_lookup_payload("抢跑手感", tb.active_terms())

    assert state["query"] == "抢跑手感"
    assert "is_term" in questions
    assert questions["is_term"]["type"] == "noul"
    assert "which_term" in questions
    which_q = questions["which_term"]
    assert which_q["type"] == "choice"
    assert "none_of_the_above" in which_q["criteria"]
    assert "client-prediction" in which_q["criteria"]


def test_parse_lookup_answer_high_confidence():
    """置信度 >= 0.6 时，直接命中并返回对应的 slug。"""
    from tcer.core.llm_prompts import parse_lookup_answer

    answers = {
        "is_term": {"type": "noul", "noul": 0.88},
        "which_term": {
            "type": "choice",
            "choice": "client-prediction",
            "confidence": 0.82,
            "probabilities": {"client-prediction": 0.82, "none_of_the_above": 0.18},
        },
    }
    result = parse_lookup_answer(answers)
    assert result["status"] == "hit"
    assert result["term_slug"] == "client-prediction"
    assert result["confidence"] == 0.82


def test_parse_lookup_answer_low_confidence_candidates():
    """置信度 < 0.6 时，返回 top-3 候选供人工甄别。"""
    from tcer.core.llm_prompts import parse_lookup_answer

    answers = {
        "is_term": {"type": "noul", "noul": 0.75},
        "which_term": {
            "type": "choice",
            "choice": "client-prediction",
            "confidence": 0.45,
            "probabilities": {
                "client-prediction": 0.45,
                "server-reconcile": 0.35,
                "lag-compensation": 0.15,
                "none_of_the_above": 0.05,
            },
        },
    }
    result = parse_lookup_answer(answers)
    assert result["status"] == "candidates"
    assert len(result["top_candidates"]) == 3
    slugs = [slug for slug, _ in result["top_candidates"]]
    assert slugs == ["client-prediction", "server-reconcile", "lag-compensation"]


def test_parse_lookup_answer_escape_hatch_or_non_term():
    """逃生口命中或 is_term <= 0.3 时返回 no_match。"""
    from tcer.core.llm_prompts import parse_lookup_answer

    # 1. 逃生口
    escape_ans = {
        "is_term": {"type": "noul", "noul": 0.80},
        "which_term": {
            "type": "choice",
            "choice": "none_of_the_above",
            "confidence": 0.90,
        },
    }
    res1 = parse_lookup_answer(escape_ans)
    assert res1["status"] == "no_match"

    # 2. 非技术术语日常口语
    casual_ans = {
        "is_term": {"type": "noul", "noul": 0.15},
    }
    res2 = parse_lookup_answer(casual_ans)
    assert res2["status"] == "no_match"
    assert "不具备技术概念" in res2["reason"]


# --------------------------------------------------------------------------- #
# 8. P4 术语考古（采样、热力图、新词提名与归属）测试
# --------------------------------------------------------------------------- #

def test_sample_archaeology_messages_discipline():
    """验证采样纪律：纠正优先、40上限、跳过斜杠命令、去重。"""
    from tcer.core.llm_prompts import sample_archaeology_messages

    msgs = [
        "/help",  # 斜杠命令 -> 跳过
        "普通消息 A",
        "普通消息 A",  # 重复 -> 去重
        "不对，这里逻辑错了，请回滚",  # 显式纠正 -> 置顶
        "普通消息 B",
        "<command-name>run</command-name>",  # 命令面板命令 -> 跳过
    ]
    # 填充大量普通消息测上限
    msgs.extend([f"普通消息 {i}" for i in range(100)])

    sampled = sample_archaeology_messages(msgs)
    assert len(sampled) <= 40
    assert sampled[0] == "不对，这里逻辑错了，请回滚"
    assert not any(m.startswith("/") or m.startswith("<command") for m in sampled)
    assert len(set(sampled)) == len(sampled)


def test_analyze_term_heatmap():
    """验证已知词条热力图统计与 ±60 字符摘录。"""
    from tcer.core.llm_prompts import analyze_term_heatmap

    tb = Termbase(terms=[_sample_term()])
    msgs = [
        "我们在战斗模块中使用了客户端预测机制来保证操作响应。",
        "网络延迟导致客户端预测发生误差。",
        "无关消息。",
    ]
    items = analyze_term_heatmap(msgs, tb)
    assert len(items) == 1
    assert items[0]["slug"] == "client-prediction"
    assert items[0]["hit_count"] == 2
    assert items[0]["msg_count"] == 2
    assert len(items[0]["excerpts"]) == 2
    assert "客户端预测" in items[0]["excerpts"][0]


def test_parse_nominated_terms_tolerance():
    """验证 LLM 提名输出的容错解析（支持 markdown 代码块与异常降级）。"""
    from tcer.core.llm_prompts import parse_nominated_terms

    valid_json = """```json
    {
      "candidates": [
        {"term": "抢跑手感", "context": "这个抢跑手感不对", "rationale": "动作前摇", "suggested_definition": "前摇机制"}
      ]
    }
    ```"""
    cands = parse_nominated_terms(valid_json)
    assert len(cands) == 1
    assert cands[0]["term"] == "抢跑手感"

    # 坏 JSON 优雅降级
    bad_cands = parse_nominated_terms("Sorry, I cannot produce JSON.")
    assert bad_cands == []


def test_build_archaeology_jev_payload_and_routing():
    """验证 Jev 归属判定 payload 构建与三种路由裁决。"""
    from tcer.core.llm_prompts import (
        build_archaeology_jev_payload,
        route_archaeology_candidate,
    )
    from tcer.core.termbase import validate_entry

    tb = Termbase(terms=[_sample_term()])
    candidates = [
        {"term": "先行预测", "context": "先行预测出现抖动", "rationale": "类似客户端预测"},
        {"term": "吃了吗", "context": "吃了吗兄弟", "rationale": "口语问候"},
        {"term": "定点回溯", "context": "需要做定点回溯", "rationale": "独立系统概念"},
    ]

    state, questions = build_archaeology_jev_payload(candidates, tb)
    assert "choice_1" in questions
    assert "noul_1" in questions
    assert "alias_of_client-prediction" in questions["choice_1"]["criteria"]
    assert "discard" in questions["choice_1"]["criteria"]
    assert "new_term" in questions["choice_1"]["criteria"]

    # 1. 别名路由
    r1 = route_archaeology_candidate(
        candidates[0],
        choice_ans={"choice": "alias_of_client-prediction", "confidence": 0.85},
        noul_ans={"noul": 0.9},
    )
    assert r1["action"] == "suggest_alias"
    assert r1["target_slug"] == "client-prediction"

    # 2. 口语噪音路由 (is_technical <= 0.3)
    r2 = route_archaeology_candidate(
        candidates[1],
        choice_ans={"choice": "new_term", "confidence": 0.5},
        noul_ans={"noul": 0.1},
    )
    assert r2["action"] == "discard"

    # 3. 新词提案路由
    r3 = route_archaeology_candidate(
        candidates[2],
        choice_ans={"choice": "new_term", "confidence": 0.75},
        noul_ans={"noul": 0.88},
    )
    assert r3["action"] == "new_term"
    assert "proposal" in r3
    proposal = r3["proposal"]
    # 提案必须符合 TermEntry schema 校验
    entry = TermEntry.from_dict(proposal)
    errs = validate_entry(entry)
    assert errs == []


def test_format_terms_report_audit_and_proposals():
    """验证 format_terms_report 生成合规 Markdown 并通过机械审计。"""
    from tcer.core.llm_prompts import audit_warnings, format_terms_report

    heatmap = [{
        "term": _sample_term(),
        "slug": "client-prediction",
        "pref_label": "客户端预测",
        "hit_count": 3,
        "msg_count": 2,
        "excerpts": ["样例上下文"],
    }]
    verdicts = [
        {
            "term": "定点回溯",
            "action": "new_term",
            "confidence": 0.8,
            "context": "样例上下文",
            "proposal": {
                "slug": "reconcile-checkpoint",
                "pref_label": "定点回溯",
                "definition": "服务器检查点对齐机制",
                "status": "draft",
            },
        }
    ]

    report_md, data = format_terms_report(
        heatmap_items=heatmap,
        candidate_verdicts=verdicts,
        total_messages_sampled=25,
    )

    assert "# 术语考古分析报告" in report_md
    assert "## 执行摘要" in report_md
    assert "## 已知词条热力图" in report_md
    assert "## 新词条提案（JSON 片段）" in report_md
    assert "reconcile-checkpoint" in report_md

    # 机械审计校验
    warns = audit_warnings(report_md, provider="terms")
    assert warns == []


def test_ui_prefs_termbase_path(tmp_path, monkeypatch):
    """测试词条库路径首选项读取与落盘。"""
    from tcer.core import ui_prefs, termbase

    test_prefs_file = tmp_path / "ui_prefs.json"
    monkeypatch.setattr(ui_prefs, "_prefs_path", lambda: test_prefs_file)

    # 初始未配置自定义路径时返回 None
    assert ui_prefs.get_termbase_path() is None
    # 默认路径回退到 prefs_dir() / termbase.json
    p0 = termbase.default_termbase_path()
    assert p0.name == "termbase.json"

    # 设置自定义路径
    custom = tmp_path / "team_shared" / "terms.json"
    ui_prefs.set_termbase_path(str(custom))
    assert ui_prefs.get_termbase_path() == str(custom)
    assert termbase.default_termbase_path() == custom


def test_termbase_view_headless(tmp_path, monkeypatch):
    """测试 TermbaseView 核心交互（列表加载、选择、修改保存、新建、删除、过滤）。"""
    import tkinter as tk
    from tcer.core.termbase import Termbase, TermEntry, save_termbase
    from tcer.gui.views import TermbaseView

    tb_file = tmp_path / "termbase.json"
    tb = Termbase(
        terms=[
            TermEntry(
                slug="test-term",
                pref_label="测试词条",
                status="active",
                definition="这是测试定义",
            )
        ]
    )
    save_termbase(tb, tb_file)

    from tcer.core import termbase
    monkeypatch.setattr(termbase, "default_termbase_path", lambda: tb_file)

    try:
        root = tk.Tk()
        root.withdraw()
    except Exception:
        import pytest
        pytest.skip("No Tk display available")

    try:
        frame = tk.Frame(root)
        view = TermbaseView(frame, controller=None)
        assert len(view._cards) == 1
        assert view._selected_slug == "test-term"

        # 修改 definition 并保存
        view._f_def.delete("1.0", "end")
        view._f_def.insert("1.0", "更新后的权威定义")
        view._save_current_entry()

        updated_tb = termbase.load_termbase(tb_file)
        assert updated_tb.terms[0].definition == "更新后的权威定义"

        # 新建词条
        view._create_new_term()
        assert len(view._termbase.terms) == 2
        assert view._selected_slug.startswith("new-concept")

        # 搜索过滤
        view._search_var.set("测试词条")
        view._on_search_changed()
        assert len(view._cards) == 1

        # 状态过滤
        view._search_var.set("")
        view._set_status_filter("draft")
        assert len(view._cards) == 1
    finally:
        root.destroy()


def test_role_key_aliases_normalization():
    """验证从外部或 LLM 报告解析时，职能键别名（engineering/design/client 等）规范化为 SSOT 标准键。"""
    from tcer.core.termbase import TermEntry, validate_entry

    raw = {
        "slug": "rollback-netcode",
        "pref_label": "回滚网络同步",
        "definition": "预测输入并在发散时回滚重放",
        "renderings": {
            "engineering": "本地预测帧与输入队列，收到权威包后纠正",
            "client": "表现层平滑插值",
            "design": "手感零延迟体验",
            "sound": "回滚时音频不做反向播放",
        },
        "misconceptions": [
            {"role": "engineering", "wrong": "以为每帧全量发包", "actual": "差分输入同步"},
            {"role": "design", "wrong": "以为物理完全一致", "actual": "定点数物理确定性"},
        ],
    }
    entry = TermEntry.from_dict(raw)
    assert "eng" in entry.renderings
    assert "designer" in entry.renderings
    assert "audio" in entry.renderings
    assert entry.misconceptions[0]["role"] == "eng"
    assert entry.misconceptions[1]["role"] == "designer"

    errs = validate_entry(entry)
    assert not errs, f"Validation errors: {errs}"


def test_termbase_view_dirty_checking_headless(tmp_path, monkeypatch):
    """验证 TermbaseView 编辑表单脏检查（防误触丢失）与 slug 清洗逻辑。"""
    import tkinter as tk
    from tcer.core import termbase
    from tcer.core.termbase import Termbase, TermEntry, save_termbase
    from tcer.gui.views import TermbaseView

    try:
        root = tk.Tk()
        root.withdraw()
    except Exception:
        import pytest
        pytest.skip("No Tk display available")

    tb_path = tmp_path / "termbase.json"
    tb = Termbase(terms=[
        TermEntry(slug="sync-term", pref_label="同步概念", definition="定义内容", status="active")
    ])
    save_termbase(tb, tb_path)
    monkeypatch.setattr(termbase, "default_termbase_path", lambda: tb_path)

    try:
        view = TermbaseView(root, controller=None)
        view.on_show()

        # 初始无未保存修改
        assert not view._has_unsaved_changes()

        # 修改其中一个字段
        view._f_pref.delete(0, "end")
        view._f_pref.insert(0, "修改后的同步概念")
        assert view._has_unsaved_changes()

        # 保存后变回洁净
        success = view._save_current_entry()
        assert success
        assert not view._has_unsaved_changes()

        # 验证 slug 规整: 包含大写与空格/下划线时，保存自动转为合法小写连字符
        view._f_slug.delete(0, "end")
        view._f_slug.insert(0, "New_Custom Concept")
        success = view._save_current_entry()
        assert success
        assert view._f_slug.get() == "new-custom-concept"
        assert not view._has_unsaved_changes()
    finally:
        root.destroy()


def test_term_import_load_from_archaeology_report_headless(tmp_path, monkeypatch):
    """验证 TermImportPopup 可直接从最新考古报告载入 JSON 提案代码块并导入。"""
    import tkinter as tk
    from tcer.core import llm_reports, termbase
    from tcer.core.termbase import Termbase, load_termbase, save_termbase
    from tcer.gui.popups import TermImportPopup

    try:
        root = tk.Tk()
        root.withdraw()
    except Exception:
        import pytest
        pytest.skip("No Tk display available")

    tb_path = tmp_path / "termbase.json"
    save_termbase(Termbase(terms=[]), tb_path)
    monkeypatch.setattr(termbase, "default_termbase_path", lambda: tb_path)

    # Mock 一条真实格式的 terms 考古报告
    report_text = """# 术语考古报告

发现以下团队黑话提案：

```json
[
  {
    "slug": "ghost-frame",
    "pref_label": "幽灵帧",
    "definition": "客户端预测与服务端状态未对齐时的瞬间闪烁帧",
    "status": "draft"
  }
]
```
"""
    mock_reports = [
        {
            "id": "rep-1",
            "kind": "terms",
            "title": "术语考古 · Session 001",
            "text": report_text,
            "created_at": 1000,
        }
    ]
    monkeypatch.setattr(llm_reports, "load", lambda: mock_reports)

    try:
        imported = []
        popup = TermImportPopup(root, on_imported=lambda: imported.append(1))
        popup._load_from_latest_report()

        # 文本框中已自动载入 JSON 片段
        content = popup._text.get("1.0", "end").strip()
        assert "ghost-frame" in content
        assert "幽灵帧" in content

        # 执行解析并导入（mock 弹窗族避免测试环境阻塞真 modal 窗口；
        # _do_import 现含「AI 提案人裁决」导入前确认清单 askyesno）
        from tkinter import messagebox
        monkeypatch.setattr(messagebox, "showinfo", lambda *args, **kwargs: None)
        monkeypatch.setattr(messagebox, "askyesno", lambda *args, **kwargs: True)
        monkeypatch.setattr(messagebox, "showerror", lambda *args, **kwargs: None)
        monkeypatch.setattr(messagebox, "showwarning", lambda *args, **kwargs: None)
        popup._do_import()
        assert len(imported) == 1

        # 验证 termbase.json 已持久化写入该词条
        updated_tb = load_termbase(tb_path)
        assert len(updated_tb.terms) == 1
        assert updated_tb.terms[0].slug == "ghost-frame"
        assert updated_tb.terms[0].pref_label == "幽灵帧"
    finally:
        root.destroy()




