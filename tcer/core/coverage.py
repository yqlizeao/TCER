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
