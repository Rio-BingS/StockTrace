"""数据源冷却（cooldown）与多源 failover。

某源连续失败后暂时跳过，降低东财限流概率（简易「负载均衡」）。
SourceChain 是全项目唯一的多源 failover 实现：市场接口（_first_ok/_safe_df_call）
与个股行情（services._try_sources）共用（ARC-03）。
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
                "数据源 %s 连续失败 %s 次，冷却 %ss: %s", name, st['fails'], _SOURCE_COOLDOWN_SEC, err
            )
        else:
            logger.warning("数据源 %s 失败 (%s): %s", name, st['fails'], err)
        _source_state_put(name, st)


def _is_empty_result(result):
    return result is None or (isinstance(result, pd.DataFrame) and result.empty)


class SourceChain:
    """统一的多源 failover（ARC-03）：个股与市场两条链路共用一套实现。

    按优先级依次尝试候选源 [(fn, source_name), ...]，返回第一个成功结果：
    - cooldown：源连续失败 _SOURCE_FAIL_THRESHOLD 次后冷却 _SOURCE_COOLDOWN_SEC，
      冷却中的源直接跳过（不等待）；成功清零。
    - backoff：callable(i) -> 秒，第 i 个源（0 起）失败后、真正尝试下一个源之前
      等待的秒数；None 表示不等待（市场链路）。
    - deadline：整条链路总时限（秒），超时不再尝试后续源；None 不限。
    - empty_ok：空 DataFrame 是合法结果（如当日无跌停股），原样返回且记成功。
    - empty_is_failure：空结果是否计入冷却。市场接口空即上游异常（默认 True）；
      个股按区间取数时空结果可能是正常无数据，只换下一个源、不冷却。
    - sleep：等待函数，便于调用方用自己模块的 time.sleep（测试可打桩）。
    - last_resort：未冷却的源优先；全部失败时仍强行尝试排在最后的冷却源一次
      （大盘资金历史：宁可多打一次也不让页面整体空白）。
    None 始终视为失败（换下一个源）。返回 (result, source_name)，全部失败 (None, None)。
    """

    def __init__(self, backoff=None, deadline=None, empty_is_failure=True, sleep=None,
                 last_resort=False):
        self.backoff = backoff
        self.last_resort = last_resort
        self.deadline = deadline
        self.empty_is_failure = empty_is_failure
        self.sleep = sleep or time.sleep

    def run(self, candidates, *args, empty_ok=False, **kwargs):
        started = time.monotonic()
        pending_wait = 0
        candidates = list(candidates)
        forced = None
        if self.last_resort:
            hot = [c for c in candidates if not _source_is_cool(c[1])]
            cool = [c for c in candidates if _source_is_cool(c[1])]
            candidates = hot + cool
            forced = candidates[-1][1] if candidates else None
        for i, (fn, name) in enumerate(candidates):
            if _source_is_cool(name) and name != forced:
                logger.debug("跳过冷却中的数据源 %s", name)
                continue
            if self.deadline is not None and time.monotonic() - started >= self.deadline:
                logger.warning("多源链路超过总时限 %ss，不再尝试 %s 及之后的源", self.deadline, name)
                break
            if pending_wait:
                self.sleep(pending_wait)
                pending_wait = 0
            try:
                result = fn(*args, **kwargs)
            except Exception as e:  # noqa: BLE001 上游边界：异常类型不固定，记入冷却统计后换下一个源
                _source_mark_fail(name, e)
                pending_wait = self.backoff(i) if self.backoff else 0
                continue
            if _is_empty_result(result):
                if empty_ok and result is not None:
                    _source_mark_ok(name)
                    return result, name
                if self.empty_is_failure:
                    _source_mark_fail(name, 'empty')
                else:
                    logger.debug("数据源 %s 返回空数据，尝试下一个", name)
                pending_wait = self.backoff(i) if self.backoff else 0
                continue
            _source_mark_ok(name)
            return result, name
        return None, None


# 市场链路：有冷却、失败立即换源（东财限流时等待无益，冷却已兜住重复打上游）
_MARKET_CHAIN = SourceChain()


def _safe_df_call(fn, *args, source_name=None, empty_ok=False, **kwargs):
    """单次调用；可选 source_name 参与冷却统计。

    empty_ok=True：空 DataFrame 是合法结果（如当日无跌停股），原样返回且不计入
    冷却；默认 False 时空结果视为上游失败（DAT-10）。None 始终视为失败。
    """
    name = source_name or getattr(fn, '__name__', 'unknown')
    result, _ = _MARKET_CHAIN.run([(fn, name)], *args, empty_ok=empty_ok, **kwargs)
    return result


def _first_ok(candidates, *args, empty_ok=False, **kwargs):
    """多源路由：按优先级逐个尝试候选源，返回第一个成功的结果。

    candidates: [(fn, source_name), ...]，fn 用 *args/**kwargs 调用。
    每个候选都受冷却约束（冷却中的直接跳过），成败都计入冷却统计；
    上一个源失败会立即尝试下一个，不重试同一个源。
    empty_ok=True 时空 DataFrame 视为合法结果直接返回（见 _safe_df_call）。
    返回 (result, source_name)；全部失败返回 (None, None)。
    """
    return _MARKET_CHAIN.run(candidates, *args, empty_ok=empty_ok, **kwargs)


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
