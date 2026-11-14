import os
import sys
from pathlib import Path

# 让 tests/ 能直接 import 项目根目录下的 web_app
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
os.environ.setdefault("TG_AUTO_START_MONITORS", "0")
