"""Mock 包：不连 B 站的假数据与客户端。

典型用法::

    from bilicleaner.mock import MockBiliClient, make_mock_data

    client = MockBiliClient()          # 已登录、无延迟、无风控注入
    data = make_mock_data(seed=42)     # 直接拿一套确定性数据

``create_app(mock=True)`` 会把 ``MockBiliClient()`` 当 ``client_factory`` 用，
``bili.*`` 里的模块函数可以原样跑在它身上（``get/post/nav`` 模拟真实 envelope）。
"""

from .fixtures import (
    DEFAULT_FAV_COUNT,
    DEFAULT_SEED,
    DYNAMIC_COUNT,
    FOLLOW_COUNT,
    GROUP_NAMES,
    MUTUAL_COUNT,
    SELF_FAV_COUNTS,
    TOVIEW_COUNT,
    make_mock_data,
)
from .mock_client import MockBiliClient

__all__ = [
    "MockBiliClient",
    "make_mock_data",
    "DEFAULT_SEED",
    "FOLLOW_COUNT",
    "MUTUAL_COUNT",
    "DYNAMIC_COUNT",
    "TOVIEW_COUNT",
    "DEFAULT_FAV_COUNT",
    "SELF_FAV_COUNTS",
    "GROUP_NAMES",
]
