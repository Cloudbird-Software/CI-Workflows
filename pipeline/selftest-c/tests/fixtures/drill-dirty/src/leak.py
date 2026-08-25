"""生产脚本（drill fixture：脏树——三接缝外的越界引用）。"""
import os


def sync():
    # 越界：直接调用 CNB 接缝之外的端点
    os.system('curl -H "Authorization: $CNB_TOKEN" '
              'https://cnb.cool/cloudbird/legacy/-/raw/state.json')
    return "@CodeBuddy review"
