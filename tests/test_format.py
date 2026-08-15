"""format.py 护栏：时长档位、日期格式常量、缺省值。"""
from __future__ import annotations

from tcer.core import format as fmt


def test_fmt_duration_ms_short():
    assert fmt.fmt_duration_ms(None, short=True) == "-"
    assert fmt.fmt_duration_ms(0, short=True) == "-"
    assert fmt.fmt_duration_ms(500, short=True) == "<1s"
    assert fmt.fmt_duration_ms(4300, short=True) == "4.3s"
    assert fmt.fmt_duration_ms(720_000, short=True) == "12m"
    assert fmt.fmt_duration_ms(7_200_000, short=True) == "2.0h"


def test_fmt_duration_ms_long():
    assert fmt.fmt_duration_ms(None) == "-"
    assert fmt.fmt_duration_ms(0) == "-"
    assert fmt.fmt_duration_ms(38 * 60_000) == "38 分钟"
    assert fmt.fmt_duration_ms(int(2.4 * 3_600_000)) == "2.4 小时"


def test_format_constants_and_defaults():
    assert fmt.FMT_MINUTE == "%Y-%m-%d %H:%M"
    assert fmt.FMT_SECOND == "%Y-%m-%d %H:%M:%S"
    assert fmt.FMT_SHORT_MINUTE == "%m-%d %H:%M"
    assert fmt.FMT_SHORT_SECOND == "%m-%d %H:%M:%S"
    assert fmt.fmt_dt(None) == "-"
    assert fmt.fmt_dt(0) == "-"


def test_fmt_approx_cn():
    from tcer.core.format import fmt_approx_cn
    assert fmt_approx_cn(41041696) == "≈ 0.41亿"
    assert fmt_approx_cn(200_000_000) == "≈ 2.00亿"
    assert fmt_approx_cn(150_000) == "≈ 15万"
    assert fmt_approx_cn(9_999_999) == "≈ 1000万"
    assert fmt_approx_cn(200_000_000) is not None
    assert fmt_approx_cn(9999) is None
    assert fmt_approx_cn(None) is None
