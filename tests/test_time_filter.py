"""时间筛选口径一致性：左栏 mtime 初筛 vs 会话 started/ended。

幽灵项目 = 项目在左栏可见（文件 mtime 在范围内），点开却 0 会话（内容
时间戳不在范围内）。两道防线：
  1. ``paths.project_latest_activity_ms`` 不计 journal.jsonl 等非会话文件
     （见 tests/test_paths.py）。
  2. 分析证实 0 会话后，GUI 把该项目记入 ``_proven_empty`` 并隐藏、自动
     推进（本文件测这道防线；不建 Tk root，无头可跑）。
"""
from __future__ import annotations

from tcer.core.models import ProjectRef
from tcer.gui.app import TcerGui


def _ref(key: str) -> ProjectRef:
    return ProjectRef(source="claude", key=key, display_name=key, cwd=None,
                      path=None)


class _Filter:
    """FilterBar 的最小替身：只提供 _compute_hidden / _maybe_hide_proven_empty 用的方法。"""

    def __init__(self, since):
        self.since = since
        self.statuses: list[str] = []

    def get_params(self):
        return {"since": self.since, "until": None, "task_type": "auto"}

    def set_status(self, text, **_kw):
        self.statuses.append(text)


def _shell(since, activity, proven_empty=None):
    """绕过 TcerGui.__init__（建整套 Tk 界面）造壳：只装 _compute_hidden /
    _maybe_hide_proven_empty 触及的状态。"""
    app = object.__new__(TcerGui)
    app._projects = [_ref("p0"), _ref("p1"), _ref("p2")]
    app._activity_cache = dict(activity)
    app._proven_empty = dict(proven_empty or {})
    app._selected_project_idx = 1
    app.filter = _Filter(since)
    app._apply_called = 0

    def _apply():
        app._apply_called += 1

    app.apply_time_filter = _apply  # type: ignore[method-assign]
    return app


def test_compute_hidden_includes_proven_empty():
    # p0/p1 的 mtime 都在今天内，p2 早于今天；p1 已被分析证实今天 0 会话。
    app = _shell("2026-11-06",
                 {0: 1_793_894_400_000, 1: 1_793_894_400_000, 2: 1_700_000_000_000},
                 {"2026-11-06": {1}})
    assert app._compute_hidden() == {1, 2}
    # 换一个 since 值：该日期的 proven-empty 结论不适用，只剩 mtime 口径
    # （p0/p1 的 mtime 是 11-06 00:00，也早于 11-07，三个全藏）。
    app.filter.since = "2026-11-07"
    assert app._compute_hidden() == {0, 1, 2}
    # 无 since（全部）：全显。
    app.filter.since = None
    assert app._compute_hidden() == set()


def test_maybe_hide_proven_empty_records_and_advances():
    app = _shell("2026-11-06",
                 {0: 1_793_894_400_000, 1: 1_793_894_400_000, 2: 1_793_894_400_000})
    assert app._maybe_hide_proven_empty(0) is True
    assert app._proven_empty == {"2026-11-06": {1}}
    assert app._apply_called == 1
    assert app._compute_hidden() == {1}
    assert any("p1" in s and "无会话活动" in s for s in app.filter.statuses)


def test_maybe_hide_proven_empty_noop_cases():
    app = _shell("2026-11-06",
                 {0: 1_793_894_400_000, 1: 1_793_894_400_000, 2: 1_793_894_400_000})
    # 有会话 → 不接管；无 since（全部）→ 不接管（真空项目由 _empty_projects 置灰）。
    assert app._maybe_hide_proven_empty(3) is False
    app.filter.since = None
    assert app._maybe_hide_proven_empty(0) is False
    assert app._proven_empty == {}
    assert app._apply_called == 0
    # 未选中项目时不接管（无 idx 可记）。
    app.filter.since = "2026-11-06"
    app._selected_project_idx = None
    assert app._maybe_hide_proven_empty(0) is False
