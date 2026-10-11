"""BaoStock 全局会话串行化（从 services 抽出，供数据源与交易日历共用，ARC-02）。

BaoStock 是模块级全局会话：多线程各自 login/logout 会互相登出（REL-06），
所有 BaoStock 调用经 bs_session 串行化 login → query → logout。
"""

import logging
import threading
from contextlib import contextmanager

logger = logging.getLogger(__name__)

bs_lock = threading.Lock()


@contextmanager
def bs_session():
    """持锁执行一次 BaoStock 会话（login → query → logout），yield baostock 模块"""
    import baostock as bs
    with bs_lock:
        lg = bs.login()
        # BaoStock 登录失败（网络不可达等）不抛异常，只在返回值里给 error_code；
        # 不检查的话调用方会拿到空结果当「无数据」，冷却/负缓存都不生效
        err = getattr(lg, 'error_code', '0')
        if isinstance(err, str) and err != '0':
            raise RuntimeError(f"BaoStock 登录失败: {err} {getattr(lg, 'error_msg', '')}")
        try:
            yield bs
        finally:
            try:
                bs.logout()
            except Exception as e:  # noqa: BLE001 上游边界：登出失败不影响已取到的结果
                logger.debug("BaoStock 登出失败: %s", e)
