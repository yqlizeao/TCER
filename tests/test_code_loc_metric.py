"""code_loc（代码行）派生指标契约：= net_loc − doc_net_loc − test_net_loc。

口径闭合（code + doc + test == net）+ None 安全 + 三通道一致（display/raw_value）。
"""
from types import SimpleNamespace

from tcer.gui import metric_defs as M


def _rep(net, doc, test):
    return SimpleNamespace(
        net_loc=net, doc_net_loc=doc, test_net_loc=test,
        usage=SimpleNamespace(), meta=SimpleNamespace(source="claude"))


def test_code_loc_value():
    rep = _rep(100, 20, 15)
    assert M._code_loc_native(rep) == 65
    assert M.display(rep, "code_loc") == "65"
    assert M.raw_value(rep, "code_loc") == 65.0


def test_code_loc_closure():
    """代码 + 文档 + 测试 == 净增行（口径闭合，不重不漏）。"""
    rep = _rep(100, 20, 15)
    assert M._code_loc_native(rep) + rep.doc_net_loc + rep.test_net_loc == rep.net_loc


def test_code_loc_none_net():
    rep = _rep(None, None, None)
    assert M._code_loc_native(rep) is None
    assert M.display(rep, "code_loc") == "-"
    assert M.raw_value(rep, "code_loc") is None


def test_code_loc_missing_doc_test_treated_as_zero():
    """部分数据源无 doc/test 拆分 → 按 0 计，code_loc == net_loc。"""
    rep = _rep(50, None, None)
    assert M._code_loc_native(rep) == 50


def test_code_loc_negative():
    """重构会话净删除 → code_loc 可为负。"""
    rep = _rep(-30, 0, 0)
    assert M._code_loc_native(rep) == -30
    assert M.raw_value(rep, "code_loc") == -30.0


def test_code_loc_registered():
    assert "code_loc" in M.ALL_KEYS
    assert M.METRIC_BY_KEY["code_loc"].name == "代码行"
