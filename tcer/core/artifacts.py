"""全局产物索引：收集各源写在项目目录之外、按会话归档的产物文件。

Antigravity 把 plan / walkthrough / 深度调研报告 / 子代理工作稿写在
``~/.gemini/<root>/brain/<conversation_id>/``（子代理写入**父会话**的 brain
目录），团队派发材料在 ``<root>/scratch/teamwork/<conversation_id>/``；
Claude 的计划文档在 ``~/.claude/plans/``（会话 jsonl 的 attachment
记录 ``planFilePath`` 反查归属），文件历史在 ``~/.claude/file-history/<sid>/``。

产物全部经会话内 write_file 等工具落盘——已计入 LOC，本模块只做索引
（读盘列文件，不解析内容、不进指标）。``.system_generated/`` 内部运行数据
（转录 chunks / step 载荷 / 截图素材，实测 4.3 万个）按 (会话, 二级目录)
聚合成单行 ``kind='internal'``/``media``，保持「全收但分类标注」且不让
列表塞入数万行噪声。
"""
from __future__ import annotations

import json
import os
import re
from dataclasses import dataclass
from pathlib import Path

from . import paths


@dataclass(frozen=True)
class Artifact:
    source: str          # "antigravity" | "claude"
    session_id: str      # 归属会话（子代理归到根会话，便于与卡面对齐）
    session_title: str   # 归属会话标题（拿不到时空串）
    project: str         # 项目显示名（拿不到时空串）
    path: Path           # 文件或聚合目录
    kind: str            # plan/report/doc/worker/media/code/data/backup/upload/internal/other
    zone: str            # brain/teamwork/plans/file-history
    size: int            # 聚合行 = 子树总大小
    mtime_ns: int        # 聚合行 = 最新 mtime
    count: int = 1       # 聚合行 = 文件数


KINDS = ("plan", "report", "doc", "worker", "media", "code",
         "data", "backup", "upload", "internal", "other")

_PLAN_NAMES = ("plan", "walkthrough", "implementation_plan", "design",
               "blueprint")
_REPORT_NAMES = ("research", "report", "audit", "review", "analysis", "summary",
                 "handoff", "deliver", "finding", "investigation", "调研", "报告")

_EXT_KINDS = {
    "media": {".png", ".jpg", ".jpeg", ".gif", ".webp", ".svg", ".mp4", ".mov",
              ".webm", ".ico", ".bmp", ".tif", ".tiff", ".woff", ".woff2",
              ".ttf", ".otf", ".avif", ".mp3", ".wav", ".pdf"},
    "code": {".py", ".js", ".ts", ".tsx", ".jsx", ".mjs", ".cjs", ".css",
             ".scss", ".less", ".html", ".sh", ".bat", ".ps1", ".sql", ".go",
             ".rs", ".java", ".c", ".h", ".cpp", ".hpp", ".cs", ".rb", ".php",
             ".kt", ".swift", ".vue", ".lua", ".r", ".pl", ".ex", ".exs",
             ".toml", ".ini", ".cfg", ".ipynb", ".proto", ".gradle", ".cmake"},
    "data": {".json", ".jsonl", ".csv", ".tsv", ".xml", ".yaml", ".yml",
             ".log", ".patch", ".diff", ".db", ".sqlite", ".ndjson", ".lock",
             ".env", ".properties"},
    "doc": {".md", ".txt", ".rst", ".adoc"},
}


def _classify(rel_parts: tuple[str, ...], name: str) -> str:
    """按相对路径段 + 文件名 + 扩展名定 kind；内部/上传区优先于文件名。"""
    parts = tuple(p.lower() for p in rel_parts)
    stem = name.rsplit(".", 1)[0].lower()
    ext = ("." + name.rsplit(".", 1)[1].lower()) if "." in name else ""
    if ".user_uploaded" in parts:
        return "upload"
    if ".tempmediastorage" in parts:
        return "internal"
    if "scratch" in parts:
        return "worker"
    if name.lower().endswith(".metadata.json"):
        return "data"            # 产物边车（Antigravity 在文档旁写的元数据）
    if any(k in stem for k in _PLAN_NAMES):
        return "plan"
    if any(k in stem for k in _REPORT_NAMES):
        return "report"
    for kind, exts in _EXT_KINDS.items():
        if ext in exts:
            return kind
    return "other"


def _walk_files(base: Path):
    try:
        for f in base.rglob("*"):
            try:
                if f.is_file():
                    yield f
            except OSError:
                continue
    except OSError:
        return


def _root_of(cid: str, parents: dict[str, str]) -> str:
    """沿父指针走到根会话（防环，最深 20 层）。"""
    seen: set[str] = set()
    cur = cid
    while cur in parents and cur not in seen and len(seen) < 20:
        seen.add(cur)
        cur = parents[cur]
    return cur


# --------------------------------------------------------------------------- #
# Antigravity
# --------------------------------------------------------------------------- #

def _collect_antigravity() -> list[Artifact]:
    from . import antigravity_reader as ag

    refs = {}
    try:
        for ref in ag.list_project_refs():
            for p in ref.session_paths:
                refs.setdefault(os.path.normcase(p.stem), ref)
    except Exception:
        pass
    parents, _ = ag._parent_index_full()
    titles: dict[str, str] = {}
    roots = ag.antigravity_roots()
    for root in roots:
        try:
            titles.update(ag._summaries_info(root).titles)
        except Exception:
            continue

    out: list[Artifact] = []

    def _emit(cid_dir: Path, zone: str) -> None:
        cid = cid_dir.name
        owner = _root_of(cid, parents)
        ref = refs.get(os.path.normcase(owner)) or refs.get(os.path.normcase(cid))
        project = ref.display_name if ref else ""
        title = titles.get(owner) or titles.get(cid) or ""
        sys_groups: dict[tuple[str, ...], list] = {}
        for f in _walk_files(cid_dir):
            rel = f.relative_to(cid_dir).parts
            try:
                st = f.stat()
            except OSError:
                continue
            if rel and rel[0].lower() == ".system_generated":
                # logs 下的 chunks/media 三级成组；其余按二级目录聚合
                if len(rel) > 2 and rel[1].lower() == "logs" \
                        and rel[2].lower() in ("chunks", "media"):
                    key = rel[:3]
                else:
                    key = rel[:2] if len(rel) > 1 else rel
                grp = sys_groups.setdefault(
                    key, [0, 0, 0, cid_dir / Path(*key)])
                grp[0] += 1
                grp[1] += st.st_size
                grp[2] = max(grp[2], st.st_mtime_ns)
                continue
            # teamwork 区（BRIEFING/DISPATCH/handoff 等）是 Swarm 派发协调
            # 材料，统一标 worker，不当作用户计划/报告
            kind = "worker" if zone == "teamwork" else _classify(rel, f.name)
            out.append(Artifact("antigravity", owner, title, project, f,
                                kind, zone, st.st_size, st.st_mtime_ns))
        # .system_generated 聚合行：媒体归 media，其余 internal
        for key, (n, total, mt, sub) in sys_groups.items():
            kind = "media" if key[-1].lower() == "media" else "internal"
            out.append(Artifact("antigravity", owner, title, project, sub,
                                kind, zone, total, mt, n))

    for root in roots:
        for base, zone in ((root / "brain", "brain"),
                           (root / "scratch" / "teamwork", "teamwork")):
            if not base.is_dir():
                continue
            try:
                cid_dirs = [d for d in base.iterdir() if d.is_dir()]
            except OSError:
                continue
            for cid_dir in cid_dirs:
                _emit(cid_dir, zone)
    return out


# --------------------------------------------------------------------------- #
# Claude
# --------------------------------------------------------------------------- #

# normcase(jsonl 路径) → (签名, [planFilePath 归一化路径])
_PLAN_LINKS: dict[str, tuple[tuple, list[str]]] = {}
_PLAN_RE = re.compile(rb'"planFilePath"\s*:\s*"((?:[^"\\]|\\.)*)"')


def _norm(p: str | Path) -> str:
    return os.path.normcase(os.path.normpath(str(p)))


def _scan_plan_links(jsonl: Path) -> list[str]:
    """会话 jsonl 中的 planFilePath 列表（attachment 结构化记录），按签名缓存。"""
    key = _norm(jsonl)
    try:
        sig = (jsonl.stat().st_mtime_ns, jsonl.stat().st_size)
    except OSError:
        return []
    cached = _PLAN_LINKS.get(key)
    if cached is not None and cached[0] == sig:
        return cached[1]
    try:
        data = jsonl.read_bytes()
    except OSError:
        return []
    plans: list[str] = []
    if b"planFilePath" in data:
        for m in _PLAN_RE.findall(data):
            try:
                plans.append(_norm(json.loads(b'"' + m + b'"')))
            except Exception:
                continue
    _PLAN_LINKS[key] = (sig, plans)
    return plans


def _collect_claude() -> list[Artifact]:
    out: list[Artifact] = []
    roots = paths.claude_config_dirs()

    # sid → 项目 hash 目录名（jsonl 文件名即会话 uuid）
    sid2proj: dict[str, str] = {}
    jsonls: list[Path] = []
    for root in roots:
        projs = root / "projects"
        if not projs.is_dir():
            continue
        try:
            hdirs = [d for d in projs.iterdir() if d.is_dir()]
        except OSError:
            continue
        for hd in hdirs:
            for j in hd.glob("*.jsonl"):
                sid2proj.setdefault(j.stem, hd.name)
                jsonls.append(j)

    # plans/*.md ← 会话 planFilePath 反查（全量扫只在有 plans 时进行）
    plan2sid: dict[str, str] = {}
    for root in roots:
        plans_dir = root / "plans"
        if not plans_dir.is_dir():
            continue
        plan_files = [f for f in _walk_files(plans_dir)]
        if not plan_files:
            continue
        want = {_norm(f): f for f in plan_files}
        for j in jsonls:
            for p in _scan_plan_links(j):
                plan2sid.setdefault(p, j.stem)
        for key, f in want.items():
            sid = plan2sid.get(key, "")
            try:
                st = f.stat()
            except OSError:
                continue
            out.append(Artifact("claude", sid, "",
                                sid2proj.get(sid, ""), f, "plan", "plans",
                                st.st_size, st.st_mtime_ns))

    # file-history/<sid>/<hash>@vN → 历史备份
    for root in roots:
        fh = root / "file-history"
        if not fh.is_dir():
            continue
        try:
            sid_dirs = [d for d in fh.iterdir() if d.is_dir()]
        except OSError:
            continue
        for sd in sid_dirs:
            sid = sd.name
            proj = sid2proj.get(sid, "")
            for f in _walk_files(sd):
                try:
                    st = f.stat()
                except OSError:
                    continue
                out.append(Artifact("claude", sid, "", proj, f,
                                    "backup", "file-history",
                                    st.st_size, st.st_mtime_ns))
    return out


# --------------------------------------------------------------------------- #
# Public API
# --------------------------------------------------------------------------- #

def collect(progress=None) -> list[Artifact]:
    """收集全部源的会话产物，按 mtime 倒序。progress(phase, n) 报告阶段进度。

    纯离线、只列目录与 stat；产物文件本身不读内容（planFilePath 反查除外）。
    """
    out = _collect_antigravity()
    if progress:
        progress("antigravity", len(out))
    out.extend(_collect_claude())
    if progress:
        progress("claude", len(out))
    out.sort(key=lambda a: a.mtime_ns, reverse=True)
    return out
