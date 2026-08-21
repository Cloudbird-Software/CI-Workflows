# W4-C3 fixture 演示 mini 试卷 A（3 通过）——内容是演示 fixture，非真实考卷


def test_holdout_demo_a1():
    assert 2 + 2 == 4


def test_holdout_demo_a2():
    assert sorted([3, 1, 2]) == [1, 2, 3]


def test_holdout_demo_a3():
    assert "holdout" in "holdout-unseal"
