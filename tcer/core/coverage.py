"""数据覆盖与守恒审计：原始数据普查 → 已处理 / 已知忽略 / 未知。

closed-loop audit（``audit.py``）用同一套解析逻辑重算，只能证明「自洽」；
数据源格式一漂移（新行类型、新 usage 字段、新取值），解析器会静默跳过，
自洽检查照样全绿。本模块补上这一块：

1. **普查**：直接逐行 ``json.loads`` 原始文件（不经 reader），统计每个维度
   （行类型、system 子类型、内容块、usage 键与取值……）实际出现的值。
2. **登记表**：每个值必须登记为「已处理」（reader 消费它）或「已知忽略」
   （写明理由）；未登记的一律 **未知**，取值超出白名单的一律 **取值异常**。
   上游升级引入的新东西因此会被点名，而不是悄悄漏计。
3. **守恒**：调用 ↔ 结果、usage 行 ↔ message.id、流式快照只增不减、
   子代理 ↔ 父会话等关系，以及 reader 工具调用数与独立计数的交叉核对。

报告只陈述「见过什么、处理了什么、跳过了什么」，不判断数值对错；
外部真值（如真实账单）不在本地数据里，无法由本模块证明。
"""
from __future__ import annotations

import json
import os
from collections import Counter
from dataclasses import dataclass, field
from pathlib import Path
from typing import Iterable

HANDLED = "handled"
IGNORED = "ignored"
UNKNOWN = "unknown"
UNEXPECTED = "unexpected"

# 普查已覆盖到的最高版本：登记表按此版本及以下的真实数据整理（2026-10-27）。
CLAUDE_CENSUS_VERSION = "2.1.311"

# --------------------------------------------------------------------------- #
# Claude 登记表：维度 → {值: (状态, 说明)}
# --------------------------------------------------------------------------- #
_H, _I = HANDLED, IGNORED
_CTX = "上下文注入/提醒，不产生用量、不影响指标"

CLAUDE_REGISTRY: dict[str, dict[str, tuple[str, str]]] = {
    "type": {
        "assistant": (_H, "回复：usage/模型/工具调用/思考"),
        "user": (_H, "提问（user_input SSOT）/工具结果/图片"),
        "system": (_H, "按 subtype 分派"),
        "attachment": (_H, "按 attachment.type 分派"),
        "mode": (_H, "计划模式"),
        "cost-state": (_H, "自报累计成本（取最大值）"),
        "queue-operation": (_H, "运行中排队输入"),
        "api-request-blob": (_I, "请求存档，不是会话事件（#15）"),
        "api-request": (_I, "请求参数存档（2.1.300+），无用量；只用于请求↔回复守恒"),
        "api-request-shape": (_I, "请求结构哈希存档，无用量"),
        "last-prompt": (_I, "上次输入缓存，非提问事件"),
        "atis-latch": (_I, "客户端内部标记"),
        "ai-title": (_I, "自动标题文本；生成它的 LLM 调用用量不落盘"),
        "custom-title": (_I, "用户自定标题"),
        "permission-mode": (_I, "权限模式切换"),
        "file-history-delta": (_I, "文件历史（rewind 用），非编辑事件"),
        "file-history-snapshot": (_I, "文件历史快照（rewind 用），非编辑事件"),
        "agent-name": (_I, "会话命名"),
        "tools-used": (_I, "工具名汇总，重复 tool_use 信息"),
        "started": (_I, "工作流子任务缓存开始标记，无用量"),
        "result": (_I, "工作流子任务结构化结果缓存，无用量"),
        "fork-context-ref": (_I, "子代理继承父上下文的引用，无用量"),
        "summary": (_I, "旧版会话摘要行"),
    },
    "system.subtype": {
        "stop_hook_summary": (_H, "hook 次数/错误/耗时"),
        "turn_duration": (_H, "回合墙钟"),
        "api_error": (_H, "429 限流"),
        "compact_boundary": (_H, "压缩次数/代价"),
        "away_summary": (_I, "离开摘要文本；生成它的 LLM 调用用量不落盘"),
        "local_command": (_I, "本地斜杠命令输出"),
        "scheduled_task_fire": (_I, "定时任务触发标记（其回复照常计入）"),
        "informational": (_I, "提示文本"),
        "model_refusal_fallback": (_I, "安全拦截后换模型；换后的回复自带 model 照常计"),
    },
    "attachment.type": {
        "plan_mode": (_H, "计划模式"),
        "plan_mode_reentry": (_H, "计划模式（重入）"),
        "read_truncation_notice": (_H, "读取截断"),
        **{k: (_I, _CTX) for k in (
            "total_tokens_reminder", "todo_reminder", "prompt_snapshot", "environment",
            "skill_listing", "agent_listing_delta", "date", "model", "session_context",
            "edited_text_file", "instructions", "language", "silent_turn_reminder",
            "mcp_instructions_delta", "queued_command", "task_reminder", "auto_mode",
            "nested_memory", "deferred_tools_delta", "file", "plan_mode_exit",
            "date_change", "compact_file_reference", "command_permissions",
            "deferred_tools_record", "batching_reminder_sent", "plan_file_reference",
            "thinking_drop", "invoked_skills", "hook_additional_context", "task_status",
        )},
    },
    "block.assistant": {
        "tool_use": (_H, "工具调用/LOC 回放"),
        "server_tool_use": (_H, "服务端工具调用"),
        "thinking": (_H, "思考块计数"),
        "text": (_I, "回复正文，不计数"),
        "redacted_thinking": (_I, "加密思考块"),
        "tool_result": (_I, "第三方代理把服务端工具结果写进 assistant（实测 glm-5.3），不计错误"),
    },
    "block.user": {
        "tool_result": (_H, "工具错误/LOC 结果裁决"),
        "text": (_H, "提问（user_input SSOT）"),
        "image": (_H, "图片输入"),
        "document": (_I, "文档附件"),
    },
    "usage": {
        "input_tokens": (_H, "计费"),
        "output_tokens": (_H, "计费"),
        "cache_creation_input_tokens": (_H, "计费"),
        "cache_read_input_tokens": (_H, "计费"),
        "cache_creation": (_H, "1h 缓存写溢价"),
        "output_tokens_details": (_H, "思考 Token"),
        "server_tool_use": (_H, "网页搜索/抓取次数"),
        "estimated": (_H, "客户端估算占位，不计"),
        "service_tier": (_I, "取值受白名单检查（非 standard 档价格不同）"),
        "speed": (_I, "取值受白名单检查（fast 档价格不同）"),
        "inference_geo": (_I, "取值受白名单检查（数据驻留可能溢价）"),
        "fallback_credit": (_I, "取值受白名单检查"),
        "iterations": (_I, "受守恒检查（多于 1 项时顶层口径未知）"),
    },
    "usage.cache_creation": {
        "ephemeral_1h_input_tokens": (_H, "1h 缓存写"),
        "ephemeral_5m_input_tokens": (_I, "= 缓存写 − 1h 部分，按基础价"),
    },
    "usage.server_tool_use": {
        "web_search_requests": (_H, "网页搜索"),
        "web_fetch_requests": (_H, "网页抓取"),
    },
    "usage.output_tokens_details": {
        "thinking_tokens": (_H, "思考 Token（含在 output 内）"),
    },
}

# 影响计价的取值白名单（JSON 序列化后的值；null = 字段存在但为空）。
CLAUDE_VALUE_ALLOW: dict[str, frozenset[str]] = {
    "usage.service_tier": frozenset({'"standard"', "null"}),
    "usage.speed": frozenset({'"standard"', "null"}),
    "usage.inference_geo": frozenset({'""', '"global"', '"not_available"', "null"}),
    "usage.fallback_credit": frozenset({"null"}),
}

_BILL_KEYS = ("input_tokens", "output_tokens", "cache_read_input_tokens",
              "cache_creation_input_tokens")
_EXAMPLES_PER_ISSUE = 3


@dataclass
class CoverageEntry:
    dim: str
    value: str
    count: int
    status: str
    note: str = ""
    min_version: str = ""
    max_version: str = ""
    example: str = ""  # 未知/异常项的首个出现位置（文件:行号）


@dataclass
class CoverageCheck:
    name: str
    ok: bool
    level: str  # "warn"：失败需处理；"info"：如实列出、不判失败
    detail: str
    examples: list[str] = field(default_factory=list)


@dataclass
class CoverageReport:
    source: str
    files: int = 0
    lines: int = 0
    bad_json: int = 0
    max_version: str = ""
    census_version: str = ""
    entries: list[CoverageEntry] = field(default_factory=list)
    checks: list[CoverageCheck] = field(default_factory=list)

    @property
    def issues(self) -> list[CoverageEntry]:
        return [e for e in self.entries if e.status in (UNKNOWN, UNEXPECTED)]

    @property
    def failed_checks(self) -> list[CoverageCheck]:
        return [c for c in self.checks if not c.ok and c.level == "warn"]

    @property
    def ok(self) -> bool:
        return not self.issues and not self.failed_checks

    @property
    def newer_than_census(self) -> bool:
        return _vkey(self.max_version) > _vkey(self.census_version)


def _vkey(v: str) -> tuple[int, ...]:
    out = []
    for part in (v or "").split("."):
        try:
            out.append(int(part))
        except ValueError:
            break
    return tuple(out)


class _Tally:
    """维度/值计数 + 版本区间 + 首个出现位置。"""

    def __init__(self) -> None:
        self.counts: Counter = Counter()
        self.vrange: dict[tuple[str, str], list[str]] = {}
        self.where: dict[tuple[str, str], str] = {}

    def add(self, dim: str, value, version: str, where: str) -> None:
        key = (dim, value if isinstance(value, str) else json.dumps(value, ensure_ascii=False))
        self.counts[key] += 1
        if key not in self.where:
            self.where[key] = where
        if version:
            r = self.vrange.get(key)
            if r is None:
                self.vrange[key] = [version, version]
            else:
                if _vkey(version) < _vkey(r[0]):
                    r[0] = version
                if _vkey(version) > _vkey(r[1]):
                    r[1] = version


def claude_files(roots: Iterable[Path] | None = None) -> list[Path]:
    """All Claude transcript JSONL files (main sessions + subagents) under every root."""
    if roots is None:
        from tcer.core.paths import claude_config_dirs
        roots = claude_config_dirs()
    out: list[Path] = []
    for root in roots:
        base = Path(root) / "projects"
        if not base.is_dir():
            continue
        for dp, _dn, fn in os.walk(base):
            out.extend(Path(dp) / f for f in fn if f.endswith(".jsonl"))
    return sorted(out)


def _num(v) -> int:
    return int(v) if isinstance(v, (int, float)) and not isinstance(v, bool) else 0


def claude_coverage(
    files: Iterable[Path] | None = None,
    *,
    cross_check_reader: bool = True,
) -> CoverageReport:
    """Census raw Claude JSONL against :data:`CLAUDE_REGISTRY` and run conservation checks."""
    files = list(files) if files is not None else claude_files()
    rep = CoverageReport(source="claude", census_version=CLAUDE_CENSUS_VERSION)
    tally = _Tally()

    orphan_results: list[str] = []
    pending_calls: list[str] = []
    usage_no_id: list[str] = []
    input_shrinks: list[str] = []
    multi_iter: list[str] = []
    missing_parent: list[str] = []
    reader_mismatch: list[str] = []
    n_orphan = n_pending = n_no_id = n_shrink = n_multi = 0
    req_total = resp_total = 0
    files_req_gap: list[str] = []

    for path in files:
        rep.files += 1
        is_sub = path.parent.name == "subagents"
        if is_sub:
            parent = path.parent.parent.with_suffix(".jsonl")
            if not parent.is_file():
                missing_parent.append(str(path))
        uses: dict[str, str] = {}
        server_uses: set[str] = set()
        server_uses_noid = 0
        results: set[str] = set()
        first_bill: dict[str, tuple[int, int, int]] = {}
        resp_ids: set[str] = set()
        n_req = 0
        try:
            fh = open(path, encoding="utf-8", errors="replace")
        except OSError:
            continue
        with fh:
            for ln, line in enumerate(fh, 1):
                line = line.strip()
                if not line:
                    continue
                rep.lines += 1
                try:
                    o = json.loads(line)
                except ValueError:
                    rep.bad_json += 1
                    continue
                if not isinstance(o, dict):
                    continue
                where = f"{path}:{ln}"
                ver = o.get("version") if isinstance(o.get("version"), str) else ""
                if ver and _vkey(ver) > _vkey(rep.max_version):
                    rep.max_version = ver
                t = o.get("type")
                tally.add("type", t, ver, where)
                if t == "system":
                    tally.add("system.subtype", o.get("subtype"), ver, where)
                elif t == "attachment":
                    att = o.get("attachment")
                    tally.add("attachment.type", att.get("type") if isinstance(att, dict) else None,
                              ver, where)
                elif t == "api-request":
                    q = o.get("querySource")
                    if q == "repl_main_thread" or q == "sdk" or (
                            isinstance(q, str) and q.startswith("agent:")):
                        n_req += 1
                if t not in ("user", "assistant") or o.get("isMeta") is True:
                    continue
                msg = o.get("message")
                if not isinstance(msg, dict):
                    continue
                role = msg.get("role")
                content = msg.get("content")
                if isinstance(content, list):
                    for it in content:
                        if not isinstance(it, dict):
                            continue
                        bt = it.get("type")
                        tally.add(f"block.{role}", bt, ver, where)
                        if role == "assistant" and bt == "tool_use":
                            tid = it.get("id")
                            if isinstance(tid, str) and tid:
                                uses.setdefault(tid, str(it.get("name")))
                        elif role == "assistant" and bt == "server_tool_use":
                            tid = it.get("id")
                            if isinstance(tid, str) and tid:
                                server_uses.add(tid)
                            else:
                                server_uses_noid += 1
                        elif role == "user" and bt == "tool_result":
                            tid = it.get("tool_use_id")
                            if isinstance(tid, str) and tid:
                                results.add(tid)
                if role != "assistant":
                    continue
                usage = msg.get("usage")
                if not isinstance(usage, dict):
                    continue
                for k, v in usage.items():
                    tally.add("usage", k, ver, where)
                    if f"usage.{k}" in CLAUDE_VALUE_ALLOW:
                        # 取值维度统一按 JSON 序列化比对（区分 "" / null / 字符串）
                        tally.add(f"usage.{k}=", json.dumps(v, ensure_ascii=False), ver, where)
                    if isinstance(v, dict) and f"usage.{k}" in CLAUDE_REGISTRY:
                        for kk in v:
                            tally.add(f"usage.{k}", kk, ver, where)
                mid = msg.get("id")
                bill = tuple(_num(usage.get(k)) for k in _BILL_KEYS)
                if any(bill) and usage.get("estimated") is not True:
                    if not (isinstance(mid, str) and mid):
                        n_no_id += 1
                        if len(usage_no_id) < _EXAMPLES_PER_ISSUE:
                            usage_no_id.append(where)
                    else:
                        resp_ids.add(mid)
                        ins = (bill[0], bill[2], bill[3])
                        prev = first_bill.setdefault(mid, ins)
                        if any(a < b for a, b in zip(ins, prev)):
                            n_shrink += 1
                            if len(input_shrinks) < _EXAMPLES_PER_ISSUE:
                                input_shrinks.append(f"{where} {mid} {prev}→{ins}")
                its = usage.get("iterations")
                if isinstance(its, list) and its:
                    sums = tuple(sum(_num(x.get(k)) for x in its if isinstance(x, dict))
                                 for k in _BILL_KEYS)
                    if len(its) > 1 or sums != bill:
                        n_multi += 1
                        if len(multi_iter) < _EXAMPLES_PER_ISSUE:
                            multi_iter.append(f"{where} len={len(its)} sum={sums} top={bill}")

        orphans = results - set(uses)
        pend = set(uses) - results
        n_orphan += len(orphans)
        n_pending += len(pend)
        if orphans and len(orphan_results) < _EXAMPLES_PER_ISSUE:
            orphan_results.append(f"{path} ×{len(orphans)}")
        if pend and len(pending_calls) < 10:
            names = Counter(uses[t] for t in pend)
            pending_calls.append(f"{path} {dict(names)}")
        if n_req:
            req_total += n_req
            resp_total += len(resp_ids)
            if n_req > len(resp_ids) and len(files_req_gap) < 5:
                files_req_gap.append(f"{path} 请求 {n_req} / 回复 {len(resp_ids)}")

        if cross_check_reader:
            from tcer.core import reader
            u, _ = reader._scan_session_uncached(
                path, with_loc=False, include_user_texts=False, cancel_check=None)
            expect = len(uses) + len(server_uses) + server_uses_noid
            got = sum(u.tool_calls.values())
            if got != expect:
                reader_mismatch.append(f"{path} reader={got} 独立计数={expect}")

    # ---- 登记表分类 ----
    for (dim, value), cnt in sorted(tally.counts.items(), key=lambda kv: (kv[0][0], -kv[1])):
        vr = tally.vrange.get((dim, value), ["", ""])
        if dim.endswith("="):
            allow = CLAUDE_VALUE_ALLOW.get(dim[:-1], frozenset())
            status = HANDLED if value in allow else UNEXPECTED
            note = "白名单内" if status == HANDLED else "取值超出白名单：可能影响计价，需核实"
        else:
            reg = CLAUDE_REGISTRY.get(dim, {})
            status, note = reg.get(value, (UNKNOWN, "未登记：reader 未处理，需判断是否影响指标"))
        rep.entries.append(CoverageEntry(
            dim=dim, value=value, count=cnt, status=status, note=note,
            min_version=vr[0], max_version=vr[1],
            example=tally.where.get((dim, value), "") if status in (UNKNOWN, UNEXPECTED) else "",
        ))

    # ---- 守恒检查 ----
    rep.checks += [
        CoverageCheck("tool_results_have_calls", n_orphan == 0, "warn",
                      f"找不到对应调用的工具结果 {n_orphan} 个", orphan_results),
        CoverageCheck("calls_have_results", n_pending == 0, "info",
                      f"没有结果的工具调用 {n_pending} 个（进行中/中断的会话属正常；"
                      f"编辑类按成功回放 LOC）", pending_calls),
        CoverageCheck("usage_lines_have_id", n_no_id == 0, "warn",
                      f"有用量但无 message.id 的行 {n_no_id} 个（无法去重）", usage_no_id),
        CoverageCheck("snapshot_input_not_shrinking", n_shrink == 0, "info",
                      f"同一回复后续行输入/缓存变小 {n_shrink} 次（reader 取最大值；"
                      f"哪个值是真实计费无法从本地数据判断）", input_shrinks),
        CoverageCheck("iterations_consistent", n_multi == 0, "warn",
                      f"usage.iterations 多于 1 项或与顶层不符 {n_multi} 次（顶层口径未知）",
                      multi_iter),
        CoverageCheck("subagent_parent_exists", not missing_parent, "warn",
                      f"找不到父会话的子代理文件 {len(missing_parent)} 个",
                      missing_parent[:_EXAMPLES_PER_ISSUE]),
        CoverageCheck("requests_vs_responses", req_total <= resp_total, "info",
                      f"api-request 记录的请求 {req_total} / 有用量的回复 {resp_total}"
                      f"（仅 2.1.300+ 有请求记录；差额为报错/拒绝/中断的请求，"
                      f"是否计费本地无法判断）", files_req_gap),
    ]
    if cross_check_reader:
        rep.checks.append(CoverageCheck(
            "reader_tool_calls_match", not reader_mismatch, "warn",
            f"reader 工具调用数与独立计数不符的文件 {len(reader_mismatch)} 个",
            reader_mismatch[:_EXAMPLES_PER_ISSUE]))
    rep.checks.append(CoverageCheck(
        "version_within_census", not rep.newer_than_census, "info",
        f"数据最高版本 {rep.max_version or '-'}，登记表普查版本 {rep.census_version}"
        + ("：有更新版本的数据，登记表未覆盖其新增取值时会在上方列为未知" if rep.newer_than_census else ""),
    ))
    return rep


_STATUS_LABEL = {HANDLED: "已处理", IGNORED: "已知忽略", UNKNOWN: "未知", UNEXPECTED: "取值异常"}


def format_report(rep: CoverageReport, *, verbose: bool = False) -> str:
    """Human-readable report: issues first, then the full ledger (verbose) and checks."""
    lines = [f"覆盖审计 [{rep.source}] 文件 {rep.files} · 行 {rep.lines:,}"
             f"{f' · JSON 解析失败 {rep.bad_json}' if rep.bad_json else ''}"
             f" · 最高版本 {rep.max_version or '-'}（普查版本 {rep.census_version}）"]
    by_status = Counter(e.status for e in rep.entries)
    lines.append("  登记分类：" + " · ".join(
        f"{_STATUS_LABEL[s]} {by_status.get(s, 0)} 项" for s in (HANDLED, IGNORED, UNKNOWN, UNEXPECTED)))
    if rep.issues:
        lines.append("  ── 需处理：未登记 / 取值异常 ──")
        for e in rep.issues:
            lines.append(f"    [{_STATUS_LABEL[e.status]}] {e.dim.rstrip('=')} = {e.value} ×{e.count:,}"
                         f"  版本 {e.min_version or '-'}~{e.max_version or '-'}  首见 {e.example}")
            if e.note:
                lines.append(f"        说明：{e.note}")
    if verbose:
        lines.append("  ── 完整登记 ──")
        for e in rep.entries:
            lines.append(f"    {_STATUS_LABEL[e.status]:<4} {e.dim:<28} {e.value:<32} "
                         f"{e.count:>9,}  {e.note}")
    lines.append("  ── 守恒检查 ──")
    for c in rep.checks:
        mark = "通过" if c.ok else ("失败" if c.level == "warn" else "提示")
        lines.append(f"    [{mark}] {c.name}: {c.detail}")
        if not c.ok:
            lines.extend(f"        - {x}" for x in c.examples)
    lines.append(f"  结论：{'PASS' if rep.ok else 'FAIL'}"
                 + ("" if rep.ok else f"（未知/异常 {len(rep.issues)} 项，失败检查 {len(rep.failed_checks)} 项）"))
    return "\n".join(lines)


def report_dict(rep: CoverageReport) -> dict:
    return {
        "source": rep.source, "files": rep.files, "lines": rep.lines, "bad_json": rep.bad_json,
        "max_version": rep.max_version, "census_version": rep.census_version, "ok": rep.ok,
        "entries": [e.__dict__ for e in rep.entries],
        "checks": [c.__dict__ for c in rep.checks],
    }


# =========================================================================== #
# Antigravity 覆盖审计（2026-11-15 起；SQLite + Protobuf，普查只用 wire 解码器）
# =========================================================================== #
# 普查已覆盖到的各端版本（language server 版本，取自 ``<root>/log/*.log``）。
ANTIGRAVITY_CENSUS_VERSIONS: dict[str, str] = {
    "antigravity-cli": "1.5.9",   # CLI（agy）
    "antigravity": "2.32.0",      # 桌面 App 2.0
}

_NONUSE = "无用量，不影响指标"
_PASS = "原名计入工具画像（非读写类）"

ANTIGRAVITY_REGISTRY: dict[str, dict[str, tuple[str, str]]] = {
    "db.table": {
        "steps": (_H, "事件流：提问/工具调用/结果/压缩"),
        "gen_metadata": (_H, "逐生成 usage 与模型（权威）"),
        "trajectory_metadata_blob": (_H, "工作区 URI + 子代理父指针（字段 5）"),
        "trajectory_meta": (_I, "轨迹元数据，" + _NONUSE),
        "executor_metadata": (_I, "执行器元数据，" + _NONUSE),
        "parent_references": (_I, "本机全空；子代理父指针取 summaries ∪ blob 字段 5"),
        "battle_mode_infos": (_I, "对战模式信息，" + _NONUSE),
    },
    "summaries.table": {
        "conversation_summaries": (_H, "会话目录：工作区/父指针/worker 名/killed/标题"),
        "conversation_parents": (_I, "2.32 新增，本机为空；父指针仍取 conversation_summaries"),
        "search_schema_version": (_I, "全文索引版本"),
        **{t: (_I, "全文索引") for t in (
            "conversations_fts", "conversations_fts_data", "conversations_fts_idx",
            "conversations_fts_content", "conversations_fts_docsize", "conversations_fts_config")},
    },
    "step_type": {
        "14": (_H, "用户输入（user_input SSOT）"),
        "15": (_H, "助手回合 / 工具调用（字段 20 可并行多个）"),
        "132": (_H, "工具结果：退出码 / LOC 裁决 / 5.4 回显补计调用"),
        "23": (_H, "上下文压缩（usage 在步骤元数据 5.28.2）"),
        "101": (_I, "后台任务（manage_task）结果系统消息，" + _NONUSE),
        "90": (_I, "浏览器快照文件引用，" + _NONUSE),
        "17": (_I, "API 报错 / 执行终止消息，" + _NONUSE + "（不计入 api_error）"),
    },
    "status": {
        "3": (_H, "完成"),
        "5": (_H, "完成但 payload 被裁剪（调用由 132 回显补回、提问取 19.3.1）"),
        "8": (_I, "生成中的瞬态（2026-11-15 实测活跃会话 step 15 先 8 后转 3）；"
                  "reader 不按非 132 步骤状态取舍，不影响计数"),
    },
    "status.132": {
        "3": (_H, "完成（按退出码判错）"),
        "5": (_H, "完成，payload 被裁剪"),
        "7": (_H, "失败 → 工具错误"),
        "4": (_H, "失败（如 Invalid argument）→ 工具错误"),
        "6": (_H, "run_command 转后台任务（task-N）：仍在运行，不计工具错误、不回放 LOC"),
        "2": (_H, "常驻进程（IsDaemon）仍在运行：不计工具错误、不回放 LOC"),
    },
    "gen.field": {
        "4": (_H, "usage 块"),
        "19": (_H, "模型名"),
        **{f: (_I, "生成请求元数据（非用量；按样本推断）") for f in (
            "1", "2", "3", "8", "9", "10", "11", "12", "14", "16", "17", "20", "21")},
    },
    "gen.usage": {
        "2": (_H, "未命中缓存输入"),
        "3": (_H, "输出（= 9 思考 + 10 回复，受守恒检查）"),
        "5": (_H, "缓存命中输入"),
        "7": (_H, "响应 id（压缩用量去重）"),
        "9": (_H, "思考 Token（output 子集）"),
        "10": (_I, "回复 Token（output 子集）"),
        "1": (_I, "模型枚举号"),
        "6": (_I, "常量（实测恒为 24）"),
        "8": (_I, "会话 ID 结构"),
        "11": (_I, "trace id"),
    },
}

# 原始工具名登记：映射表里的 + 已知原名透传的。``mcp_*`` 由 _canon_tool 规则判定。
_AGY_PASSTHROUGH = (
    "manage_task", "read_board", "post_message", "swarm_wait", "webview_browser",
    "enter_plan_mode", "exit_plan_mode", "schedule", "send_message", "manage_subagents",
    "define_subagent", "list_plugin_accounts", "search_marketplace",
)


def _agy_tool_status(name: str, mcp_servers: set[str]) -> tuple[str, str]:
    from tcer.core import antigravity_reader as ar
    if name in ar._ANTIGRAVITY_TOOL_MAP:
        return _H, f"映射为 {ar._ANTIGRAVITY_TOOL_MAP[name]}"
    if name == "call_mcp_tool":
        return _H, "MCP 网关 → mcp__<server>__<tool>"
    if name == "swarm":
        return _H, "action=add → Task；status/kill/title 原名"
    if name in _AGY_PASSTHROUGH:
        return _H, _PASS
    if name.startswith("mcp_"):
        canon = ar._canon_tool(name, "", mcp_servers)
        if canon != name:
            return _H, f"MCP → {canon.split('__')[1]}"
        return UNKNOWN, "MCP server 边界未知（原名计入）：确认 server 名后登记 _MCP_KNOWN_SERVERS"
    return UNKNOWN, "未登记工具：若是写文件类会漏计 LOC，需判断"


def _agy_log_version(root: Path) -> str:
    """``<root>/log/*.log`` 里最新的 ``Language server version: X``（只读最近几个文件头）。"""
    import re as _re
    d = Path(root) / "log"
    if not d.is_dir():
        return ""
    pat = _re.compile(r"Language server version:\s*(\S+)")
    best = ""
    for f in sorted(d.glob("*.log"))[-5:]:
        try:
            with open(f, encoding="utf-8", errors="replace") as fh:
                for _, line in zip(range(40), fh):
                    m = pat.search(line)
                    if m and _vkey(m.group(1)) > _vkey(best):
                        best = m.group(1)
        except OSError:
            continue
    return best


def antigravity_coverage(roots: Iterable[Path] | None = None, *,
                         cross_check_reader: bool = True) -> CoverageReport:
    """Census raw Antigravity SQLite/Protobuf against :data:`ANTIGRAVITY_REGISTRY`."""
    import re as _re
    from tcer.core import antigravity_reader as ar
    if roots is None:
        from tcer.core.paths import antigravity_roots
        roots = antigravity_roots()
    roots = [Path(r) for r in roots]
    P, F = ar._parse_proto_fields, ar._find_field
    uuid_re = _re.compile(r"^[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}$")

    rep = CoverageReport(source="antigravity")
    tally = _Tally()
    versions: dict[str, str] = {}
    split_bad: list[str] = []
    uuid_prompts: list[str] = []
    reader_mismatch: list[str] = []
    compaction_no_usage: list[str] = []
    gen_no_id: list[str] = []
    n_split = n_uuid = n_empty14 = n_comp_nouse = n_gen_noid = 0
    all_cids: set[str] = set()
    parents: dict[str, str] = {}
    tool_where: dict[str, str] = {}
    tool_servers: dict[str, set[str]] = {}

    for root in roots:
        name = root.name
        v = _agy_log_version(root)
        if v:
            versions[name] = max(versions.get(name, ""), v, key=_vkey)
        sp = root / "conversation_summaries.db"
        if sp.is_file():
            try:
                with ar._connect_ro(sp) as c:
                    for (t,) in c.execute("select name from sqlite_master where type='table'"):
                        tally.add("summaries.table", t, "", str(sp))
            except Exception:  # noqa: BLE001 — 运行中的 App 瞬时锁：跳过该库
                pass
            try:
                parents.update(ar._summaries_info(root).parents)
            except Exception:  # noqa: BLE001
                pass
        d = root / "conversations"
        if not d.is_dir():
            continue
        for db in sorted(d.glob("*.db")):
            all_cids.add(db.stem)
            rep.files += 1
            try:
                conn = ar._connect_ro(db)
            except Exception:  # noqa: BLE001
                rep.bad_json += 1
                continue
            try:
                for (t,) in conn.execute("select name from sqlite_master where type='table'"):
                    tally.add("db.table", t, "", str(db))
                servers: set[str] = set()
                for gidx, data in conn.execute("select idx, data from gen_metadata"):
                    if not data:
                        continue
                    servers |= ar._mcp_servers_in(data)
                    inner = F(P(data), 1)
                    if not isinstance(inner, bytes):
                        continue
                    ifs = P(inner)
                    for fn in {fn for fn, _, _ in ifs}:
                        tally.add("gen.field", str(fn), "", f"{db}:gen#{gidx}")
                    ub = F(ifs, 4)
                    if not isinstance(ub, bytes):
                        continue
                    uf = P(ub)
                    for fn in {fn for fn, _, _ in uf}:
                        tally.add("gen.usage", str(fn), "", f"{db}:gen#{gidx}")
                    g = {fn: val for fn, _, val in uf if isinstance(val, int)}
                    if 10 in g and g.get(3, 0) != g.get(9, 0) + g[10]:
                        n_split += 1
                        if len(split_bad) < _EXAMPLES_PER_ISSUE:
                            split_bad.append(f"{db}:gen#{gidx} out={g.get(3)} think={g.get(9)} reply={g[10]}")
                    if (g.get(2) or g.get(3) or g.get(5)) and not isinstance(F(uf, 7), bytes):
                        n_gen_noid += 1
                        if len(gen_no_id) < _EXAMPLES_PER_ISSUE:
                            gen_no_id.append(f"{db}:gen#{gidx}")

                calls15: list[str] = []
                echo132: set[str] = set()
                for idx, st, status, payload in conn.execute(
                        "select idx, step_type, status, step_payload from steps order by idx"):
                    rep.lines += 1
                    where = f"{db}:step#{idx}"
                    tally.add("step_type", str(st), "", where)
                    tally.add("status.132" if st == 132 else "status", str(status), "", where)
                    if not payload:
                        continue
                    if st == 15:
                        f20 = F(P(payload), 20)
                        if isinstance(f20, bytes):
                            for fn, _, f7 in P(f20):
                                if fn != 7 or not isinstance(f7, bytes):
                                    continue
                                cfs = P(f7)
                                nm = F(cfs, 2)
                                cid = F(cfs, 1)
                                calls15.append(cid.decode("utf-8", "ignore") if isinstance(cid, bytes) else "")
                                if isinstance(nm, bytes):
                                    tn = nm.decode("utf-8", "ignore")
                                    tally.add("tool", tn, "", where)
                                    tool_where.setdefault(tn, where)
                                    tool_servers.setdefault(tn, set()).update(servers)
                    elif st == 132:
                        f5 = F(P(payload), 5)
                        f4 = F(P(f5), 4) if isinstance(f5, bytes) else None
                        if isinstance(f4, bytes):
                            cfs = P(f4)
                            nm, cid = F(cfs, 2), F(cfs, 1)
                            if isinstance(nm, bytes) and nm and isinstance(cid, bytes) and cid:
                                echo132.add(cid.decode("utf-8", "ignore"))
                                tn = nm.decode("utf-8", "ignore")
                                if tn not in tool_where:
                                    tally.add("tool", tn, "", where)
                                    tool_where[tn] = where
                                tool_servers.setdefault(tn, set()).update(servers)
                    elif st == 14:
                        txt = ar._extract_user_text(payload)
                        if not txt:
                            n_empty14 += 1
                        elif uuid_re.match(txt):
                            n_uuid += 1
                            if len(uuid_prompts) < _EXAMPLES_PER_ISSUE:
                                uuid_prompts.append(f"{where} {txt}")
                    elif st == 23:
                        f5 = F(P(payload), 5)
                        if not (isinstance(f5, bytes) and isinstance(F(P(f5), 28), bytes)):
                            n_comp_nouse += 1
                            if len(compaction_no_usage) < _EXAMPLES_PER_ISSUE:
                                compaction_no_usage.append(f"{where} status={status}")

                if cross_check_reader:
                    expect = len(calls15) + len(echo132 - {c for c in calls15 if c})
                    got = sum(ar._aggregate_single(db).tool_calls.values())
                    if got != expect:
                        reader_mismatch.append(f"{db} reader={got} 独立计数={expect}")
            finally:
                conn.close()

    # ---- 登记表分类 ----
    for (dim, value), cnt in sorted(tally.counts.items(), key=lambda kv: (kv[0][0], -kv[1])):
        if dim == "tool":
            status, note = _agy_tool_status(value, tool_servers.get(value, set()))
        else:
            status, note = ANTIGRAVITY_REGISTRY.get(dim, {}).get(
                value, (UNKNOWN, "未登记：reader 未处理，需判断是否影响指标"))
        rep.entries.append(CoverageEntry(
            dim=dim, value=value, count=cnt, status=status, note=note,
            example=tally.where.get((dim, value), "") if status in (UNKNOWN, UNEXPECTED) else ""))

    orphans = sorted(c for c, p in parents.items() if c in all_cids and p not in all_cids)
    rep.max_version = " / ".join(f"{k} {v}" for k, v in sorted(versions.items())) or "-"
    rep.census_version = " / ".join(f"{k} {v}" for k, v in sorted(ANTIGRAVITY_CENSUS_VERSIONS.items()))
    newer = [f"{k} {v}（普查 {ANTIGRAVITY_CENSUS_VERSIONS.get(k, '未登记')}）"
             for k, v in sorted(versions.items())
             if _vkey(v) > _vkey(ANTIGRAVITY_CENSUS_VERSIONS.get(k, ""))]

    rep.checks += [
        CoverageCheck("output_equals_thinking_plus_reply", n_split == 0, "warn",
                      f"usage 输出 ≠ 思考 + 回复 {n_split} 次（output 口径变了）", split_bad),
        CoverageCheck("prompt_text_not_metadata", n_uuid == 0, "warn",
                      f"提问正文是纯 UUID {n_uuid} 条（提取误入元数据）", uuid_prompts),
        CoverageCheck("prompt_text_present", True, "info",
                      f"无正文的用户步骤 {n_empty14} 条（空提交 / 无参数技能调用 / 仅编辑器上下文；"
                      f"仍计入 user_msgs）"),
        CoverageCheck("compaction_has_usage", n_comp_nouse == 0, "info",
                      f"压缩步骤无用量（5.28 缺失）{n_comp_nouse} 次：该次压缩调用用量本地不可得",
                      compaction_no_usage),
        CoverageCheck("gen_usage_has_response_id", n_gen_noid == 0, "info",
                      f"有用量但无响应 id 的生成 {n_gen_noid} 条（压缩用量无法与之去重）", gen_no_id),
        CoverageCheck("subagent_parent_exists", not orphans, "info",
                      f"父会话库缺失的子代理 {len(orphans)} 个（孤儿独立成卡）",
                      orphans[:_EXAMPLES_PER_ISSUE]),
        CoverageCheck("version_within_census", not newer, "info",
                      f"各端版本 {rep.max_version}，普查版本 {rep.census_version}"
                      + (f"：{'、'.join(newer)} 更新，新增取值未登记时会在上方列为未知" if newer else "")),
    ]
    if cross_check_reader:
        rep.checks.insert(2, CoverageCheck(
            "reader_tool_calls_match", not reader_mismatch, "warn",
            f"reader 工具调用数与独立计数不符的会话库 {len(reader_mismatch)} 个",
            reader_mismatch[:_EXAMPLES_PER_ISSUE]))
    return rep


# =========================================================================== #
# Codex 覆盖审计（2026-11-19 起；rollout JSONL / .jsonl.zst）
# =========================================================================== #
# 普查已覆盖到的最高版本：登记表按此版本及以下的真实数据整理。
CODEX_CENSUS_VERSION = "0.163"

CODEX_REGISTRY: dict[str, dict[str, tuple[str, str]]] = {
    "type": {
        "session_meta": (_H, "会话头：id/cwd/cli_version/子代理标记"),
        "turn_context": (_H, "逐回合模型/窗口/权限"),
        "event_msg": (_H, "运行时事件：按 event 维度分派"),
        "response_item": (_H, "模型输出项：按 item 维度分派"),
        "token_usage_record": (_H, "逐请求 usage 快照（估速终点）"),
        "compacted": (_H, "压缩行"),
        "ghost_snapshot": (_I, "undo 快照，无用量"),
        "turn_diff": (_I, "diff 补丁存档（patch_apply_end 之外另计），无用量"),
        "list_mcp_templates": (_I, "MCP 目录列表，无用量"),
    },
    "event": {  # event_msg payload.type
        "user_message": (_H, "提问（user_input SSOT 一路；与 response_item 去重）"),
        "task_started": (_H, "回合开始（权威回合边界）"),
        "turn_started": (_H, "回合开始别名"),
        "task_complete": (_H, "回合结束：耗时/TTFT"),
        "turn_complete": (_H, "回合结束别名"),
        "turn_aborted": (_H, "中断回合：aborted/abort_reasons"),
        "context_compacted": (_H, "压缩事件计数"),
        "web_search_end": (_H, "网页搜索次数兜底"),
        "patch_apply_end": (_H, "补丁应用结果（LOC 兜底来源）"),
        "token_count": (_H, "用量差分权威（total_token_usage）"),
        "agent_message": (_I, "与 response_item.message 重复，仅作事件边界"),
        "agent_reasoning": (_I, "与 reasoning 项重复，防双计忽略"),
        "agent_reasoning_delta": (_I, "流式增量，存档去重后忽略"),
        "agent_message_delta": (_I, "流式增量，存档去重后忽略"),
        "exec_command_begin": (_I, "执行过程事件，无用量"),
        "exec_command_end": (_I, "执行过程事件，无用量"),
        "exec_command_output_delta": (_I, "流式输出增量"),
        "exec_output_delta": (_I, "流式输出增量"),
        "patch_apply_begin": (_I, "补丁开始事件"),
        "web_search_begin": (_I, "搜索开始事件"),
        "turn_aborted_notification": (_I, "中断通知"),
        "task_complete_notification": (_I, "完成通知"),
    },
    "item": {  # response_item payload.type
        "message": (_H, "用户/助手消息（按 role 维度分派）"),
        "function_call": (_H, "工具调用"),
        "function_call_output": (_H, "工具结果（exit code 权威）"),
        "custom_tool_call": (_H, "自定义工具调用（同函数名映射）"),
        "custom_tool_call_output": (_H, "自定义工具结果"),
        "local_shell_call": (_H, "旧版 shell 调用 → Bash"),
        "reasoning": (_H, "思考块计数"),
        "web_search_call": (_H, "网页搜索"),
        "item_reference": (_I, "跨项引用（item 重写器），无独立用量"),
        "ghost_snapshot": (_I, "undo 快照，无用量"),
    },
    "role": {  # response_item message.role
        "user": (_H, "提问（user_input SSOT 另一路；与 event 去重）"),
        "assistant": (_H, "回复正文"),
        "developer": (_I, "系统/权限前导，非真人输入"),
        "system": (_I, "系统前导"),
    },
    "usage": {  # token_count.{last,total}_token_usage / token_usage_record.usage
        "input_tokens": (_H, "输入（含缓存，按差分累加）"),
        "cached_input_tokens": (_H, "缓存命中（input 子集）"),
        "cache_write_input_tokens": (_H, "缓存写（input 子集，新版）"),
        "output_tokens": (_H, "输出"),
        "reasoning_output_tokens": (_H, "推理 Token（output 子集）"),
        "total_tokens": (_I, "合计字段；差分按分量，不取它"),
        "model_context_window": (_I, "窗口大小（_set_max 已消费）"),
    },
    "token_count": {  # token_count payload 顶层键
        "info": (_H, "last/total_token_usage + model_context_window"),
        "rate_limits": (_H, "配额峰值（primary/secondary）"),
        "type": (_H, "固定 'token_count'"),
    },
}

_CODEX_CALL_TYPES = ("function_call", "custom_tool_call", "local_shell_call")
_CODEX_RESULT_TYPES = ("function_call_output", "custom_tool_call_output")


def _codex_tool_status(name: str) -> tuple[str, str]:
    from tcer.core import codex_reader as cr
    if name in cr._CODEX_NAME_MAP:
        return _H, f"映射为 {cr._CODEX_NAME_MAP[name]}"
    if name in ("exec", "exec_command", "shell_command"):
        return _H, "shell 命令经 _classify_shell_command 分派"
    if name.startswith("mcp__") or name.startswith("mcp_"):
        return _I, "MCP 原名计入工具画像（非读写类）"
    return UNKNOWN, "未登记工具：若是写文件类会漏计 LOC，需判断"


def codex_coverage(files: Iterable[Path] | None = None, *,
                   cross_check_reader: bool = True) -> CoverageReport:
    """Census raw Codex rollouts against :data:`CODEX_REGISTRY` + conservation checks."""
    from tcer.core import codex_reader as cr
    if files is None:
        files = cr.discover_sessions()
    rep = CoverageReport(source="codex", census_version=CODEX_CENSUS_VERSION)
    tally = _Tally()

    orphan_results: list[str] = []
    pending_calls: list[str] = []
    total_resets: list[str] = []
    token_mismatch: list[str] = []
    reader_mismatch: list[str] = []
    no_session_meta: list[str] = []
    missing_parent: list[str] = []
    n_orphan = n_pending = n_reset = n_tok_bad = n_sub = 0
    thread_ids: set[str] = set()
    parents: dict[str, str] = {}  # thread_id -> parent_thread_id

    for path in files:
        rep.files += 1
        uses: dict[str, str] = {}
        results: set[str] = set()
        prev_total: dict[str, int] = {}
        expect = {k: 0 for k in cr._TOKEN_FIELDS}  # 独立重放的差分累加
        saw_meta = False
        try:
            fh = cr._open_text(path)
        except OSError:
            continue
        with fh:
            for ln, line in enumerate(fh, 1):
                line = line.strip()
                if not line:
                    continue
                rep.lines += 1
                try:
                    o = json.loads(line)
                except ValueError:
                    rep.bad_json += 1
                    continue
                if not isinstance(o, dict):
                    continue
                where = f"{path}:{ln}"
                ver = ""
                typ = o.get("type")
                tally.add("type", typ, "", where)
                pl = o.get("payload")
                if not isinstance(pl, dict):
                    continue
                if typ == "session_meta":
                    saw_meta = True
                    ver = pl.get("cli_version") if isinstance(pl.get("cli_version"), str) else ""
                    if ver and _vkey(ver) > _vkey(rep.max_version):
                        rep.max_version = ver
                    tid = _first = pl.get("id") or pl.get("session_id")
                    if isinstance(tid, str) and tid:
                        thread_ids.add(tid)
                    par = pl.get("parent_thread_id")
                    if isinstance(par, str) and par and isinstance(tid, str):
                        parents[tid] = par
                    elif isinstance(pl.get("source"), dict) and "subagent" in pl["source"]:
                        n_sub += 1
                if typ == "event_msg":
                    tally.add("event", pl.get("type"), ver, where)
                    if pl.get("type") == "token_count":
                        for k in pl:
                            if k != "type":
                                tally.add("token_count", k, ver, where)
                        info = pl.get("info")
                        if isinstance(info, dict):
                            for uk in info.get("last_token_usage") or {}:
                                tally.add("usage", uk, ver, where)
                            total = info.get("total_token_usage")
                            if isinstance(total, dict):
                                for uk in total:
                                    tally.add("usage", uk, ver, where)
                                # 独立重放差分算法（与 reader 同规则）
                                delta = {k: cr._as_int(total.get(k)) - prev_total.get(k, 0)
                                         for k in cr._TOKEN_FIELDS}
                                if all(v >= 0 for v in delta.values()):
                                    prev_total = {k: cr._as_int(total.get(k))
                                                  for k in cr._TOKEN_FIELDS}
                                    for k, v in delta.items():
                                        expect[k] += v
                                else:
                                    n_reset += 1
                                    if len(total_resets) < _EXAMPLES_PER_ISSUE:
                                        total_resets.append(where)
                                    prev_total = {k: cr._as_int(total.get(k))
                                                  for k in cr._TOKEN_FIELDS}
                                    last = info.get("last_token_usage")
                                    if isinstance(last, dict):
                                        for k in cr._TOKEN_FIELDS:
                                            expect[k] += cr._as_int(last.get(k))
                            else:
                                last = info.get("last_token_usage")
                                if isinstance(last, dict):
                                    for k in cr._TOKEN_FIELDS:
                                        expect[k] += cr._as_int(last.get(k))
                    continue
                if typ == "token_usage_record":
                    us = pl.get("usage")
                    if isinstance(us, dict):
                        for uk in us:
                            tally.add("usage", uk, ver, where)
                    continue
                if typ != "response_item":
                    continue
                pt = pl.get("type")
                tally.add("item", pt, ver, where)
                if pt == "message":
                    tally.add("role", pl.get("role"), ver, where)
                elif pt in _CODEX_CALL_TYPES:
                    cid = pl.get("call_id") or pl.get("id")
                    nm = pl.get("name")
                    if pt == "local_shell_call":
                        nm = "local_shell_call"
                    if isinstance(nm, str) and nm:
                        tally.add("tool", nm, ver, where)
                    if isinstance(cid, str) and cid:
                        uses[cid] = str(nm or pt)
                elif pt in _CODEX_RESULT_TYPES:
                    cid = pl.get("call_id") or pl.get("id")
                    if isinstance(cid, str) and cid:
                        results.add(cid)
        if not saw_meta:
            no_session_meta.append(str(path))
        orphans = results - set(uses)
        pend = set(uses) - results
        n_orphan += len(orphans)
        n_pending += len(pend)
        if orphans and len(orphan_results) < _EXAMPLES_PER_ISSUE:
            orphan_results.append(f"{path} ×{len(orphans)}")
        if pend and len(pending_calls) < 10:
            names = Counter(uses[t] for t in pend)
            pending_calls.append(f"{path} {dict(names)}")

        if cross_check_reader:
            u = cr._aggregate_single(path)
            got_calls = sum(u.tool_calls.values())
            if got_calls != len(uses):
                reader_mismatch.append(f"{path} reader={got_calls} 独立计数={len(uses)}")
            got_tokens = (u.input_tokens, u.cache_read_input_tokens,
                          u.cache_creation_input_tokens, u.output_tokens,
                          u.reasoning_output_tokens)
            exp_tokens = tuple(expect[k] for k in cr._TOKEN_FIELDS)
            # reader 的 input_tokens 是减去缓存后的净输入；独立重放的是毛口径。
            exp_net = (exp_tokens[0] - exp_tokens[1] - exp_tokens[2],
                       exp_tokens[1], exp_tokens[2], exp_tokens[3], exp_tokens[4])
            if got_tokens != exp_net:
                n_tok_bad += 1
                if len(token_mismatch) < _EXAMPLES_PER_ISSUE:
                    token_mismatch.append(f"{path} reader={got_tokens} 独立重放={exp_net}")

    for (dim, value), cnt in sorted(tally.counts.items(), key=lambda kv: (kv[0][0], -kv[1])):
        if dim == "tool":
            status, note = _codex_tool_status(value)
        else:
            status, note = CODEX_REGISTRY.get(dim, {}).get(
                value, (UNKNOWN, "未登记：reader 未处理，需判断是否影响指标"))
        rep.entries.append(CoverageEntry(
            dim=dim, value=value, count=cnt, status=status, note=note,
            example=tally.where.get((dim, value), "") if status in (UNKNOWN, UNEXPECTED) else ""))

    missing_parent = sorted(
        f"{tid} → 父 {p}" for tid, p in parents.items() if p not in thread_ids)
    rep.checks += [
        CoverageCheck("tool_results_have_calls", n_orphan == 0, "warn",
                      f"找不到对应调用的工具结果 {n_orphan} 个", orphan_results),
        CoverageCheck("calls_have_results", n_pending == 0, "info",
                      f"没有结果的工具调用 {n_pending} 个（进行中/中断属正常）",
                      pending_calls),
        CoverageCheck("total_token_usage_monotonic", n_reset == 0, "info",
                      f"total_token_usage 回退/重置 {n_reset} 次（差分口径回退 last）",
                      total_resets),
        CoverageCheck("reader_tokens_match", n_tok_bad == 0, "warn",
                      f"reader Token 累计与独立差分重放不符的文件 {n_tok_bad} 个",
                      token_mismatch),
        CoverageCheck("session_meta_present", not no_session_meta, "info",
                      f"无 session_meta 头的文件 {len(no_session_meta)} 个"
                      f"（cwd/版本/子代理标记不可得）", no_session_meta[:_EXAMPLES_PER_ISSUE]),
        CoverageCheck("subagent_parent_exists", not missing_parent, "info",
                      f"父线程 rollout 缺失的子代理 {len(missing_parent)} 个（孤儿独立计）",
                      missing_parent[:_EXAMPLES_PER_ISSUE]),
        CoverageCheck("version_within_census", not rep.newer_than_census, "info",
                      f"数据最高版本 {rep.max_version or '-'}，登记表普查版本 {rep.census_version}"
                      + ("：有更新版本的数据" if rep.newer_than_census else "")),
    ]
    if cross_check_reader:
        rep.checks.insert(2, CoverageCheck(
            "reader_tool_calls_match", not reader_mismatch, "warn",
            f"reader 工具调用数与独立计数不符的文件 {len(reader_mismatch)} 个",
            reader_mismatch[:_EXAMPLES_PER_ISSUE]))
    return rep


# =========================================================================== #
# omp / Pi 覆盖审计（2026-11-19 起；同族 JSONL，登记表共用、Pi 字段为超集）
# =========================================================================== #
OMP_CENSUS_VERSION = "18.10.4"
PI_CENSUS_VERSION = ""  # Pi 无已知版本锚点（上游仓库未发版号到本地数据）

OMP_PI_REGISTRY: dict[str, dict[str, tuple[str, str]]] = {
    "type": {
        "session": (_H, "会话头：id/cwd/title"),
        "title": (_H, "定宽标题槽"),
        "title_change": (_H, "标题变更（read_session_meta 消费）"),
        "model_change": (_H, "活跃模型（model / provider+modelId 两形）"),
        "thinking_level_change": (_H, "reasoning_effort"),
        "message": (_H, "回合流：按 role 维度分派"),
        "compaction": (_H, "压缩事件：tokensBefore/After → 次数/代价"),
        "mode_change": (_H, "计划模式生命周期（mode=plan 计入）"),
        "model_usage": (_I, "辅助模型用量（如 advisor）：刻意不并入——并入破 "
                           "turn_stats_match_total，需先加辅助用量字段"),
        "usage": (_I, "Pi 顶层辅助用量行：同上刻意不并入"),
        "branch_summary": (_I, "分支摘要；其 usage 属辅助用量，不并入"),
        "custom": (_I, "自定义事件，无用量"),
        "custom_message": (_I, "自定义消息，无用量"),
    },
    "role": {
        "user": (_H, "提问（agent 注入有 attribution/synthetic 标记，剔除）"),
        "assistant": (_H, "回复：usage/模型/工具/思考/stopReason"),
        "toolResult": (_H, "工具结果：isError/LOC 裁决/details"),
    },
    "block.assistant": {
        "thinking": (_H, "思考块计数"),
        "text": (_I, "回复正文，不计数"),
        "toolCall": (_H, "工具调用/LOC 回放"),
    },
    "block.user": {
        "text": (_H, "提问正文"),
        "image": (_H, "内联 base64 图片输入"),
        "image_url": (_H, "内联图片输入（image_count 同口径）"),
        "input_image": (_H, "内联图片输入（image_count 同口径）"),
    },
    "usage": {  # assistant message.usage
        "input": (_H, "未命中缓存输入"),
        "output": (_H, "输出（Pi 含 reasoning 子集）"),
        "cacheRead": (_H, "缓存命中输入"),
        "cacheWrite": (_H, "缓存写输入"),
        "cacheWrite1h": (_H, "Pi 1h 缓存写子集（溢价）"),
        "totalTokens": (_H, "守恒锚点：input+output+cacheRead+cacheWrite"),
        "cost": (_H, "自报成本（cost.total 求和 → reported_cost_usd）"),
        "reasoning": (_H, "Pi 推理 Token（output 子集）"),
        "reasoningTokens": (_H, "omp 18.x 推理 Token（output 子集）"),
    },
    "usage.cost": {
        "total": (_H, "会话自报成本"),
        "input": (_I, "明细（只取 total）"),
        "output": (_I, "明细"),
        "cacheRead": (_I, "明细"),
        "cacheWrite": (_I, "明细"),
        "cacheWrite1h": (_I, "明细"),
    },
    "stop_reason": {
        "aborted": (_H, "中断 → aborted/abort_reasons"),
        "error": (_H, "错误（仅 429 计 rate_limit）"),
        "stop": (_I, "正常结束"),
        "toolUse": (_I, "正常工具回合结束"),
        "length": (_I, "输出截断"),
        "end_turn": (_I, "正常结束"),
        "max_tokens": (_I, "输出截断"),
        "content_filter": (_I, "内容拦截"),
        "pause": (_I, "暂停"),
    },
    "snapshot": {  # contextSnapshot 键（omp fork 字段；Pi 无）
        "promptTokens": (_H, "整轮输入（peak_input）"),
    },
    "mode": {  # mode_change data.mode
        "plan": (_H, "计划模式计入"),
        "plan_paused": (_I, "退出不计"),
        "none": (_I, "退出不计"),
    },
}


def _omp_tool_status(name: str) -> tuple[str, str]:
    from tcer.core import omp_reader as ompr
    if name in ompr._OMP_TOOL_MAP:
        return _H, f"映射为 {ompr._OMP_TOOL_MAP[name]}"
    if name.startswith("mcp"):
        return _I, "MCP 原名计入工具画像（非读写类）"
    return UNKNOWN, "未登记工具：若是写文件类会漏计 LOC，需判断"


def omp_pi_coverage(files: Iterable[Path], *, source: str,
                    census_version: str,
                    cross_check_reader: bool = True) -> CoverageReport:
    """Census omp/Pi JSONL against :data:`OMP_PI_REGISTRY` + conservation checks."""
    from tcer.core import omp_reader as ompr
    rep = CoverageReport(source=source, census_version=census_version)
    tally = _Tally()

    orphan_results: list[str] = []
    pending_calls: list[str] = []
    total_bad: list[str] = []
    reasoning_over: list[str] = []
    no_session: list[str] = []
    zero_usage: list[str] = []
    reader_mismatch: list[str] = []
    n_orphan = n_pending = n_total_bad = n_reason_over = n_zero = 0
    stems: set[str] = set()
    sub_files: list[Path] = []

    files = list(files)
    for p in files:
        stems.add(p.stem)
    for p in files:
        if ompr._is_subagent_file(p):
            sub_files.append(p)

    for path in files:
        rep.files += 1
        uses: dict[str, str] = {}
        results: set[str] = set()
        saw_session = False
        try:
            fh = open(path, encoding="utf-8", errors="replace")
        except OSError:
            continue
        with fh:
            for ln, line in enumerate(fh, 1):
                line = line.strip()
                if not line:
                    continue
                rep.lines += 1
                try:
                    o = json.loads(line)
                except ValueError:
                    rep.bad_json += 1
                    continue
                if not isinstance(o, dict):
                    continue
                where = f"{path}:{ln}"
                typ = o.get("type")
                tally.add("type", typ, "", where)
                if typ == "session":
                    saw_session = True
                    continue
                if typ == "mode_change":
                    data = o.get("data")
                    tally.add("mode", data.get("mode") if isinstance(data, dict) else None,
                              "", where)
                    continue
                if typ != "message":
                    continue
                mm = o.get("message")
                if not isinstance(mm, dict):
                    continue
                role = mm.get("role")
                tally.add("role", role, "", where)
                if role in ("assistant", "user"):
                    content = mm.get("content")
                    if isinstance(content, list):
                        for b in content:
                            if not isinstance(b, dict):
                                continue
                            tally.add(f"block.{role}", b.get("type"), "", where)
                            if role == "assistant" and b.get("type") == "toolCall":
                                nm = b.get("name")
                                cid = b.get("id")
                                if isinstance(nm, str) and nm:
                                    tally.add("tool", nm, "", where)
                                if isinstance(cid, str) and cid:
                                    uses[cid] = str(nm or "")
                sr = mm.get("stopReason")
                if sr is not None:
                    tally.add("stop_reason", sr, "", where)
                snap = mm.get("contextSnapshot")
                if isinstance(snap, dict):
                    for k in snap:
                        tally.add("snapshot", k, "", where)
                if role == "toolResult":
                    cid = mm.get("toolCallId")
                    if isinstance(cid, str) and cid:
                        results.add(cid)
                if role != "assistant":
                    continue
                usage = mm.get("usage")
                if not isinstance(usage, dict):
                    continue
                vals: dict[str, int] = {}
                for k, v in usage.items():
                    tally.add("usage", k, "", where)
                    if k == "cost" and isinstance(v, dict):
                        for ck in v:
                            tally.add("usage.cost", ck, "", where)
                    elif isinstance(v, (int, float)) and not isinstance(v, bool):
                        vals[k] = int(v)
                parts = (vals.get("input", 0) + vals.get("output", 0)
                         + vals.get("cacheRead", 0) + vals.get("cacheWrite", 0))
                if any(vals.get(k) for k in ("input", "output", "cacheRead", "cacheWrite",
                                             "cacheWrite1h", "reasoning", "reasoningTokens")):
                    tt = vals.get("totalTokens", 0)
                    if tt > 0 and parts != tt:
                        n_total_bad += 1
                        if len(total_bad) < _EXAMPLES_PER_ISSUE:
                            total_bad.append(
                                f"{where} input+output+cacheR+cacheW={parts} total={tt}")
                    reason = vals.get("reasoning", vals.get("reasoningTokens", 0))
                    if reason > vals.get("output", 0):
                        n_reason_over += 1
                        if len(reasoning_over) < _EXAMPLES_PER_ISSUE:
                            reasoning_over.append(
                                f"{where} reasoning={reason} > output={vals.get('output', 0)}")
                else:
                    n_zero += 1
                    if len(zero_usage) < _EXAMPLES_PER_ISSUE:
                        zero_usage.append(where)
        if not saw_session:
            no_session.append(str(path))
        orphans = results - set(uses)
        pend = set(uses) - results
        n_orphan += len(orphans)
        n_pending += len(pend)
        if orphans and len(orphan_results) < _EXAMPLES_PER_ISSUE:
            orphan_results.append(f"{path} ×{len(orphans)}")
        if pend and len(pending_calls) < 10:
            names = Counter(uses[t] for t in pend)
            pending_calls.append(f"{path} {dict(names)}")

        if cross_check_reader:
            u = ompr._usage_from_events(ompr.iter_events(path),
                                        is_subagent=ompr._is_subagent_file(path))
            got = sum(u.tool_calls.values())
            if got != len(uses):
                reader_mismatch.append(f"{path} reader={got} 独立计数={len(uses)}")

    missing_parent = sorted(
        str(p) for p in sub_files if p.parent.with_suffix(".jsonl") not in files
        and p.parent.with_suffix(".jsonl").stem not in stems)
    for (dim, value), cnt in sorted(tally.counts.items(), key=lambda kv: (kv[0][0], -kv[1])):
        if dim == "tool":
            status, note = _omp_tool_status(value)
        else:
            status, note = OMP_PI_REGISTRY.get(dim, {}).get(
                value, (UNKNOWN, "未登记：reader 未处理，需判断是否影响指标"))
        rep.entries.append(CoverageEntry(
            dim=dim, value=value, count=cnt, status=status, note=note,
            example=tally.where.get((dim, value), "") if status in (UNKNOWN, UNEXPECTED) else ""))

    rep.checks += [
        CoverageCheck("tool_results_have_calls", n_orphan == 0, "warn",
                      f"找不到对应 toolCall 的 toolResult {n_orphan} 个", orphan_results),
        CoverageCheck("calls_have_results", n_pending == 0, "info",
                      f"没有 toolResult 的调用 {n_pending} 个（进行中/中断属正常）",
                      pending_calls),
        CoverageCheck("usage_total_consistency", n_total_bad == 0, "warn",
                      f"input+output+cacheRead+cacheWrite ≠ totalTokens 的响应 "
                      f"{n_total_bad} 条（上游守恒约定变了）", total_bad),
        CoverageCheck("reasoning_within_output", n_reason_over == 0, "info",
                      f"reasoning > output 的响应 {n_reason_over} 条"
                      f"（已知代理路由口径异常；计价维持 output 原值）", reasoning_over),
        CoverageCheck("session_header_present", not no_session, "warn",
                      f"无 session 头的文件 {len(no_session)} 个（cwd/标题不可得）",
                      no_session[:_EXAMPLES_PER_ISSUE]),
        CoverageCheck("zero_usage_responses", True, "info",
                      f"零用量 assistant 响应 {n_zero} 条（empty_usage_skipped 口径）",
                      zero_usage[:_EXAMPLES_PER_ISSUE] if n_zero else []),
        CoverageCheck("subagent_parent_exists", not missing_parent, "warn",
                      f"找不到主文件的子代理 {len(missing_parent)} 个",
                      missing_parent[:_EXAMPLES_PER_ISSUE]),
        CoverageCheck("version_within_census", True, "info",
                      f"普查版本 {census_version or '未登记'}（本地数据无版本字段）"),
    ]
    if cross_check_reader:
        rep.checks.insert(2, CoverageCheck(
            "reader_tool_calls_match", not reader_mismatch, "warn",
            f"reader 工具调用数与独立计数不符的文件 {len(reader_mismatch)} 个",
            reader_mismatch[:_EXAMPLES_PER_ISSUE]))
    return rep


def omp_coverage(files: Iterable[Path] | None = None, *,
                 cross_check_reader: bool = True) -> CoverageReport:
    """Census ``~/.omp`` sessions."""
    if files is None:
        from tcer.core import omp_reader as ompr
        files = ompr.discover_sessions()
    return omp_pi_coverage(files, source="omp", census_version=OMP_CENSUS_VERSION,
                           cross_check_reader=cross_check_reader)


def pi_coverage(files: Iterable[Path] | None = None, *,
                cross_check_reader: bool = True) -> CoverageReport:
    """Census ``~/.pi`` sessions."""
    if files is None:
        from tcer.core import pi_reader as pr
        files = pr.discover_sessions()
    return omp_pi_coverage(files, source="pi", census_version=PI_CENSUS_VERSION,
                           cross_check_reader=cross_check_reader)


# =========================================================================== #
# OpenCode 覆盖审计（2026-11-19 起；仅 SQLite opencode.db）
# =========================================================================== #
OPENCODE_CENSUS_VERSION = "1.19.4"

OPENCODE_REGISTRY: dict[str, dict[str, tuple[str, str]]] = {
    "db.table": {
        "session": (_H, "会话行：cwd/项目/parent_id/逐 token 列/cost"),
        "message": (_H, "消息行：role/模型/tokens/time"),
        "part": (_H, "部件行：按 part 维度分派"),
        "project": (_H, "项目行：worktree/name"),
        "session_v2": (_I, "上游 dev 分支 v2 存储，未发布未实现"),
        "session_message": (_I, "v2 存储，未发布"),
    },
    "role": {
        "user": (_H, "提问（合成/注入行有 synthetic/ignored 标记剔除）"),
        "assistant": (_H, "回复：模型/tokens/time（est 速度、peak 补齐）"),
    },
    "part": {
        "text": (_H, "正文（user 的是提问 SSOT）"),
        "reasoning": (_H, "思考块 + time.start/end → 思考耗时"),
        "file": (_H, "附件（图片计 image_count）"),
        "tool": (_H, "工具调用（completed 才回放 LOC）"),
        "tool-invocation": (_H, "工具调用旧形"),
        "tool-call": (_H, "工具调用旧形"),
        "tool-result": (_H, "工具结果（错误裁决）"),
        "tool-output": (_H, "工具结果旧形"),
        "step-finish": (_H, "步末 Token 快照（peak/逐回合时间线）"),
        "step_finish": (_H, "步末快照旧形"),
        "compaction": (_H, "压缩计数"),
    },
    "tokens": {  # message data.tokens
        "input": (_H, "未命中缓存输入"),
        "output": (_H, "输出"),
        "reasoning": (_H, "推理 Token（折入 output）"),
        "cache": (_H, "缓存子对象（read/write 维度）"),
        "total": (_I, "合计字段，按分量计"),
    },
    "tokens.cache": {
        "read": (_H, "缓存命中"),
        "write": (_H, "缓存写"),
    },
}

_OC_FTS = ("conversations_fts", "session_fts", "message_fts", "part_fts")


def _oc_tool_status(name: str) -> tuple[str, str]:
    from tcer.core import opencode_reader as ocr
    canon = {"read", "edit", "write", "grep", "glob", "bash", "task",
             "websearch", "webfetch", "todowrite"}
    lower = name.lower().strip()
    if lower in ocr._OPENCODE_TOOL_MAP:
        return _H, f"映射为 {ocr._OPENCODE_TOOL_MAP[lower]}"
    if lower in canon:
        return _H, "原名即规范名"
    if name.startswith("mcp"):
        return _I, "MCP 原名计入工具画像（非读写类）"
    return UNKNOWN, "未登记工具：若是写文件类会漏计 LOC，需判断"


def opencode_coverage(db_paths: Iterable[Path] | None = None, *,
                      cross_check_reader: bool = True) -> CoverageReport:
    """Census OpenCode ``opencode.db`` against :data:`OPENCODE_REGISTRY`."""
    from tcer.core import opencode_reader as ocr
    if db_paths is None:
        db_paths = ocr.discover_databases()
    rep = CoverageReport(source="opencode", census_version=OPENCODE_CENSUS_VERSION)
    tally = _Tally()

    orphan_results: list[str] = []
    pending_calls: list[str] = []
    token_mismatch: list[str] = []
    reader_mismatch: list[str] = []
    n_orphan = n_pending = n_tok_bad = 0

    for db in db_paths:
        db = Path(db)
        rep.files += 1
        try:
            conn = ocr._connect(db)
        except Exception:  # noqa: BLE001
            rep.bad_json += 1
            continue
        try:
            tables = {r[0] for r in conn.execute(
                "select name from sqlite_master where type='table'")}
            for t in sorted(tables):
                if t in OPENCODE_REGISTRY["db.table"]:
                    tally.add("db.table", t, "", str(db))
                elif any(t.startswith(p) for p in _OC_FTS) or "_fts" in t:
                    continue  # 全文索引族，随消息表漂移，不逐表登记
                else:
                    tally.add("db.table", t, "", str(db))
            if "session" not in tables or "message" not in tables:
                continue

            # ---- 逐会话普查 ----
            sess_rows = conn.execute("select * from session").fetchall()
            msg_rows = (conn.execute("select * from message").fetchall()
                        if "message" in tables else [])
            part_rows = (conn.execute("select * from part").fetchall()
                         if "part" in tables else [])
            msgs_by_session: dict[str, list] = {}
            for m in msg_rows:
                rep.lines += 1
                msgs_by_session.setdefault(str(m["session_id"]), []).append(m)
            parts_by_session: dict[str, list] = {}
            for p in part_rows:
                rep.lines += 1
                parts_by_session.setdefault(str(p["session_id"]), []).append(p)

            for sess in sess_rows:
                sid = str(sess["id"])
                for m in msgs_by_session.get(sid, []):
                    data = ocr._json_obj(m["data"])
                    role = ocr._first_str(
                        data.get("role"),
                        m["data"] if m["data"] in ("user", "assistant") else None)
                    tally.add("role", role, "", f"{db}:msg#{m['id']}")
                    tok = data.get("tokens")
                    if isinstance(tok, dict):
                        for k, v in tok.items():
                            tally.add("tokens", k, "", f"{db}:msg#{m['id']}")
                            if k == "cache" and isinstance(v, dict):
                                for ck in v:
                                    tally.add("tokens.cache", ck, "", f"{db}:msg#{m['id']}")

                uses: dict[str, str] = {}
                results: set[str] = set()
                for p in parts_by_session.get(sid, []):
                    data = ocr._json_obj(p["data"])
                    pt = ocr._first_str(data.get("type"), data.get("kind"))
                    tally.add("part", pt, "", f"{db}:part#{p['id']}")
                    if pt in ("tool", "tool-invocation", "tool-call"):
                        nm = ocr._first_str(data.get("tool"), data.get("toolName"),
                                            data.get("name"), data.get("title"))
                        if isinstance(nm, str) and nm:
                            tally.add("tool", nm, "", f"{db}:part#{p['id']}")
                        cid = ocr._first_str(data.get("callID"), data.get("call_id"),
                                             data.get("toolCallId"), data.get("id"))
                        if cid:
                            uses[cid] = str(nm or "")
                    elif pt in ("tool-result", "tool-output"):
                        cid = ocr._first_str(data.get("callID"), data.get("call_id"),
                                             data.get("toolCallId"))
                        if cid:
                            results.add(cid)

                orphans = results - set(uses)
                pend = set(uses) - results
                n_orphan += len(orphans)
                n_pending += len(pend)
                if orphans and len(orphan_results) < _EXAMPLES_PER_ISSUE:
                    orphan_results.append(f"{db}:{sid} ×{len(orphans)}")
                if pend and len(pending_calls) < 10:
                    names = Counter(uses[t] for t in pend)
                    pending_calls.append(f"{db}:{sid} {dict(names)}")

                # 守恒：session 列 == Σ assistant message tokens
                s_cols = set(sess.keys())
                if "tokens_input" in s_cols:
                    sums = [0, 0, 0, 0, 0]
                    saw_msg_tokens = False
                    for m in msgs_by_session.get(sid, []):
                        data = ocr._json_obj(m["data"])
                        if ocr._first_str(data.get("role")) != "assistant":
                            continue
                        tok = data.get("tokens")
                        if not isinstance(tok, dict):
                            continue
                        saw_msg_tokens = True
                        cache = tok.get("cache") if isinstance(tok.get("cache"), dict) else {}
                        sums[0] += ocr._as_int(tok.get("input"))
                        sums[1] += ocr._as_int(tok.get("output"))
                        sums[2] += ocr._as_int(tok.get("reasoning"))
                        sums[3] += ocr._as_int(cache.get("read"))
                        sums[4] += ocr._as_int(cache.get("write"))
                    cols = (ocr._as_int(sess["tokens_input"]), ocr._as_int(sess["tokens_output"]),
                            ocr._as_int(sess["tokens_reasoning"]),
                            ocr._as_int(sess["tokens_cache_read"]),
                            ocr._as_int(sess["tokens_cache_write"]))
                    # 消息根本不写 tokens（旧格式）：总量只能取会话列，不算缺口；
                    # 消息写了却对不上 → 分桶有静默差额，点名。
                    if saw_msg_tokens and tuple(sums) != cols:
                        n_tok_bad += 1
                        if len(token_mismatch) < _EXAMPLES_PER_ISSUE:
                            token_mismatch.append(f"{db}:{sid} 消息和={sums} 会话列={cols}")

                if cross_check_reader:
                    try:
                        u = ocr._aggregate_single(db, sid)
                    except Exception:  # noqa: BLE001
                        continue
                    got = sum(u.tool_calls.values())
                    if got != len(uses):
                        reader_mismatch.append(f"{db}:{sid} reader={got} 独立计数={len(uses)}")
        finally:
            conn.close()

    for (dim, value), cnt in sorted(tally.counts.items(), key=lambda kv: (kv[0][0], -kv[1])):
        if dim == "tool":
            status, note = _oc_tool_status(value)
        else:
            status, note = OPENCODE_REGISTRY.get(dim, {}).get(
                value, (UNKNOWN, "未登记：reader 未处理，需判断是否影响指标"))
        rep.entries.append(CoverageEntry(
            dim=dim, value=value, count=cnt, status=status, note=note,
            example=tally.where.get((dim, value), "") if status in (UNKNOWN, UNEXPECTED) else ""))

    rep.checks += [
        CoverageCheck("tool_results_have_calls", n_orphan == 0, "warn",
                      f"找不到对应调用的工具结果 part {n_orphan} 个", orphan_results),
        CoverageCheck("calls_have_results", n_pending == 0, "info",
                      f"没有结果 part 的工具调用 {n_pending} 个（进行中属正常）",
                      pending_calls),
        CoverageCheck("session_tokens_equal_message_sum", n_tok_bad == 0, "warn",
                      f"会话 token 列 ≠ Σ assistant 消息 tokens 的会话 {n_tok_bad} 个"
                      f"（reader 用会话列取总、用消息分桶模型——不符即有静默差额）",
                      token_mismatch),
        CoverageCheck("version_within_census", True, "info",
                      f"普查版本 {rep.census_version}（本地 db 无版本列）"),
    ]
    if cross_check_reader:
        rep.checks.insert(2, CoverageCheck(
            "reader_tool_calls_match", not reader_mismatch, "warn",
            f"reader 工具调用数与独立计数不符的会话 {len(reader_mismatch)} 个",
            reader_mismatch[:_EXAMPLES_PER_ISSUE]))
    return rep
