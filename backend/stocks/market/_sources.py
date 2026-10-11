"""数据源冷却（cooldown）与多源 failover。

某源连续失败后暂时跳过，降低东财限流概率（简易「负载均衡」）。
"""

from __future__ import annotations

import logging
import threading
import time

import pandas as pd

logger = logging.getLogger(__name__)

# 源失败冷却：秒（东财限流时拉长）
_SOURCE_COOLDOWN_SEC = 180
_SOURCE_FAIL_THRESHOLD = 2
_source_lock = threading.Lock()
# name -> {fails, cool_until, last_ok, last_err}；按最近更新排序，超过上限淘汰最旧
# （source_name 可能拼入周期等参数，需防无界增长，REL-07）
_SOURCE_STATE_MAX = 256
_source_state = {}


def _source_state_put(name, st):
    """写入源状态并移到最近位置；调用方须持有 _source_lock"""
    _source_state.pop(name, None)
    _source_state[name] = st
    while len(_source_state) > _SOURCE_STATE_MAX:
        _source_state.pop(next(iter(_source_state)))


def _source_is_cool(name):
    st = _source_state.get(name) or {}
    until = st.get('cool_until') or 0
    return time.time() < until


def _source_mark_ok(name):
    with _source_lock:
        _source_state_put(name, {
            'fails': 0,
            'cool_until': 0,
            'last_ok': time.time(),
            'last_err': '',
        })


def _source_mark_fail(name, err):
    with _source_lock:
        st = dict(_source_state.get(name) or {'fails': 0, 'cool_until': 0})
        st['fails'] = int(st.get('fails') or 0) + 1
        st['last_err'] = str(err)[:200]
        if st['fails'] >= _SOURCE_FAIL_THRESHOLD:
            st['cool_until'] = time.time() + _SOURCE_COOLDOWN_SEC
            logger.warning(
                f"数据源 {name} 连续失败 {st['fails']} 次，冷却 {_SOURCE_COOLDOWN_SEC}s: {err}"
            )
        else:
            logger.warning(f"数据源 {name} 失败 ({st['fails']}): {err}")
        _source_state_put(name, st)


def _is_empty_result(result):
    return result is None or (isinstance(result, pd.DataFrame) and result.empty)


def _safe_df_call(fn, *args, source_name=None, empty_ok=False, **kwargs):
    """单次调用；可选 source_name 参与冷却统计。

    empty_ok=True：空 DataFrame 是合法结果（如当日无跌停股），原样返回且不计入
    冷却；默认 False 时空结果视为上游失败（DAT-10）。None 始终视为失败。
    """
    name = source_name or getattr(fn, '__name__', 'unknown')
    if _source_is_cool(name):
        logger.debug(f"跳过冷却中的数据源 {name}")
        return None
    try:
        result = fn(*args, **kwargs)
        if _is_empty_result(result):
            if empty_ok and result is not None:
                _source_mark_ok(name)
                return result
            _source_mark_fail(name, 'empty')
            return None
        _source_mark_ok(name)
        return result
    except Exception as e:
        _source_mark_fail(name, e)
        return None


def _first_ok(candidates, *args, empty_ok=False, **kwargs):
    """多源路由：按优先级逐个尝试候选源，返回第一个成功的结果。

    candidates: [(fn, source_name), ...]，fn 用 *args/**kwargs 调用。
    每个候选都受冷却约束（冷却中的直接跳过），成败都计入冷却统计；
    上一个源失败会立即尝试下一个，不重试同一个源。
    empty_ok=True 时空 DataFrame 视为合法结果直接返回（见 _safe_df_call）。
    返回 (result, source_name)；全部失败返回 (None, None)。
    """
    for fn, name in candidates:
        if _source_is_cool(name):
            logger.debug(f"跳过冷却中的数据源 {name}")
            continue
        try:
            result = fn(*args, **kwargs)
            if _is_empty_result(result):
                if empty_ok and result is not None:
                    _source_mark_ok(name)
                    return result, name
                _source_mark_fail(name, 'empty')
                continue
            _source_mark_ok(name)
            return result, name
        except Exception as e:
            _source_mark_fail(name, e)
            continue
    return None, None


def get_source_health():
    """调试用：各源冷却状态。"""
    now = time.time()
    out = {}
    with _source_lock:
        for name, st in _source_state.items():
            out[name] = {
                'fails': st.get('fails', 0),
                'cooling': bool(st.get('cool_until', 0) > now),
                'cool_remaining_sec': max(0, int((st.get('cool_until') or 0) - now)),
                'last_err': st.get('last_err', ''),
            }
    return out
