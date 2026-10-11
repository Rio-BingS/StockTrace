"""A 股证券代码工具：所属交易所与交易所前缀格式（DAT-12 / A-28）。"""

from __future__ import annotations


def exchange(code):
    """6 位代码所属交易所：'sh' / 'sz' / 'bj'

    北交所 92/4/8 开头（先于 9 判断）；沪市 5（ETF/LOF）/6/9（B 股）开头；其余深市。
    """
    code = str(code).zfill(6)
    if code.startswith(('92', '4', '8')):
        return 'bj'
    if code.startswith(('5', '6', '9')):
        return 'sh'
    return 'sz'


def exchange_prefix(code, sep=''):
    """代码加交易所前缀：600000→sh600000；sep='.' 时为 BaoStock 格式 sh.600000"""
    code = str(code).zfill(6)
    return f'{exchange(code)}{sep}{code}'
