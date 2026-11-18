"""`python -m v5.app.cli` 入口（拆分后包目录需要独立 __main__）。"""

from __future__ import annotations

import sys

from . import main

if __name__ == "__main__":
    sys.exit(main())
