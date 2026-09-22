"""TCER 术语库核心数据模型、校验、索引、匹配与导出（纯本地，零第三方依赖）。

依据 termbase-handoff.md §4 实施规格编写。
为 SharedBrain 跨职能语义对齐与歧义检测提供底层支持。
"""
from __future__ import annotations

import json
import os
import re
import tempfile
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path

# --------------------------------------------------------------------------- #
# 枚举与规范常量（SSOT）
# --------------------------------------------------------------------------- #

ROLE_LABELS: dict[str, str] = {
    "art": "美术",
    "designer": "策划",
    "ux": "交互",
    "eng": "程序",
    "audio": "音频",
    "qa": "QA",
}

_ROLE_KEY_ALIASES: dict[str, str] = {
    "engineering": "eng",
    "design": "designer",
    "client": "eng",
    "server": "eng",
    "developer": "eng",
    "program": "eng",
    "test": "qa",
    "sound": "audio",
}

MDA_LAYERS: dict[str, str] = {
    "mechanics": "机制",
    "dynamics": "动态",
    "aesthetics": "体验",
}

STATUS_SET: set[str] = {"draft", "active", "deprecated"}

STATUS_LABELS: dict[str, str] = {
    "active": "活跃",
    "draft": "草稿",
    "deprecated": "已废弃",
}

MIN_LABEL_LEN: int = 2          # 短黑话保护：低于此长度的标签不参与匹配
MIN_EN_LABEL_LEN: int = 4       # 英文短词保护：低于此长度的 term_en 不参与匹配

_SLUG_RE = re.compile(r"^[a-z0-9-]+$")
_save_lock = threading.Lock()


# --------------------------------------------------------------------------- #
# 数据结构
# --------------------------------------------------------------------------- #

@dataclass
class TermEntry:
    slug: str
    pref_label: str
    term_en: str = ""
    alt_labels: list[str] = field(default_factory=list)
    mda_layer: str = ""
    status: str = "active"
    owner: str = ""
    definition: str = ""
    renderings: dict[str, str] = field(default_factory=dict)
    misconceptions: list[dict[str, str]] = field(default_factory=list)
    notes: str = ""
    extra: dict = field(default_factory=dict)

    def to_dict(self) -> dict:
        d = {
            "slug": self.slug,
            "pref_label": self.pref_label,
            "term_en": self.term_en,
            "alt_labels": list(self.alt_labels),
            "mda_layer": self.mda_layer,
            "status": self.status,
            "owner": self.owner,
            "definition": self.definition,
            "renderings": dict(self.renderings),
            "misconceptions": [dict(m) for m in self.misconceptions],
            "notes": self.notes,
        }
        if self.extra:
            d.update(self.extra)
        return d

    @classmethod
    def from_dict(cls, data: dict) -> TermEntry:
        known = {
            "slug", "pref_label", "term_en", "alt_labels", "mda_layer",
            "status", "owner", "definition", "renderings", "misconceptions", "notes"
        }
        extra = {k: v for k, v in data.items() if k not in known}
        alt = data.get("alt_labels")
        if isinstance(alt, list):
            alt_labels = [str(x).strip() for x in alt if str(x).strip()]
        else:
            alt_labels = []

        rends = data.get("renderings")
        renderings = {}
        if isinstance(rends, dict):
            for k, v in rends.items():
                sk = str(k).strip()
                canonical_k = _ROLE_KEY_ALIASES.get(sk, sk)
                renderings[canonical_k] = str(v).strip()

        miscs = data.get("misconceptions")
        misconceptions: list[dict[str, str]] = []
        if isinstance(miscs, list):
            for m in miscs:
                if isinstance(m, dict):
                    r = str(m.get("role", "")).strip()
                    canonical_r = _ROLE_KEY_ALIASES.get(r, r)
                    misconceptions.append({
                        "role": canonical_r,
                        "wrong": str(m.get("wrong", "")).strip(),
                        "actual": str(m.get("actual", "")).strip(),
                    })

        return cls(
            slug=str(data.get("slug", "")).strip(),
            pref_label=str(data.get("pref_label", "")).strip(),
            term_en=str(data.get("term_en", "")).strip(),
            alt_labels=alt_labels,
            mda_layer=str(data.get("mda_layer", "")).strip(),
            status=str(data.get("status", "active")).strip() or "active",
            owner=str(data.get("owner", "")).strip(),
            definition=str(data.get("definition", "")).strip(),
            renderings=renderings,
            misconceptions=misconceptions,
            notes=str(data.get("notes", "")).strip(),
            extra=extra,
        )


@dataclass
class TermHit:
    term: TermEntry
    matched_label: str
    start: int
    end: int


@dataclass
class Termbase:
    version: int = 1
    terms: list[TermEntry] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)

    def term_count(self) -> int:
        return len(self.terms)

    def active_terms(self) -> list[TermEntry]:
        return [t for t in self.terms if t.status == "active"]

    def by_slug(self, slug: str) -> TermEntry | None:
        for t in self.terms:
            if t.slug == slug:
                return t
        return None


# --------------------------------------------------------------------------- #
# 文件读写与校验
# --------------------------------------------------------------------------- #

def validate_entry(entry: TermEntry | dict, existing_slugs: set[str] | None = None) -> list[str]:
    """校验单条词条的合法性，返回错误描述列表（空列表 = 合法）。"""
    if isinstance(entry, dict):
        slug = str(entry.get("slug", "")).strip()
        pref_label = str(entry.get("pref_label", "")).strip()
        status = str(entry.get("status", "active")).strip()
        mda_layer = str(entry.get("mda_layer", "")).strip()
        renderings = entry.get("renderings")
        misconceptions = entry.get("misconceptions")
    else:
        slug = entry.slug
        pref_label = entry.pref_label
        status = entry.status
        mda_layer = entry.mda_layer
        renderings = entry.renderings
        misconceptions = entry.misconceptions

    errs: list[str] = []
    if not slug:
        errs.append("slug 必填且不能为空")
    elif not _SLUG_RE.match(slug):
        errs.append(f"slug '{slug}' 格式非法（只能包含小写字母、数字和连字符，如 [a-z0-9-]）")
    elif existing_slugs is not None and slug in existing_slugs:
        errs.append(f"slug '{slug}' 已存在，不能重复")

    if not pref_label:
        errs.append("中文主标签 pref_label 必填且不能为空")

    if status and status not in STATUS_SET:
        errs.append(f"status '{status}' 非法（必须为 {', '.join(sorted(STATUS_SET))} 之一）")

    if mda_layer and mda_layer not in MDA_LAYERS:
        errs.append(f"mda_layer '{mda_layer}' 非法（必须为 {', '.join(sorted(MDA_LAYERS))} 或留空）")

    if isinstance(renderings, dict):
        for rk in renderings.keys():
            if rk not in ROLE_LABELS and rk not in _ROLE_KEY_ALIASES:
                errs.append(f"renderings 职能 '{rk}' 非法（必须为 {', '.join(sorted(ROLE_LABELS))} 之一）")
    elif renderings is not None:
        errs.append("renderings 必须为字典")

    if isinstance(misconceptions, list):
        for idx, m in enumerate(misconceptions):
            if not isinstance(m, dict):
                errs.append(f"misconceptions[{idx}] 必须为字典对象")
                continue
            role = m.get("role")
            if role and role not in ROLE_LABELS and role not in _ROLE_KEY_ALIASES:
                errs.append(f"misconceptions[{idx}] 职能 '{role}' 非法（必须为 {', '.join(sorted(ROLE_LABELS))} 之一）")
            if not str(m.get("wrong", "")).strip():
                errs.append(f"misconceptions[{idx}] 误解描述 wrong 必填且不能为空")
    elif misconceptions is not None:
        errs.append("misconceptions 必须为列表")

    return errs


def default_termbase_path() -> Path:
    """返回默认术语库文件路径（优先从 ui_prefs 获取自定义路径，缺省回退 prefs_dir() / 'termbase.json'）。"""
    from tcer.core import app_dirs, ui_prefs
    try:
        custom = ui_prefs.get_termbase_path()
        if custom:
            return Path(custom)
    except Exception:
        pass
    return app_dirs.prefs_dir() / "termbase.json"


def load_termbase(path: str | Path | None = None) -> Termbase:
    """加载词条库。文件不存在时返回空 Termbase；容忍单条目损坏（收集进 errors）。"""
    p = Path(path) if path is not None else default_termbase_path()
    if not p.exists():
        return Termbase(version=1, terms=[], errors=[])

    try:
        raw = p.read_bytes().decode("utf-8")
        data = json.loads(raw)
    except Exception as e:
        return Termbase(version=1, terms=[], errors=[f"读取或解析 JSON 失败: {e}"])

    if not isinstance(data, dict):
        return Termbase(version=1, terms=[], errors=["根节点必须为 JSON 对象"])

    version = int(data.get("version", 1))
    terms_raw = data.get("terms", [])
    if not isinstance(terms_raw, list):
        return Termbase(version=version, terms=[], errors=["'terms' 字段必须为列表"])

    terms: list[TermEntry] = []
    errors: list[str] = []
    seen_slugs: set[str] = set()

    for idx, item in enumerate(terms_raw):
        if not isinstance(item, dict):
            errors.append(f"条目 #{idx + 1} 格式非法，不是 JSON 对象")
            continue
        val_errs = validate_entry(item, seen_slugs)
        if val_errs:
            errors.extend([f"条目 #{idx + 1} ({item.get('slug', '无slug')}): {e}" for e in val_errs])
            continue
        entry = TermEntry.from_dict(item)
        seen_slugs.add(entry.slug)
        terms.append(entry)

    return Termbase(version=version, terms=terms, errors=errors)


def save_termbase(
    path_or_tb: str | Path | Termbase | None,
    tb_or_path: Termbase | str | Path | None = None,
) -> None:
    """原子写：tmp + os.replace；持有模块级 threading.Lock。兼容 (tb, path) 与 (path, tb) 传参。"""
    if isinstance(path_or_tb, Termbase):
        tb = path_or_tb
        p = Path(tb_or_path) if tb_or_path is not None else default_termbase_path()
    elif isinstance(tb_or_path, Termbase):
        tb = tb_or_path
        p = Path(path_or_tb) if path_or_tb is not None else default_termbase_path()
    else:
        raise TypeError("save_termbase requires a Termbase instance")
    p.parent.mkdir(parents=True, exist_ok=True)
    payload = {
        "version": tb.version,
        "terms": [t.to_dict() for t in tb.terms],
    }
    content = json.dumps(payload, ensure_ascii=False, indent=2)
    with _save_lock:
        tmp_fd, tmp_path_str = tempfile.mkstemp(dir=str(p.parent), prefix="tb_", suffix=".tmp")
        try:
            with os.fdopen(tmp_fd, "w", encoding="utf-8") as fh:
                fh.write(content)
            os.replace(tmp_path_str, str(p))
        except BaseException:
            try:
                os.unlink(tmp_path_str)
            except OSError:
                pass
            raise


# --------------------------------------------------------------------------- #
# 索引与匹配
# --------------------------------------------------------------------------- #

def build_alias_index(tb: Termbase) -> dict[str, list[str]]:
    """构建所有小写化标签（含 pref_label / alt_labels / term_en）到 slug 列表的索引。"""
    index: dict[str, list[str]] = {}
    for term in tb.terms:
        candidates = [term.pref_label] + list(term.alt_labels)
        if term.term_en and len(term.term_en.strip()) >= MIN_EN_LABEL_LEN:
            candidates.append(term.term_en)
        for label in candidates:
            k = (label or "").strip().lower()
            if not k or len(k) < MIN_LABEL_LEN:
                continue
            if k not in index:
                index[k] = []
            if term.slug not in index[k]:
                index[k].append(term.slug)
    return index


def _has_cjk(s: str) -> bool:
    return any(
        "\u4e00" <= ch <= "\u9fff"
        or "\u3400" <= ch <= "\u4dbf"
        or "\u20000" <= ch <= "\u2a6df"
        for ch in s
    )


def find_terms_in_text(
    text: str,
    tb: Termbase,
    *,
    include_deprecated: bool = False,
) -> list[TermHit]:
    """在文本中查找术语命中。
    
    规则（termbase-handoff.md §4.3）：
    1. 候选标签 = pref_label + alt_labels + (term_en 若长 >= MIN_EN_LABEL_LEN)；
    2. 标签长 < MIN_LABEL_LEN 忽略（短黑话保护）；
    3. CJK 标签：子串直接匹配（大小写不敏感）；
    4. 拉丁标签：大小写不敏感 + 词边界环视 (?<![A-Za-z0-9_]) 与 (?![A-Za-z0-9_])，
       严格杜绝 edit 命中 editor；
    5. 返回全部命中位置，按 start 升序排序。
    """
    if not text:
        return []

    candidate_terms = tb.terms if include_deprecated else [t for t in tb.terms if t.status != "deprecated"]
    hits: list[TermHit] = []
    seen: set[tuple[str, int, int, str]] = set()

    for term in candidate_terms:
        labels_to_check: list[str] = []
        if term.pref_label:
            labels_to_check.append(term.pref_label)
        for alt in term.alt_labels:
            if alt:
                labels_to_check.append(alt)
        if term.term_en and len(term.term_en.strip()) >= MIN_EN_LABEL_LEN:
            labels_to_check.append(term.term_en)

        for label in labels_to_check:
            clean_lbl = label.strip()
            if len(clean_lbl) < MIN_LABEL_LEN:
                continue

            if _has_cjk(clean_lbl):
                t_lower = text.lower()
                l_lower = clean_lbl.lower()
                idx = 0
                while True:
                    pos = t_lower.find(l_lower, idx)
                    if pos == -1:
                        break
                    span = (term.slug, pos, pos + len(clean_lbl), clean_lbl)
                    if span not in seen:
                        seen.add(span)
                        hits.append(TermHit(term=term, matched_label=clean_lbl, start=pos, end=pos + len(clean_lbl)))
                    idx = pos + 1
            else:
                escaped = re.escape(clean_lbl)
                pattern = re.compile(rf"(?<![A-Za-z0-9_]){escaped}(?![A-Za-z0-9_])", re.IGNORECASE)
                for m in pattern.finditer(text):
                    span = (term.slug, m.start(), m.end(), clean_lbl)
                    if span not in seen:
                        seen.add(span)
                        hits.append(TermHit(term=term, matched_label=clean_lbl, start=m.start(), end=m.end()))

    hits.sort(key=lambda h: (h.start, h.end, h.term.slug))
    return hits


# --------------------------------------------------------------------------- #
# Markdown 导出
# --------------------------------------------------------------------------- #

def export_markdown(tb: Termbase) -> str:
    """自包含 Markdown 导出对照表（纯字符串拼装，无第三方库）。"""
    now_str = time.strftime("%Y-%m-%d %H:%M:%S")
    lines: list[str] = [
        f"# TCER 术语库导出 · {len(tb.terms)} 词条",
        f"> 生成时间: {now_str}",
        "",
    ]

    active_draft = [t for t in tb.terms if t.status != "deprecated"]
    deprecated = [t for t in tb.terms if t.status == "deprecated"]

    active_draft.sort(key=lambda t: (0 if t.status == "active" else 1, t.slug))
    deprecated.sort(key=lambda t: t.slug)

    for t in active_draft + deprecated:
        en_part = f"（{t.term_en}）" if t.term_en else ""
        dep_part = " （已废弃）" if t.status == "deprecated" else ""
        lines.append(f"## {t.pref_label}{en_part}{dep_part}")
        lines.append("")

        meta_parts: list[str] = []
        status_cn = STATUS_LABELS.get(t.status, t.status)
        meta_parts.append(f"**状态**: {status_cn}")
        if t.owner:
            meta_parts.append(f"**负责人**: {t.owner}")
        if t.mda_layer:
            mda_cn = MDA_LAYERS.get(t.mda_layer, t.mda_layer)
            meta_parts.append(f"**MDA 维度**: {mda_cn}")
        lines.append(" | ".join(meta_parts))
        lines.append("")

        if t.definition:
            lines.append(f"- **定义**: {t.definition}")
        if t.alt_labels:
            lines.append(f"- **别名**: {'、'.join(t.alt_labels)}")
        if t.notes:
            lines.append(f"- **备注**: {t.notes}")
        lines.append("")

        if t.renderings:
            lines.append("### 各职能理解")
            for rk in sorted(t.renderings.keys()):
                r_label = ROLE_LABELS.get(rk, rk)
                lines.append(f"- **{r_label}**: {t.renderings[rk]}")
            lines.append("")

        if t.misconceptions:
            lines.append("### 常见误解")
            for m in t.misconceptions:
                r_label = ROLE_LABELS.get(m.get("role", ""), m.get("role", "全员"))
                wrong = m.get("wrong", "")
                actual = m.get("actual", "")
                lines.append(f"- **{r_label}**: 以为 {wrong} → 实际 {actual}")
            lines.append("")

        lines.append("---")
        lines.append("")

    return "\n".join(lines).strip() + "\n"
