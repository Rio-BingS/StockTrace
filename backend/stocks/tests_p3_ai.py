"""P3 回归测试：AI 方向（AIX-07 服务商优先级/参数风格、AI 后台任务落库、选股翻译/点评异步化）。"""
from types import SimpleNamespace

from django.test import TestCase, TransactionTestCase
from rest_framework.test import APIClient

from .tests_p2_sec_ai_ops import _ai_fixtures, _ai_patches


def _rows(resp):
    body = resp.json()
    return body['results'] if isinstance(body, dict) and 'results' in body else body


class AiProviderPriorityTests(TestCase):
    """AIX-07：priority 越小越优先，列表顺序与调用回退顺序一致；param_style 可配置。"""

    def setUp(self):
        from stocks.ai.crypto import encrypt_api_key
        from stocks.models import AiProvider
        self.p1, self.stock = _ai_fixtures()
        # 后录入但优先级更高（数字更小）
        self.p2 = AiProvider.objects.create(
            name='P2', base_url='https://b.com', model='m2', priority=10,
            api_key_encrypted=encrypt_api_key('sk-p2-1111'),
        )
        self.post = _ai_patches(self)
        self.client = APIClient()

    def _urls(self):
        return [c.args[0] for c in self.post.call_args_list]

    def test_defaults(self):
        self.assertEqual(self.p1.priority, 100)
        self.assertEqual(self.p1.param_style, 'auto')

    def test_selection_and_list_follow_priority(self):
        from stocks.ai.llm import enabled_providers
        from stocks.tests_ai import _fake_response
        from stocks.tests_p1_ai import _ok
        self.assertEqual([p.id for p in enabled_providers()], [self.p2.id, self.p1.id])
        listed = [p['id'] for p in _rows(self.client.get('/api/ai-providers/'))]
        self.assertEqual(listed, [self.p2.id, self.p1.id])
        # 优先级高的先调，鉴权失败回退到下一个
        self.post.side_effect = [_fake_response(401, text='no'), _ok('回退成功')]
        r = self.client.post(f'/api/stocks/{self.stock.id}/ai-analysis/')
        self.assertEqual(r.status_code, 200)
        self.assertEqual(self._urls(), ['https://b.com/chat/completions',
                                        'https://mock.local/chat/completions'])

    def test_same_priority_falls_back_to_creation_order(self):
        from stocks.ai.llm import enabled_providers
        self.p2.priority = 100
        self.p2.save()
        self.assertEqual([p.id for p in enabled_providers()], [self.p1.id, self.p2.id])

    def test_serializer_reads_and_writes_fields(self):
        r = self.client.patch(f'/api/ai-providers/{self.p1.id}/',
                              {'priority': 1, 'param_style': 'reasoning'}, format='json')
        self.assertEqual(r.status_code, 200, r.content)
        self.assertEqual((r.data['priority'], r.data['param_style']), (1, 'reasoning'))
        self.p1.refresh_from_db()
        self.assertEqual((self.p1.priority, self.p1.param_style), (1, 'reasoning'))
        listed = [p['id'] for p in _rows(self.client.get('/api/ai-providers/'))]
        self.assertEqual(listed[0], self.p1.id)

    def test_create_with_fields(self):
        r = self.client.post('/api/ai-providers/', {
            'name': 'N', 'base_url': 'https://b.com', 'model': 'o3-mini', 'api_key': 'sk-new-2222',
            'priority': 5, 'param_style': 'openai_reasoning',
        }, format='json')
        self.assertEqual(r.status_code, 201, r.content)
        self.assertEqual(r.data['priority'], 5)
        self.assertEqual(r.data['param_style'], 'openai_reasoning')

    def test_invalid_values_rejected(self):
        url = f'/api/ai-providers/{self.p1.id}/'
        for bad in (-1, 10000):
            r = self.client.patch(url, {'priority': bad}, format='json')
            self.assertEqual(r.status_code, 400, bad)
            self.assertIn('priority', r.data)
        r = self.client.patch(url, {'param_style': 'turbo'}, format='json')
        self.assertEqual(r.status_code, 400)
        self.assertIn('param_style', r.data)

    def test_param_style_field_drives_payload(self):
        from stocks.ai.llm import build_payload
        # auto：按模型名推断
        p = build_payload(SimpleNamespace(model='o1', param_style='auto'), [], 0.3, 100)
        self.assertEqual(p.get('max_completion_tokens'), 100)
        self.assertNotIn('temperature', p)
        # 显式 reasoning：模型名看不出是推理模型，也不传 temperature
        self.p1.param_style = 'reasoning'
        p = build_payload(self.p1, [], 0.3, 100)
        self.assertNotIn('temperature', p)
        self.assertEqual(p['max_tokens'], 100)
        # 显式 standard 覆盖模型名推断
        self.p1.model, self.p1.param_style = 'deepseek-reasoner', 'standard'
        p = build_payload(self.p1, [], 0.3, 100)
        self.assertEqual((p['temperature'], p['max_tokens']), (0.3, 100))


# ---- AI 后台任务落库（AIX-08 收尾）----

def _make_job(**kw):
    import uuid
    from stocks.models import AiJob
    kw.setdefault('job_id', uuid.uuid4().hex)
    kw.setdefault('kind', 'analysis')
    return AiJob.objects.create(**kw)


def _ago(seconds):
    from datetime import timedelta
    from django.utils import timezone
    return timezone.now() - timedelta(seconds=seconds)


class AiJobQueueTests(TestCase):
    """认领互斥、全局并发、重启恢复、保留期与轮询 JSON 形状。"""

    def setUp(self):
        from unittest import mock
        from stocks.ai import jobs
        self.jobs = jobs
        patcher = mock.patch.object(jobs, 'ensure_worker')
        self.ensure_worker = patcher.start()
        self.addCleanup(patcher.stop)
        self.calls = []
        jobs._handlers['p3_test'] = self._handler
        self.addCleanup(jobs._handlers.pop, 'p3_test', None)
        self.client = APIClient()

    def _handler(self, params):
        self.calls.append(params)
        if params.get('boom'):
            raise RuntimeError('secret-boom')
        return params.get('status', 200), {'echo': params.get('n')}

    def test_claim_is_exclusive(self):
        job_id = self.jobs.submit('p3_test', {'n': 1})
        first = self.jobs.claim_next('w1')
        self.assertEqual(first.job_id, job_id)
        self.assertEqual((first.status, first.owner, first.attempts), ('running', 'w1', 1))
        self.assertIsNotNone(first.lease_until)
        # 另一个 worker 再抢：没有 pending 了
        self.assertIsNone(self.jobs.claim_next('w2'))

    def test_conditional_update_loses_race(self):
        from unittest import mock
        from stocks.models import AiJob
        job = _make_job(kind='p3_test')
        real_filter = AiJob.objects.filter

        def racing_filter(*args, **kwargs):
            # 模拟另一个 worker 在本 worker 读出候选之后、UPDATE 之前抢走
            if kwargs.get('pk') == job.pk and kwargs.get('status') == 'pending':
                real_filter(pk=job.pk).update(status='running', owner='other')
            return real_filter(*args, **kwargs)

        with mock.patch.object(AiJob.objects, 'filter', side_effect=racing_filter):
            self.assertIsNone(self.jobs.claim_next('me'))
        job.refresh_from_db()
        self.assertEqual(job.owner, 'other')

    def test_global_concurrency_limit(self):
        from datetime import timedelta
        from django.test import override_settings
        from django.utils import timezone
        _make_job(kind='p3_test', status='running', owner='x',
                  lease_until=timezone.now() + timedelta(seconds=60))
        _make_job(kind='p3_test')
        with override_settings(STOCKTRACE_AI_MAX_CONCURRENCY=1):
            self.assertIsNone(self.jobs.claim_next())
        with override_settings(STOCKTRACE_AI_MAX_CONCURRENCY=2):
            self.assertIsNotNone(self.jobs.claim_next())

    def test_run_pending_executes_once_in_order(self):
        a = self.jobs.submit('p3_test', {'n': 1})
        b = self.jobs.submit('p3_test', {'n': 2, 'status': 400, 'error': 'x'})
        self.assertEqual(self.jobs.run_pending(), 2)
        self.assertEqual(self.jobs.run_pending(), 0)
        self.assertEqual([c['n'] for c in self.calls], [1, 2])
        self.assertEqual(self.jobs.get(a)['status'], 'done')
        failed = self.jobs.get(b)
        self.assertEqual((failed['status'], failed['http_status']), ('failed', 400))

    def test_handler_exception_and_unknown_kind(self):
        a = self.jobs.submit('p3_test', {'boom': True})
        b = self.jobs.submit('no_such_kind', {})
        with self.assertLogs('stocks.ai.jobs', level='ERROR'):
            self.jobs.run_pending()
        for job_id in (a, b):
            snap = self.jobs.get(job_id)
            self.assertEqual((snap['status'], snap['http_status']), ('failed', 500))
            self.assertEqual(snap['result']['error'], 'internal')
            self.assertNotIn('secret-boom', str(snap))

    def test_poll_json_shape(self):
        first = self.jobs.submit('p3_test', {'n': 1})
        second = self.jobs.submit('p3_test', {'n': 2})
        body = self.client.get(f'/api/ai/jobs/{second}/').json()
        self.assertEqual(body, {'job_id': second, 'kind': 'p3_test', 'status': 'queued',
                                'queued_ahead': 1})
        # 未完成的任务被轮询时拉起执行线程（重启后继续处理）
        self.ensure_worker.assert_called()
        claimed = self.jobs.claim_next()
        body = self.client.get(f'/api/ai/jobs/{first}/').json()
        self.assertEqual(body, {'job_id': first, 'kind': 'p3_test', 'status': 'running'})
        self.jobs.run_job(claimed)
        body = self.client.get(f'/api/ai/jobs/{first}/').json()
        self.assertEqual(body, {'job_id': first, 'kind': 'p3_test', 'status': 'done',
                                'http_status': 200, 'result': {'echo': 1}})

    def test_submit_wakes_worker_after_commit(self):
        with self.captureOnCommitCallbacks(execute=True):
            self.jobs.submit('p3_test', {})
        self.ensure_worker.assert_called_once()

    def test_expired_result_404_and_pruned(self):
        job = _make_job(kind='p3_test', status='done', http_status=200, result={},
                        finished_at=_ago(3601))
        self.assertIsNone(self.jobs.get(job.job_id))
        self.assertEqual(self.client.get(f'/api/ai/jobs/{job.job_id}/').status_code, 404)
        fresh = _make_job(kind='p3_test', status='done', http_status=200, result={},
                          finished_at=_ago(60))
        self.assertEqual(self.jobs.prune(), 1)
        self.assertTrue(type(job).objects.filter(pk=fresh.pk).exists())

    def test_prune_drops_day_old_orphans(self):
        from stocks.models import AiJob
        job = _make_job(kind='p3_test', status='running')
        AiJob.objects.filter(pk=job.pk).update(created_at=_ago(2 * 86400))
        self.assertEqual(self.jobs.prune(), 1)

    def test_owner_mismatch_does_not_overwrite(self):
        self.jobs.submit('p3_test', {'n': 1})
        job = self.jobs.claim_next('w1')
        type(job).objects.filter(pk=job.pk).delete()
        with self.assertLogs('stocks.ai.jobs', level='WARNING'):
            self.jobs.run_job(job)


class AiJobRecoveryTests(TestCase):
    """进程重启：pending 由任一 worker 继续执行；running 租约过期标失败；排队超时撤销占位。"""

    def setUp(self):
        from unittest import mock
        from stocks.ai import jobs
        self.jobs = jobs
        patcher = mock.patch.object(jobs, 'ensure_worker')
        patcher.start()
        self.addCleanup(patcher.stop)
        self.provider, self.stock = _ai_fixtures()
        self.post = _ai_patches(self)
        self.client = APIClient()

    def _pending_log(self):
        from stocks.models import AiCallLog
        return AiCallLog.objects.create(purpose='analysis', provider=self.provider,
                                        stock=self.stock, success=False, status='pending')

    def test_running_past_lease_marked_interrupted(self):
        from stocks.models import AiCallLog
        log = self._pending_log()
        job = _make_job(status='running', owner='dead:1', lease_until=_ago(1),
                        params={'call_log_id': log.id})
        with self.assertLogs('stocks.ai.jobs', level='WARNING'):
            self.assertEqual(self.jobs.recover_stale(), (1, 0))
        snap = self.jobs.get(job.job_id)
        self.assertEqual((snap['status'], snap['http_status']), ('failed', 500))
        self.assertEqual(snap['result']['error'], 'interrupted')
        log.refresh_from_db()
        # 上游可能已计费：流水按失败回填，计入每日上限
        self.assertEqual(log.status, AiCallLog.STATUS_FAILED)
        self.assertEqual(log.error_reason, 'interrupted')

    def test_running_within_lease_untouched(self):
        from datetime import timedelta
        from django.utils import timezone
        job = _make_job(status='running', owner='alive:1',
                        lease_until=timezone.now() + timedelta(seconds=30))
        self.assertEqual(self.jobs.recover_stale(), (0, 0))
        job.refresh_from_db()
        self.assertEqual(job.status, 'running')

    def test_stale_pending_expired_and_reservation_cancelled(self):
        from stocks.models import AiCallLog, AiJob
        log = self._pending_log()
        job = _make_job(params={'call_log_id': log.id})
        AiJob.objects.filter(pk=job.pk).update(created_at=_ago(601))
        with self.assertLogs('stocks.ai.jobs', level='WARNING'):
            self.assertEqual(self.jobs.recover_stale(), (0, 1))
        snap = self.jobs.get(job.job_id)
        self.assertEqual((snap['status'], snap['http_status']), ('failed', 503))
        self.assertEqual(snap['result']['error'], 'expired')
        # 从未调用上游：撤销占位，不占额度
        self.assertFalse(AiCallLog.objects.filter(pk=log.id).exists())

    def test_pending_resumed_after_restart(self):
        from stocks.models import AiCallLog
        from stocks.tests_p1_ai import _ok
        self.post.return_value = _ok('重启后完成')
        r = self.client.post(f'/api/stocks/{self.stock.id}/ai-analysis/?async=1')
        self.assertEqual(r.status_code, 202)
        # 模拟进程重启：内存里什么都没有，新进程按库里的 pending 继续执行
        self.jobs._worker_thread = None
        self.assertEqual(self.jobs.run_pending(), 1)
        poll = self.client.get(f"/api/ai/jobs/{r.data['job_id']}/").json()
        self.assertEqual(poll['status'], 'done')
        self.assertEqual(poll['result']['analysis'], '重启后完成')
        self.assertTrue(AiCallLog.objects.get().success)

    def test_late_finish_overrides_interrupted(self):
        from stocks.tests_p1_ai import _ok
        self.post.return_value = _ok('迟到的结果')
        r = self.client.post(f'/api/stocks/{self.stock.id}/ai-analysis/?async=1')
        job = self.jobs.claim_next('slow:1')
        type(job).objects.filter(pk=job.pk).update(lease_until=_ago(1))
        with self.assertLogs('stocks.ai.jobs', level='WARNING'):
            self.jobs.recover_stale()
        # 原执行者其实还活着并跑完：以真实结果为准
        self.jobs.run_job(job)
        poll = self.client.get(f"/api/ai/jobs/{r.data['job_id']}/").json()
        self.assertEqual(poll['status'], 'done')

    def test_scheduler_cleanup_step(self):
        from stocks import scheduler
        _make_job(status='running', owner='dead:1', lease_until=_ago(1))
        _make_job(status='done', http_status=200, result={}, finished_at=_ago(7200))
        with self.assertLogs('stocks.ai.jobs', level='WARNING'):
            scheduler._cleanup_ai_jobs()
        from stocks.models import AiJob
        self.assertEqual(list(AiJob.objects.values_list('status', flat=True)), ['failed'])
        failed = scheduler._run_steps('t', [('AI 后台任务清理', scheduler._cleanup_ai_jobs)])
        self.assertEqual(failed, [])

    def test_provider_removed_while_queued(self):
        from stocks.models import AiCallLog
        r = self.client.post(f'/api/stocks/{self.stock.id}/ai-analysis/?async=1')
        self.provider.is_enabled = False
        self.provider.save()
        self.jobs.run_pending()
        poll = self.client.get(f"/api/ai/jobs/{r.data['job_id']}/").json()
        self.assertEqual((poll['status'], poll['http_status']), ('failed', 400))
        self.assertFalse(AiCallLog.objects.exists())
        self.post.assert_not_called()


class AiJobMultiWorkerTests(TransactionTestCase):
    """多个执行者并发抢同一批任务：每个任务只执行一次。"""

    def test_no_duplicate_execution(self):
        import threading
        from django.db import close_old_connections
        from django.test import override_settings
        from stocks.ai import jobs
        from unittest import mock

        calls = []
        lock = threading.Lock()

        def handler(params):
            with lock:
                calls.append(params['n'])
            return 200, {}

        jobs._handlers['p3_count'] = handler
        self.addCleanup(jobs._handlers.pop, 'p3_count', None)
        with mock.patch.object(jobs, 'ensure_worker'):
            ids = [jobs.submit('p3_count', {'n': i}) for i in range(6)]
        errors = []

        def worker():
            try:
                for _ in range(20):
                    try:
                        jobs.run_pending()
                    except Exception as exc:  # SQLite 写锁竞争：重试，与线程里的循环一致
                        if 'locked' not in str(exc):
                            raise
                    if len(calls) >= 6:
                        break
            except Exception as exc:  # pragma: no cover - 失败时给出原因
                errors.append(exc)
            finally:
                close_old_connections()

        with override_settings(STOCKTRACE_AI_MAX_CONCURRENCY=6):
            threads = [threading.Thread(target=worker) for _ in range(4)]
            for t in threads:
                t.start()
            for t in threads:
                t.join(30)
        self.assertEqual(errors, [])
        self.assertEqual(sorted(calls), list(range(6)))
        self.assertEqual({jobs.get(i)['status'] for i in ids}, {'done'})


class ScreenerAiAsyncTests(TestCase):
    """AI 选股翻译、点评走同一后台任务机制（?async=1）；默认同步接口保留。"""

    def setUp(self):
        from unittest import mock
        from stocks.ai import jobs
        from stocks.tests_ai import _no_valuation
        self.jobs = jobs
        for patcher in (mock.patch.object(jobs, 'ensure_worker'),
                        mock.patch('stocks.ai.valuation.fetch_valuation_map', new=_no_valuation)):
            patcher.start()
            self.addCleanup(patcher.stop)
        self.provider, self.stock = _ai_fixtures()
        self.post = _ai_patches(self)
        self.client = APIClient()

    def _spec_reply(self, spec):
        import json
        from stocks.tests_p1_ai import _ok
        return _ok(json.dumps(spec))

    def _poll(self, job_id):
        return self.client.get(f'/api/ai/jobs/{job_id}/').json()

    def test_translate_async_matches_sync(self):
        from stocks.models import AiCallLog
        spec = {'logic': 'all', 'conditions': [{'field': 'change_pct', 'op': 'gt', 'value': 3}]}
        self.post.return_value = self._spec_reply(spec)
        sync = self.client.post('/api/screener/ai/', {'query': '涨3个点以上'}, format='json')
        self.assertEqual(sync.status_code, 200, sync.content)
        r = self.client.post('/api/screener/ai/?async=1', {'query': '涨3个点以上'}, format='json')
        self.assertEqual(r.status_code, 202)
        self.assertEqual(r.data['status'], 'queued')
        self.assertEqual(r.data['poll_url'], f"/api/ai/jobs/{r.data['job_id']}/")
        self.assertEqual(self.post.call_count, 1)
        self.jobs.run_pending()
        poll = self._poll(r.data['job_id'])
        self.assertEqual((poll['kind'], poll['status'], poll['http_status']),
                         ('screener_ai', 'done', 200))
        # 轮询结果与同步接口的 JSON 完全一致
        self.assertEqual(poll['result'], sync.json())
        self.assertEqual(AiCallLog.objects.filter(
            purpose='screener_translate', status='success').count(), 2)

    def test_translate_async_bad_conditions(self):
        bad = {'logic': 'all', 'conditions': [{'field': '市盈率', 'op': 'gt', 'value': 10}]}
        self.post.return_value = self._spec_reply(bad)
        r = self.client.post('/api/screener/ai/', {'query': '低估值', 'async': True}, format='json')
        self.assertEqual(r.status_code, 202)
        self.jobs.run_pending()
        poll = self._poll(r.data['job_id'])
        self.assertEqual((poll['status'], poll['http_status']), ('failed', 400))
        self.assertIn('无法执行', poll['result']['detail'])
        self.assertEqual(poll['result']['conditions'], bad)

    def test_comment_async(self):
        from stocks.models import AiCallLog
        from stocks.tests_p1_ai import _ok
        self.post.return_value = _ok('整体偏强')
        r = self.client.post('/api/screener/ai/comment/?async=1',
                             {'query': '强势股', 'results': [{'code': '000001'}]}, format='json')
        self.assertEqual(r.status_code, 202)
        self.post.assert_not_called()
        self.jobs.run_pending()
        poll = self._poll(r.data['job_id'])
        self.assertEqual((poll['kind'], poll['status']), ('screener_comment', 'done'))
        self.assertEqual(poll['result']['comment'], '整体偏强')
        self.assertTrue(poll['result']['disclaimer'])
        log = AiCallLog.objects.get()
        self.assertEqual((log.purpose, log.status), ('screener_comment', 'success'))

    def test_upstream_failure_in_job(self):
        from stocks.tests_ai import _fake_response
        self.post.return_value = _fake_response(401, text='secret-body')
        r = self.client.post('/api/screener/ai/comment/?async=1',
                             {'results': [{'code': '000001'}]}, format='json')
        self.jobs.run_pending()
        poll = self._poll(r.data['job_id'])
        self.assertEqual((poll['status'], poll['http_status']), ('failed', 502))
        self.assertEqual(poll['result']['error'], 'auth')
        self.assertNotIn('secret-body', str(poll))

    def test_validation_provider_and_quota_stay_synchronous(self):
        from django.test import override_settings
        from stocks.models import AiJob
        r = self.client.post('/api/screener/ai/?async=1', {'query': ''}, format='json')
        self.assertEqual(r.status_code, 400)
        r = self.client.post('/api/screener/ai/comment/?async=1',
                             {'results': [{'code': '999999'}]}, format='json')
        self.assertEqual(r.status_code, 400)
        with override_settings(STOCKTRACE_AI_DAILY_LIMIT=0):
            r = self.client.post('/api/screener/ai/?async=1', {'query': '涨'}, format='json')
        self.assertEqual(r.status_code, 429)
        self.provider.delete()
        r = self.client.post('/api/screener/ai/?async=1', {'query': '涨'}, format='json')
        self.assertEqual(r.status_code, 400)
        self.assertFalse(AiJob.objects.exists())
