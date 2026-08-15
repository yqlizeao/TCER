"""自包含单文件 HTML 效率报告（项目级 + 会话级）。

渲染完全复用 ``metric_defs`` SSOT（``GROUPS`` / ``MODEL_GROUPS`` / ``display`` /
``model_display``），保证 HTML 数值与 GUI 网格逐字节一致；颜色复用 ``theme``
常量。内联 CSS + 少量原生 JS（表格排序），零依赖、单文件、可直接分享。
本模块与 metric_defs / theme 一样不依赖 Tkinter，可无头测试。
"""
from __future__ import annotations

import html
from datetime import datetime

from tcer.core import format as fmt
from tcer.core import metrics
from tcer.core.export import score_ranking
from tcer.core.models import SessionReport
from tcer.gui import metric_defs, theme


def _esc(s: object) -> str:
    return html.escape(str(s), quote=True)


def _session_label(r: SessionReport) -> str:
    return r.meta.session_id or r.meta.path.stem


def _session_title(r: SessionReport) -> str:
    return r.meta.title or _session_label(r)


# --------------------------------------------------------------------------- #
# CSS / JS
# --------------------------------------------------------------------------- #
def _css() -> str:
    return f"""
:root {{ color-scheme: dark; }}
* {{ box-sizing: border-box; }}
body {{ margin: 0; padding: 24px; background: {theme.BG}; color: {theme.FG};
       font: 14px/1.6 "Microsoft YaHei", "PingFang SC", "Noto Sans CJK SC", sans-serif; }}
main {{ max-width: 1150px; margin: 0 auto; }}
h1 {{ font-size: 22px; margin: 0 0 4px; }}
h2 {{ font-size: 16px; margin: 28px 0 10px; border-left: 3px solid {theme.ACCENT}; padding-left: 8px; }}
.meta {{ color: {theme.MUTED}; font-size: 12px; margin-bottom: 18px; }}
.meta b {{ color: {theme.FG}; font-weight: 600; }}
.kpis {{ display: grid; grid-template-columns: repeat(auto-fit, minmax(150px, 1fr)); gap: 10px; }}
.kpi {{ background: {theme.PANEL_2}; border: 1px solid #3a3a3e; border-radius: 8px; padding: 10px 14px; }}
.kpi .v {{ font: 700 20px/1.3 Consolas, Menlo, monospace; }}
.kpi .l {{ color: {theme.MUTED}; font-size: 12px; }}
.groups {{ display: grid; grid-template-columns: repeat(auto-fit, minmax(460px, 1fr)); gap: 12px; }}
.card {{ background: {theme.PANEL}; border: 1px solid #333; border-radius: 8px; overflow: hidden; }}
.card h3 {{ margin: 0; padding: 6px 12px; font-size: 13px; color: #fff; }}
table {{ border-collapse: collapse; width: 100%; font-size: 13px; }}
td, th {{ padding: 4px 12px; border-top: 1px solid #303034; text-align: left; }}
td.v, th.num {{ text-align: right; font-family: Consolas, Menlo, monospace; white-space: nowrap; }}
td.u {{ color: {theme.MUTED}; font-size: 12px; width: 70px; }}
tr.sub td {{ background: #2b2b2f; color: {theme.MUTED}; font-size: 12px; padding: 2px 12px; }}
.warn {{ background: #3a2a1e; border: 1px solid {theme.WARNING}; border-radius: 8px;
        padding: 10px 14px; margin: 14px 0; font-size: 13px; }}
.note {{ color: {theme.MUTED}; font-size: 12px; margin: 6px 0; }}
.barrow {{ display: flex; align-items: center; gap: 8px; margin: 3px 0; font-size: 12px; }}
.barrow .lbl {{ width: 150px; text-align: right; color: {theme.MUTED};
               font-family: Consolas, Menlo, monospace; overflow: hidden; text-overflow: ellipsis; }}
.barrow .track {{ flex: 1; background: #2b2b2f; border-radius: 3px; height: 14px; }}
.barrow .fill {{ height: 100%; border-radius: 3px; min-width: 2px; }}
.barrow .val {{ width: 130px; font-family: Consolas, Menlo, monospace; }}
.scroll {{ overflow-x: auto; }}
table.sortable th {{ cursor: pointer; user-select: none; background: #2f2f33; position: sticky; top: 0; }}
table.sortable th:hover {{ background: #3a3a3f; }}
table.sortable td {{ white-space: nowrap; }}
.grade {{ padding: 1px 8px; border-radius: 8px; font-size: 12px; color: #fff; }}
details {{ background: {theme.PANEL}; border: 1px solid #333; border-radius: 8px;
          margin: 8px 0; padding: 0; }}
details summary {{ cursor: pointer; padding: 8px 12px; font-size: 13px; }}
details summary:hover {{ background: #2d2d31; }}
details .groups {{ padding: 10px; }}
footer {{ margin-top: 28px; color: {theme.MUTED}; font-size: 12px;
         border-top: 1px solid #333; padding-top: 10px; }}
.best {{ color: {theme.VALUE_BEST}; font-weight: 700; }}
"""


_SORT_JS = """
document.querySelectorAll("table.sortable").forEach(function (tbl) {
  tbl.querySelectorAll("th").forEach(function (th, idx) {
    th.addEventListener("click", function () {
      var tbody = tbl.tBodies[0];
      var rows = Array.from(tbody.rows);
      var asc = th.dataset.asc !== "1";
      tbl.querySelectorAll("th").forEach(function (h) { delete h.dataset.asc; });
      th.dataset.asc = asc ? "1" : "0";
      rows.sort(function (a, b) {
        var ca = a.cells[idx], cb = b.cells[idx];
        var va = ca.dataset.v, vb = cb.dataset.v;
        var r;
        if (va !== undefined && vb !== undefined) r = parseFloat(va) - parseFloat(vb);
        else if (va !== undefined) r = 1;
        else if (vb !== undefined) r = -1;
        else r = ca.textContent.localeCompare(cb.textContent, "zh");
        return asc ? r : -r;
      });
      rows.forEach(function (r) { tbody.appendChild(r); });
    });
  });
});
"""


# --------------------------------------------------------------------------- #
# Sections
# --------------------------------------------------------------------------- #
def _kpi(label: str, value: str, color: str = "") -> str:
    style = f' style="color:{color}"' if color else ""
    return (f'<div class="kpi"><div class="v"{style}>{_esc(value)}</div>'
            f'<div class="l">{_esc(label)}</div></div>')


def _kpi_section(rep: SessionReport, n_sessions: int | None) -> str:
    d = lambda k: metric_defs.display(rep, k)
    cards = []
    if n_sessions is not None:
        cards.append(_kpi("会话数", str(n_sessions)))
    cards += [
        _kpi("总 Token", d("total_tokens")),
        _kpi("总成本", d("cost")),
        _kpi("净增行", d("net_loc")),
        _kpi("TCER（行/百万）", d("tcer"), theme.SUCCESS),
        _kpi("缓存命中率", d("chr")),
    ]
    if rep.score is not None:
        color = theme.GRADE_HEX.get(rep.tier or "", "")
        cards.append(_kpi("综合效率分", d("score"), color))
        cards.append(_kpi("评级", d("tier"), color))
    else:
        cards.append(_kpi("千行代码成本", d("cpe")))
    return f'<div class="kpis">{"".join(cards)}</div>'


def _metric_row(rep: SessionReport, m: metric_defs.Metric) -> str:
    val = metric_defs.display(rep, m.key)
    color = theme.LEVEL_COLORS.get(m.level, theme.FG)
    tip = f' title="{_esc(m.tip)}"' if m.tip else ""
    return (f'<tr><td{tip}>{_esc(m.name)}</td>'
            f'<td class="v" style="color:{color}">{_esc(val)}</td>'
            f'<td class="u">{_esc(m.unit)}</td></tr>')


def _group_card(rep: SessionReport, g: metric_defs.Group) -> str:
    head_bg = theme.GROUP_COLORS.get(g.id, theme.GROUP_COLORS["G_NEUTRAL"])
    rows: list[str] = []
    if g.subgroups:
        for sg in g.subgroups:
            rows.append(f'<tr class="sub"><td colspan="3">{_esc(sg.name)}</td></tr>')
            rows += [_metric_row(rep, m) for m in sg.metrics]
    else:
        rows = [_metric_row(rep, m) for m in g.metrics]
    return (f'<div class="card"><h3 style="background:{head_bg}">'
            f'{_esc(g.name)}</h3><table>{"".join(rows)}</table></div>')


def _groups_section(rep: SessionReport) -> str:
    return f'<div class="groups">{"".join(_group_card(rep, g) for g in metric_defs.GROUPS)}</div>'


def _score_section(reports: list[SessionReport]) -> str:
    ranking = score_ranking(reports)
    if not ranking:
        return ('<p class="note">无可评分会话（会话未产出可计量净代码，或已禁用 LOC 统计）。</p>')
    # 综合效率分天然有界 0–100，条形直接按满刻度，无需 P90 截尾。
    rows = []
    for label, score, tier in ranking:
        w = max(1.0, min(100.0, score))
        color = theme.GRADE_HEX.get(tier, theme.ACCENT)
        rows.append(
            f'<div class="barrow"><span class="lbl">{_esc(label)}</span>'
            f'<span class="track"><span class="fill" style="width:{w:.1f}%;'
            f'background:{color}"></span></span>'
            f'<span class="val">{score:.1f}　{_esc(tier)}</span></div>')
    return "".join(rows)


def _grade_pill(tier: str | None) -> str:
    if not tier:
        return "-"
    color = theme.GRADE_HEX.get(tier, "#555")
    return f'<span class="grade" style="background:{color}">{_esc(tier)}</span>'


def _num_td(raw: float | None, text: str) -> str:
    dv = f' data-v="{raw}"' if raw is not None else ""
    return f'<td class="v"{dv}>{_esc(text)}</td>'


def _sessions_table(reports: list[SessionReport]) -> str:
    head = ("<tr><th>会话</th><th>开始时间</th><th class='num'>回合</th>"
            "<th class='num'>总 Token</th><th class='num'>成本</th>"
            "<th class='num'>净增行</th><th class='num'>TCER</th>"
            "<th class='num'>综合效率分</th><th>评级</th></tr>")
    rows = []
    for r in reports:
        u = r.usage
        title = _session_title(r)
        started = fmt.fmt_dt(u.started_at)
        rows.append(
            "<tr>"
            f'<td title="{_esc(_session_label(r))}">{_esc(title[:48])}</td>'
            f'<td data-v="{u.started_at or 0}">{_esc(started)}</td>'
            + _num_td(u.assistant_msgs, metric_defs.display(r, "turns"))
            + _num_td(u.total, metric_defs.display(r, "total_tokens"))
            + _num_td(r.cost, metric_defs.display(r, "cost"))
            + _num_td(r.net_loc, metric_defs.display(r, "net_loc"))
            + _num_td(r.tcer, metric_defs.display(r, "tcer"))
            + _num_td(r.score, metric_defs.display(r, "score"))
            + f"<td>{_grade_pill(r.tier)}</td></tr>")
    return (f'<div class="scroll"><table class="sortable"><thead>{head}</thead>'
            f'<tbody>{"".join(rows)}</tbody></table></div>'
            '<p class="note">点击表头排序。</p>')


def _models_section(reports: list[SessionReport]) -> str:
    comps = metrics.compare_models(reports)
    if not comps:
        return '<p class="note">无逐模型数据。</p>'
    head = "<tr><th>指标</th>" + "".join(
        f"<th class='num'>{_esc(mc.display_name)}</th>" for mc in comps) + "</tr>"
    body: list[str] = []
    for g in metric_defs.MODEL_GROUPS:
        body.append(f'<tr class="sub"><td colspan="{len(comps) + 1}">{_esc(g.name)}</td></tr>')
        for m in g.metrics:
            raws = [metric_defs.model_raw(mc, m.key) for mc in comps]
            best = None
            if m.sentiment and len(comps) > 1:
                vals = [v for v in raws if v is not None]
                if vals:
                    best = max(vals) if m.sentiment == "up" else min(vals)
            tip = metric_defs.model_tip(m.key)
            tip_attr = f' title="{_esc(tip)}"' if tip else ""
            cells = []
            for mc, raw in zip(comps, raws):
                cls = "v best" if (best is not None and raw == best) else "v"
                cells.append(f'<td class="{cls}">{_esc(metric_defs.model_display(mc, m.key))}</td>')
            body.append(f"<tr><td{tip_attr}>{_esc(m.name)}</td>{''.join(cells)}</tr>")
    note = ('<p class="note">金色 = 该行最优值（按指标好坏方向）。'
            '产出/行为/质量指标按各模型在会话内的 Token 占比加权分摊：'
            '单模型会话全额归因，混合会话按占比拆分到每个模型。</p>')
    return f'<div class="scroll"><table>{head}{"".join(body)}</table></div>{note}'


def _mini_timeline(report: SessionReport, cap: int = 200) -> str:
    """每会话折叠区内的时间线缩略条：逐回合竖条，高∝Token，红=有工具错误。"""
    stats = report.usage.turn_stats
    if not stats:
        return ""
    shown = stats[:cap]
    max_tok = max((t.input_tokens + t.cache_write + t.cache_read
                   + t.output_tokens) for t in shown) or 1
    spans = []
    for t in shown:
        tot = t.input_tokens + t.cache_write + t.cache_read + t.output_tokens
        h = max(2, round(tot / max_tok * 24))
        color = theme.ERROR if t.errors else theme.ACCENT
        tip = f"回合 {t.turn + 1} · {tot:,} tok"
        if t.duration_ms is not None:
            tip += f" · {fmt.fmt_duration_ms(t.duration_ms, short=True)}"
        spans.append(
            f'<span title="{_esc(tip)}" style="display:inline-block;width:3px;'
            f'margin-right:1px;height:{h}px;background:{color};'
            f'vertical-align:bottom"></span>')
    note = (f'<span style="color:{theme.MUTED};font-size:11px"> 前 {cap}/{len(stats)} 回合</span>'
            if len(stats) > cap else "")
    return (f'<div style="line-height:0;padding:6px 14px 0">{"".join(spans)}{note}</div>')


def _session_details_section(reports: list[SessionReport]) -> str:
    parts = []
    for r in reports:
        summary = (f"{_esc(_session_title(r)[:60])}"
                   f'<span style="color:{theme.MUTED}"> · {_esc(fmt.fmt_dt(r.usage.started_at))}'
                   f" · {_esc(metric_defs.display(r, 'total_tokens'))} tok"
                   f" · {_esc(metric_defs.display(r, 'cost'))}</span>")
        parts.append(f"<details><summary>{summary}</summary>"
                     f"{_mini_timeline(r)}{_groups_section(r)}</details>")
    return "".join(parts)


def _unseen_warning(rep: SessionReport) -> str:
    if not rep.unseen_writes:
        return ""
    return (f'<div class="warn">⚠️ <b>{rep.unseen_writes} 次盲写文件</b>（F1 暴露）——'
            "Write 工具假设写入新文件（原大小 = 0）；若覆盖已有文件且会话数据未携带 "
            "originalFile 修正，写入行会高估、删除行会遗漏（Edit 不受影响）。"
            "该计数是净增行潜在高估的上界。</div>")


def _meta_line(rep: SessionReport, *, source_label: str, extra: str = "") -> str:
    u = rep.usage
    span = f"{fmt.fmt_dt(u.started_at)} → {fmt.fmt_dt(u.ended_at)}"
    parts = [
        f"来源 <b>{_esc(source_label or '-')}</b>",
        f"时间窗 <b>{_esc(span)}</b>",
        f"模型 <b>{_esc(fmt.models_label(u))}</b>",
    ]
    if extra:
        parts.append(extra)
    parts.append(f"生成于 <b>{fmt.fmt_now()}</b> · TCER v{_esc(_version())}")
    return f'<div class="meta">{" ｜ ".join(parts)}</div>'


def _shell(title: str, body: str) -> str:
    return (
        "<!DOCTYPE html>\n<html lang=\"zh-CN\">\n<head>\n<meta charset=\"utf-8\">\n"
        f"<meta name=\"viewport\" content=\"width=device-width, initial-scale=1\">\n"
        f"<title>{_esc(title)}</title>\n<style>{_css()}</style>\n</head>\n"
        f"<body>\n<main>\n{body}\n</main>\n<script>{_SORT_JS}</script>\n</body>\n</html>\n"
    )


# --------------------------------------------------------------------------- #
# Public entry points
# --------------------------------------------------------------------------- #
# 项目级报告的可选章节 key（导出弹窗据此勾选；默认全选 = 旧行为）。
PROJECT_SECTIONS = ("kpi", "groups", "score", "models", "sessions", "details")
_SECTION_LABELS = {
    "kpi": "总览 KPI", "groups": "六组指标", "score": "评分与排名",
    "models": "模型对比", "sessions": "会话明细表", "details": "每会话完整指标",
}


def render_project_html(
    reports: list[SessionReport],
    agg: SessionReport,
    *,
    project_name: str,
    source_label: str = "",
    n_sessions: int | None = None,
    n_subagents: int = 0,
    sections: "list[str] | tuple[str, ...] | set[str] | None" = None,
) -> str:
    """项目级自包含 HTML 报告：总览 + 聚合指标 + 综合效率分排名 + 模型对比 + 会话明细。

    ``sections`` 传入要保留的章节 key 子集（见 ``PROJECT_SECTIONS``），未选
    章节直接不拼对应 HTML 块；None = 全选（默认，旧行为不变）。
    """
    sel = set(PROJECT_SECTIONS) if sections is None else set(sections)
    n = n_sessions if n_sessions is not None else len(reports)
    extra = f"会话 <b>{n}</b>" + (f"（含 {n_subagents} 个子代理）" if n_subagents else "")
    body = [
        f"<h1>TCER 效率报告 · {_esc(project_name)}</h1>",
        _meta_line(agg, source_label=source_label, extra=extra),
    ]
    if "kpi" in sel:
        body += [_kpi_section(agg, n), _unseen_warning(agg)]
    if "groups" in sel:
        body += [
            "<h2>聚合指标（六组分类）</h2>",
            '<p class="note">白色 = 基准值/纯数据；黄色 = 含 magic number，仅作参考。'
            "综合效率分为三正交轴加权，聚合视图按聚合口径重算后有效。</p>",
            _groups_section(agg),
        ]
    if "score" in sel:
        body += ["<h2>综合效率分排名</h2>", _score_section(reports)]
    if "models" in sel:
        body += ["<h2>模型对比</h2>", _models_section(reports)]
    if "sessions" in sel:
        body += ["<h2>会话明细</h2>", _sessions_table(reports)]
    if "details" in sel:
        body += [
            "<h2>每会话完整指标</h2>",
            '<p class="note">点击展开单个会话的全部指标（与 GUI 指标分类页一致）。</p>',
            _session_details_section(reports),
        ]
    body.append(
        f"<footer>由 TCER v{_esc(_version())} 生成 · 纯离线分析，LOC 来自会话内工具调用回放，"
        "不依赖 git · 成本按 API 官方标价估算，非订阅实际扣费。</footer>")
    return _shell(f"TCER 效率报告 · {project_name}", "\n".join(body))


def _timeline_section(report: SessionReport) -> str:
    """逐回合时间线表（token 堆叠条 + 权威耗时 + 错误标记）。"""
    stats = report.usage.turn_stats
    if not stats:
        return ""
    max_tok = max((t.input_tokens + t.cache_write + t.cache_read
                   + t.output_tokens) for t in stats) or 1
    rows = []
    for i, t in enumerate(stats):
        total = t.input_tokens + t.cache_write + t.cache_read + t.output_tokens
        segs = "".join(
            f'<span style="display:inline-block;height:10px;width:{v / max_tok * 200:.0f}px;'
            f'background:{color}"></span>'
            for v, color in ((t.input_tokens, theme.TOKEN_COLORS["input"]),
                             (t.cache_write, theme.TOKEN_COLORS["cache_write"]),
                             (t.cache_read, theme.TOKEN_COLORS["cache_read"]),
                             (t.output_tokens, theme.TOKEN_COLORS["output"]))
            if v > 0)
        dur = fmt.fmt_duration_ms(t.duration_ms, short=True)
        err = f'<span style="color:{theme.ERROR}">⚠ {t.errors}</span>' if t.errors else ""
        rows.append(
            f"<tr><td class='v'>{i + 1}</td>"
            f"<td>{_esc(fmt.fmt_dt(t.ts, fmt.FMT_SHORT_SECOND) if t.ts else '-')}</td>"
            f"<td>{segs}</td>"
            + _num_td(total, f"{total:,}")
            + _num_td(t.output_tokens, f"{t.output_tokens:,}")
            + f"<td class='v'>{dur}</td><td class='v'>{t.tool_calls or ''}</td>"
            + f"<td>{err}</td></tr>")
    head = ("<tr><th class='num'>回合</th><th>时间</th><th>Token 构成</th>"
            "<th class='num'>总 Token</th><th class='num'>输出</th>"
            "<th class='num'>耗时</th><th class='num'>工具</th><th>错误</th></tr>")
    _tc = theme.TOKEN_COLORS
    legend = (f'<p class="note">构成条：<span style="color:{_tc["input"]}">■输入</span> '
              f'<span style="color:{_tc["cache_write"]}">■缓存写</span> '
              f'<span style="color:{_tc["cache_read"]}">■缓存读</span> '
              f'<span style="color:{_tc["output"]}">■输出</span>；'
              "耗时仅在数据源提供权威回合耗时（不含用户暂停）时显示。</p>")
    return (f"<h2>会话时间线（{len(stats)} 回合）</h2>{legend}"
            f'<div class="scroll"><table class="sortable"><thead>{head}</thead>'
            f'<tbody>{"".join(rows)}</tbody></table></div>')


def render_session_html(
    report: SessionReport,
    *,
    project_name: str,
    source_label: str = "",
) -> str:
    """会话级自包含 HTML 报告：单会话全部指标 + 时间线 + 该会话的模型对比。"""
    title = _session_title(report)
    body = [
        f"<h1>TCER 会话报告 · {_esc(title[:60])}</h1>",
        _meta_line(report, source_label=source_label,
                   extra=f"项目 <b>{_esc(project_name)}</b>"),
        _kpi_section(report, None),
        _unseen_warning(report),
        "<h2>全部指标（六组分类）</h2>",
        '<p class="note">白色 = 基准值/纯数据；黄色 = 含 magic number，仅作参考。</p>',
        _groups_section(report),
        _timeline_section(report),
        "<h2>模型对比（本会话）</h2>",
        _models_section([report]),
        f"<footer>由 TCER v{_esc(_version())} 生成 · 纯离线分析，LOC 来自会话内工具调用回放，"
        "不依赖 git · 成本按 API 官方标价估算，非订阅实际扣费。</footer>",
    ]
    return _shell(f"TCER 会话报告 · {title[:40]}", "\n".join(body))


def render_overview_html(rows: list[dict]) -> str:
    """项目总览自包含 HTML：全部项目可排序对比表（与总览弹窗同一数据）。

    ``rows`` 为弹窗 ``ProjectOverviewPopup._data`` 形状的 dict 列表
    （source/name/sessions/tokens/cost/net/tcer/chr/churn）。
    """
    total_cost = sum(d["cost"] or 0 for d in rows)
    total_tok = sum(d["tokens"] or 0 for d in rows)
    total_net = sum(d["net"] or 0 for d in rows)
    head = ("<tr><th>来源</th><th>项目</th><th class='num'>会话</th>"
            "<th class='num'>总 Token</th><th class='num'>成本</th>"
            "<th class='num'>净增行</th><th class='num'>TCER</th>"
            "<th class='num'>缓存命中</th><th class='num'>返工率</th></tr>")
    body = []
    for d in sorted(rows, key=lambda x: -(x["cost"] or 0)):
        body.append(
            "<tr>"
            f"<td>{_esc(d['source'])}</td><td>{_esc(d['name'])}</td>"
            + _num_td(d["sessions"], str(d["sessions"]))
            + _num_td(d["tokens"], f"{d['tokens']:,}")
            + _num_td(d["cost"], fmt.fmt_money(d["cost"]))
            + _num_td(d["net"], fmt.fmt_int(d["net"]))
            + _num_td(d["tcer"], fmt.fmt_float(d["tcer"], "0.0"))
            + _num_td(d["chr"], fmt.fmt_pct(d["chr"]))
            + _num_td(d["churn"], fmt.fmt_pct(d["churn"]))
            + "</tr>")
    body_html = [
        "<h1>TCER 项目总览</h1>",
        f'<div class="meta">{len(rows)} 个项目 ｜ <b>{total_tok:,}</b> Token ｜ '
        f"<b>{_esc(fmt.fmt_money(total_cost))}</b> ｜ 净增 <b>{total_net:,}</b> 行 ｜ "
        f"生成于 <b>{fmt.fmt_now()}</b>"
        f" · TCER v{_esc(_version())}</div>",
        f'<div class="scroll"><table class="sortable"><thead>{head}</thead>'
        f'<tbody>{"".join(body)}</tbody></table></div>',
        '<p class="note">点击表头排序。</p>',
        f"<footer>由 TCER v{_esc(_version())} 生成 · 聚合口径与桌面端项目总览一致。</footer>",
    ]
    return _shell("TCER 项目总览", "\n".join(body_html))


def _version() -> str:
    try:
        from tcer import __version__
        return __version__
    except (ImportError, AttributeError):
        return "unknown"
