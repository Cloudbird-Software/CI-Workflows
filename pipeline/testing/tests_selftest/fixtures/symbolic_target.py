"""符号试点静态近似的目标 fixture（机械分支/循环计数用）。"""


def clamp(value, lo, hi):
    if value < lo:
        return lo
    elif value > hi:
        return hi
    return value


def batch_total(rows):
    total = 0
    for row in rows:
        if row > 0:
            total += row
    return total


def pick(tag):
    if tag == "a":
        return 1
    elif tag == "b":
        while True:
            break
        return 2
    return 0
