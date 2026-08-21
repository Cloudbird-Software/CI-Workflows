# W4-C3 fixture 演示 mini 试卷 B（1 通过 1 失败——制造明细与通过率差演示数据）


def test_holdout_demo_b1():
    assert len("cloudbird") == 9


def test_holdout_demo_b2():
    assert 1 == 2  # 演示用确定性失败：详情应只进 holdout 仓 issue/artifact，不进 PR 日志
