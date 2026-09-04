"""llm_reports 单测：持久化 / 排序 / 上限 / 删除 / 损坏容错。"""
import json


def _use_tmp(monkeypatch, tmp_path):
    from tcer.core import llm_reports
    monkeypatch.setattr(llm_reports, "_path",
                        lambda: tmp_path / "tcer_llm_reports.json")
    return llm_reports


def _entry(i: int) -> dict:
    return {"id": f"r{i}", "created_at": 1000 + i, "text": f"报告{i}"}


def test_missing_file_empty(monkeypatch, tmp_path):
    lr = _use_tmp(monkeypatch, tmp_path)
    assert lr.load() == []


def test_append_load_newest_first(monkeypatch, tmp_path):
    lr = _use_tmp(monkeypatch, tmp_path)
    lr.append(_entry(1))
    lr.append(_entry(2))
    assert [r["id"] for r in lr.load()] == ["r2", "r1"]   # 新→旧


def test_append_trims_to_max(monkeypatch, tmp_path):
    lr = _use_tmp(monkeypatch, tmp_path)
    for i in range(lr.MAX_REPORTS + 30):
        lr.append(_entry(i))
    reports = lr.load()
    assert len(reports) == lr.MAX_REPORTS
    assert reports[0]["id"] == f"r{lr.MAX_REPORTS + 29}"   # 最新的在
    assert reports[-1]["id"] == "r30"                       # 最旧的被裁


def test_delete_and_clear(monkeypatch, tmp_path):
    lr = _use_tmp(monkeypatch, tmp_path)
    lr.append(_entry(1))
    lr.append(_entry(2))
    lr.delete("r1")
    assert [r["id"] for r in lr.load()] == ["r2"]
    lr.clear()
    assert lr.load() == []


def test_corrupted_file_returns_empty(monkeypatch, tmp_path):
    lr = _use_tmp(monkeypatch, tmp_path)
    (tmp_path / "tcer_llm_reports.json").write_text("not-json", encoding="utf-8")
    assert lr.load() == []
    (tmp_path / "tcer_llm_reports.json").write_text(
        json.dumps([{"id": "x"}, "junk", 3]), encoding="utf-8")
    assert [r["id"] for r in lr.load()] == ["x"]            # 非 dict 项被滤
