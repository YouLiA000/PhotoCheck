"""双击入口：起本地服务并打开浏览器。

包一层是为了让启动方式与「怎么安装」无关：
无论从仓库直接跑、还是从便携包里跑，都是 ``python app/main.py``。
"""

from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))
VENDOR = ROOT / "app" / "vendor"
if str(VENDOR) not in sys.path:
    sys.path.insert(0, str(VENDOR))

from photocheck.network_guard import assert_network_blocked, install_network_guard  # noqa: E402
from app.server import main  # noqa: E402

if __name__ == "__main__":
    guard = install_network_guard()
    try:
        assert_network_blocked()
        raise SystemExit(main())
    finally:
        guard.__exit__(None, None, None)
