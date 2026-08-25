"""build-a2 离线自测（unittest，stdlib only；fixtures 自带，无网络/git）。

运行：python -m unittest discover -s tests -v
"""
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))
