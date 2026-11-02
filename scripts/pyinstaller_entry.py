"""PyInstaller 打包入口：等价于 ``python -m bilicleaner``。

不能直接拿 ``src/bilicleaner/__main__.py`` 当 PyInstaller 入口，因为那样它会被
当成顶层脚本执行，文件里的相对导入（``from .const import ...``）会失败。
这个小入口从包外导入，PyInstaller 会把整个 ``bilicleaner`` 包一起打进去。
"""

from __future__ import annotations

import sys

from bilicleaner.__main__ import main


if __name__ == "__main__":
    sys.exit(main())
