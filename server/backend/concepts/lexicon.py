"""S2 词汇层（文档 §5.2）。

- 已知术语命中：直接复用 ``tcer.core.termbase.find_terms_in_text``（拉丁词边界 /
  CJK 子串 / 短词保护三条规则与客户端一致，不另写）。
- 新词挖掘：CJK 2–6 字 n-gram + 拉丁 token/短语，频次与人数下限过滤，G² 对照背景
  语料（AI 的通用措辞）打分，再做子串吸收。纯标准库。
"""
from __future__ import annotations

import math
import re
from collections import Counter, defaultdict

from tcer.core import termbase as tb_mod

_CJK = "㐀-䶿一-鿿"
_CJK_RUN_RE = re.compile(f"[{_CJK}]+")
_LATIN_TOKEN_RE = re.compile(r"[A-Za-z][A-Za-z0-9_\-\.#+]*[A-Za-z0-9+#]|[A-Za-z]{2,}")

CJK_MIN_N, CJK_MAX_N = 2, 6
MIN_FREQ = 5
MIN_PERSONS = 2
# 邻接字多样性（accessor variety）：CJK 候选左右两侧各至少出现 N 种不同的邻接字
# （句首 / 句尾每次算一种，但至少一侧要有真实邻接字），否则判为跨词边界的截断片段（「战令系统第」「励配置要调整」）。
MIN_ACCESSOR_VARIETY = 2
MAX_CANDIDATES = 300
_SCAN_CHARS = 2000   # 单条 utterance 参与挖掘的字符上限（长粘贴不拖垮统计）

# 中文 n-gram 首尾不能是虚词/代词/量词（「的问题」「这个」之类没有术语价值）。
_CJK_EDGE_STOP = set("的了是在和就也都而及与着或一这那我你他她它们吗呢吧啊呀么个把被给让从对向到"
                     "为以于之其并且但又再很太更最还只已要会能可将该每各些所如若则即便因此"
                     "下上中里外来去说看做用有没不")
_CJK_STOP_GRAMS = {
    "一下", "一个", "这个", "那个", "什么", "怎么", "为什么", "可以", "需要", "现在", "然后",
    "如果", "因为", "所以", "但是", "已经", "还是", "就是", "或者", "应该", "不要", "问题",
    "时候", "东西", "情况", "方式", "部分", "内容", "文件", "代码", "功能", "修改", "实现",
    "看看", "帮我", "一些", "这里", "那里", "所有", "继续", "直接", "目前", "当前", "之后",
    "之前", "以及", "进行", "处理", "使用", "没有", "不是", "我们", "你们", "他们", "自己",
}
_EN_STOP = {
    "the", "and", "for", "that", "this", "with", "you", "are", "not", "but", "can", "have",
    "was", "all", "use", "from", "will", "should", "would", "could", "there", "then", "than",
    "what", "when", "which", "into", "your", "just", "like", "also", "please", "let", "make",
    "need", "want", "file", "files", "code", "line", "lines", "test", "tests", "run", "yes",
    "now", "here", "only", "one", "two", "new", "get", "set", "see", "try", "fix", "add",
    "http", "https", "www", "com", "true", "false", "none", "null", "json", "todo", "ok",
    "etc", "via", "per", "how", "why", "any", "each", "its", "our", "out", "has", "had",
}


def find_hits(text: str, tb: tb_mod.Termbase) -> list[tb_mod.TermHit]:
    """已知术语命中，重叠命中保留最长的一条（「上下文窗口」不再额外算「窗口」）。"""
    hits = tb_mod.find_terms_in_text(text, tb)
    hits.sort(key=lambda h: (h.start, -(h.end - h.start)))
    out: list[tb_mod.TermHit] = []
    last_end = -1
    for h in hits:
        if h.start < last_end:
            continue
        out.append(h)
        last_end = h.end
    return out


def _grams(text: str) -> set[str]:
    """一条 utterance 的候选集合（按条去重，频次 = 出现的 utterance 数，抗刷屏）。"""
    t = text[:_SCAN_CHARS]
    out: set[str] = set()
    for run in _CJK_RUN_RE.findall(t):
        L = len(run)
        for n in range(CJK_MIN_N, CJK_MAX_N + 1):
            for i in range(0, L - n + 1):
                g = run[i:i + n]
                if g[0] in _CJK_EDGE_STOP or g[-1] in _CJK_EDGE_STOP:
                    continue
                if g in _CJK_STOP_GRAMS:
                    continue
                out.add(g)
    toks = [m.group(0) for m in _LATIN_TOKEN_RE.finditer(t)]
    norm = [w.strip(".-").lower() for w in toks]
    for w, raw in zip(norm, toks):
        if len(w) < 3 or w in _EN_STOP or w.isdigit():
            continue
        # 纯小写常见英文单词信息量低；保留含大写 / 数字 / 符号的（SLO、P99、C#）与长词。
        if raw.islower() and len(w) < 5:
            continue
        out.add(w)
    for a, b in zip(norm, norm[1:]):
        if len(a) >= 3 and len(b) >= 3 and a not in _EN_STOP and b not in _EN_STOP:
            out.add(f"{a} {b}")
    return out


def g2(a: int, b: int, n1: int, n2: int) -> float:
    """对数似然比（Dunning 1993）。a/n1 = 目标语料频率，b/n2 = 背景语料频率。

    只对「目标更常用」的方向给正分，否则 0。
    """
    if n1 <= 0 or a <= 0:
        return 0.0
    if n2 <= 0:
        return float(a)
    if a / n1 <= b / n2:
        return 0.0
    total = n1 + n2
    e1 = n1 * (a + b) / total
    e2 = n2 * (a + b) / total

    def term(o, e):
        return o * math.log(o / e) if o > 0 and e > 0 else 0.0

    return 2.0 * (term(a, e1) + term(b, e2))


def mine_candidates(user_utts: list[dict], background_texts: list[str],
                    known_labels: set[str]) -> list[dict]:
    """从 user 侧 utterance 挖候选新词。

    ``user_utts``: ``[{text, person, project}]``；``known_labels``：已入库标签（小写），
    命中的跳过。返回按 G² 降序、已做子串吸收的候选列表。
    """
    freq: Counter = Counter()
    persons: dict[str, set] = defaultdict(set)
    projects: dict[str, set] = defaultdict(set)
    for u in user_utts:
        for g in _grams(u["text"]):
            freq[g] += 1
            if u.get("person"):
                persons[g].add(u["person"])
            if u.get("project"):
                projects[g].add(u["project"])
    pool = [g for g, n in freq.items()
            if n >= MIN_FREQ and len(persons[g]) >= MIN_PERSONS and g.lower() not in known_labels]
    pool = _accessor_filter(pool, [u["text"] for u in user_utts])
    if not pool:
        return []
    bg: Counter = Counter()
    pool_set = set(pool)
    for t in background_texts:
        for g in _grams(t) & pool_set:
            bg[g] += 1
    n1, n2 = len(user_utts), len(background_texts)
    scored = []
    for g in pool:
        s = g2(freq[g], bg[g], n1, n2)
        if s <= 0 and n2 > 0:
            continue
        scored.append((s if n2 > 0 else float(freq[g] * len(persons[g])), g))
    scored.sort(reverse=True)
    top = [g for _, g in scored[:MAX_CANDIDATES * 3]]
    score_of = {g: s for s, g in scored}
    kept = _absorb_substrings(top, freq)
    out = []
    for g in kept[:MAX_CANDIDATES]:
        out.append({"surface": g, "g2": round(score_of[g], 3), "freq": freq[g],
                    "persons": sorted(persons[g]), "projects": sorted(projects[g])})
    return out


def _accessor_filter(pool: list[str], texts: list[str]) -> list[str]:
    """CJK 候选按邻接字多样性过滤；拉丁候选天然有分隔符，原样保留。"""
    cjk = {g for g in pool if _CJK_RUN_RE.fullmatch(g)}
    if not cjk:
        return pool
    left: dict[str, set] = defaultdict(set)
    right: dict[str, set] = defaultdict(set)
    edge = 0
    for t in texts:
        for run in _CJK_RUN_RE.findall(t[:_SCAN_CHARS]):
            L = len(run)
            for n in range(CJK_MIN_N, min(CJK_MAX_N, L) + 1):
                for i in range(0, L - n + 1):
                    g = run[i:i + n]
                    if g not in cjk:
                        continue
                    edge += 1
                    left[g].add(run[i - 1] if i > 0 else f"^{edge}")
                    right[g].add(run[i + n] if i + n < L else f"${edge}")

    def real(xs: set) -> int:
        return sum(1 for x in xs if x[0] not in "^$")

    def ok(g: str) -> bool:
        # 两侧各 ≥N 种邻接（句首/句尾每次算一种，句首词如「战令…」不被误杀）；
        # 且至少一侧有 ≥N 种**真实**邻接字——两侧都只靠边界凑数的是夹在数字/
        # 英文之间的碎片（「5 秒改成 3 秒」里的「秒改成」）。
        return (len(left[g]) >= MIN_ACCESSOR_VARIETY and len(right[g]) >= MIN_ACCESSOR_VARIETY
                and max(real(left[g]), real(right[g])) >= MIN_ACCESSOR_VARIETY)

    return [g for g in pool if g not in cjk or ok(g)]


def _absorb_substrings(cands: list[str], freq: Counter) -> list[str]:
    """被更长候选包含且频次几乎相同（≥90%）的子串丢弃；更长的片段若只是更短
    候选的偶发扩展（频次 <50%）也丢弃。"""
    cand_set = set(cands)
    drop: set[str] = set()
    for s in cands:
        for L in cands:
            if L == s or len(L) <= len(s) or s not in L:
                continue
            if freq[L] >= 0.9 * freq[s]:
                drop.add(s)
            elif freq[L] < 0.5 * freq[s]:
                drop.add(L)
    return [c for c in cands if c in cand_set and c not in drop]


def nearest_slug(surface: str, tb: tb_mod.Termbase) -> str | None:
    """疑似某已有术语的别名：标签互相包含，或共享 ≥2 字公共片段（CJK）。"""
    s = surface.lower()
    best = None
    for t in tb.terms:
        labels = [t.pref_label, t.term_en] + list(t.alt_labels)
        for lbl in labels:
            l = (lbl or "").lower()
            if len(l) < 2:
                continue
            if l in s or s in l:
                return t.slug
            if best is None and len(s) >= 2 and any(s[i:i + 2] in l for i in range(len(s) - 1)) \
                    and _CJK_RUN_RE.fullmatch(s):
                best = t.slug
    return best


def known_label_set(tb: tb_mod.Termbase) -> set[str]:
    out = set()
    for t in tb.terms:
        for lbl in [t.pref_label, t.term_en] + list(t.alt_labels):
            if lbl:
                out.add(lbl.strip().lower())
    return out
