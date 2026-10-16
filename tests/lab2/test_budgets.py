from dataclasses import replace
import json
from pathlib import Path
import tempfile
import unittest

import httpx
from lab_config import LabConfig
from lab_storage import LabDatabase
from litellm_admin import LiteLLMAdmin, BudgetUnavailable
from research_ai import ResearchAI


class Proxy:
    def __init__(self):
        self.keys = {}
        self.users = {}
        self.calls = []
        self.key_updates = 0
        self.fail_after_update = False

    def __call__(self,r):
        body = json.loads(r.content) if r.content else {}
        self.calls.append((r.url.path,body,r.headers.get('Authorization')))
        if r.url.path == '/key/info':
            key = r.url.params['key']
            return httpx.Response(200,json={'info':self.keys[key]}) if key in self.keys else httpx.Response(404)
        if r.url.path == '/user/new':
            self.users[body['user_id']] = body
            return httpx.Response(200,json=body)
        if r.url.path == '/user/info': return httpx.Response(200,json={})
        if r.url.path == '/key/generate':
            self.keys[body['key']] = dict(body,spend=0)
            return httpx.Response(200,json={'key':body['key']})
        if r.url.path == '/user/update':
            self.users[body['user_id']].update(body)
            return httpx.Response(200,json=body)
        if r.url.path == '/key/update':
            self.key_updates += 1
            self.keys[body['key']].update(body)
            if self.fail_after_update:
                self.fail_after_update = False
                raise httpx.ReadTimeout('unknown result')
            return httpx.Response(200,json={})
        raise AssertionError(r.url.path)


class BudgetTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        import asyncio
        asyncio.get_running_loop().slow_callback_duration = 5
        self.temp = tempfile.TemporaryDirectory()
        self.db = LabDatabase(Path(self.temp.name)/'bot.db')
        self.db.initialize()
        self.db.register(100)
        self.config = LabConfig(master_key='sk-test-management',admin_telegram_ids=frozenset({1}),ai_enabled=True)
        self.proxy = Proxy()
        self.management = LiteLLMAdmin(self.db,self.config,httpx.MockTransport(self.proxy))

    async def asyncTearDown(self):
        await self.management.close()
        self.temp.cleanup()

    async def test_one_persistent_virtual_key_no_reset_and_restricted_models(self):
        first = await self.management.ensure(100)
        await self.management.ensure(100)
        other = LiteLLMAdmin(self.db,self.config,httpx.MockTransport(self.proxy))
        try: self.assertEqual(await other.ensure(100),first)
        finally: await other.close()
        self.assertEqual(sum(p=='/key/generate' for p,_,_ in self.proxy.calls),1)
        limits = await self.management.get_limits(100)
        self.assertEqual(limits['allocated'],0.5)
        self.assertEqual(self.proxy.keys[first[1]]['models'],['research-basic'])
        self.assertNotIn('budget_duration',self.proxy.keys[first[1]])

    async def test_two_step_credit_keeps_spend_and_duplicate_confirmation_is_noop(self):
        _,key = await self.management.ensure(100)
        self.proxy.keys[key]['spend'] = 0.2
        op = await self.management.prepare_increase(1,100)
        self.assertEqual((await self.management.get_limits(100))['allocated'],0.5)
        await self.management.confirm_increase(1,op['id'])
        await self.management.confirm_increase(1,op['id'])
        limits = await self.management.get_limits(100)
        self.assertEqual((limits['allocated'],limits['spent']),(1.0,0.2))
        self.assertEqual(self.proxy.key_updates,1)

    async def test_unknown_network_outcome_is_reconciled_before_retry(self):
        op = await self.management.prepare_increase(1,100)
        self.proxy.fail_after_update = True
        with self.assertRaises(BudgetUnavailable): await self.management.confirm_increase(1,op['id'])
        self.assertEqual(self.management.operation(op['id'])['status'],'unknown')
        final = await self.management.confirm_increase(1,op['id'])
        self.assertEqual(final['status'],'done')
        self.assertEqual(self.proxy.key_updates,1)

    async def test_concurrent_confirmations_and_increases_serialized(self):
        import asyncio
        op = await self.management.prepare_increase(1,100)
        with self.assertRaises(ValueError): await self.management.prepare_increase(1,100)
        await asyncio.gather(*(self.management.confirm_increase(1,op['id']) for _ in range(4)))
        self.assertEqual(self.proxy.key_updates,1)

    async def test_admin_allowlist_checked_in_service(self):
        with self.assertRaises(ValueError): await self.management.prepare_increase(99,100)
        op = await self.management.prepare_increase(1,100)
        with self.assertRaises(ValueError): await self.management.confirm_increase(99,op['id'])

    async def test_changed_budget_not_blindly_incremented(self):
        op = await self.management.prepare_increase(1,100)
        _,key = await self.management.ensure(100)
        self.proxy.keys[key]['max_budget'] = 2
        with self.assertRaises(BudgetUnavailable): await self.management.confirm_increase(1,op['id'])
        self.assertEqual(self.proxy.key_updates,0)

    async def test_limits_unavailable_never_displays_cached_balance(self):
        await self.management.ensure(100)
        await self.management.client.aclose()
        self.management.client = httpx.AsyncClient(base_url=self.config.litellm_url,transport=httpx.MockTransport(lambda r:httpx.Response(503)))
        with self.assertRaisesRegex(BudgetUnavailable,'остаток бюджета неизвестен'): await self.management.get_limits(100)

    async def test_exhausted_budget_prevents_any_llm_http_request(self):
        _,key = await self.management.ensure(100)
        self.proxy.keys[key]['spend'] = 0.5
        calls = []
        ai = ResearchAI(self.db,self.config,self.management,httpx.MockTransport(lambda r:calls.append(r)))
        try:
            with self.assertRaisesRegex(BudgetUnavailable,'исчерпан'): await ai.generate(100,'test','id')
            self.assertEqual(calls,[])
        finally: await ai.close()

    async def test_llm_uses_personal_key_responses_and_cached_result(self):
        calls = []
        def handler(r):
            calls.append(r)
            return httpx.Response(200,json={'output':[{'content':[{'type':'output_text','text':'ok'}]}]})
        ai = ResearchAI(self.db,self.config,self.management,httpx.MockTransport(handler))
        try:
            self.assertEqual(await ai.generate(100,'test','id'),'ok')
            self.assertEqual(await ai.generate(100,'test','id'),'ok')
            self.assertEqual(len(calls),1)
            self.assertEqual(calls[0].url.path,'/v1/responses')
            self.assertNotIn(self.config.master_key,calls[0].headers['Authorization'])
            self.assertNotIn('tools',json.loads(calls[0].content))
        finally: await ai.close()

    async def test_failed_paid_call_is_not_repeated(self):
        calls = []
        def handler(r):
            calls.append(r)
            raise httpx.ReadTimeout('uncertain')
        ai = ResearchAI(self.db,self.config,self.management,httpx.MockTransport(handler))
        try:
            for _ in range(2):
                with self.assertRaises(BudgetUnavailable): await ai.generate(100,'test','id')
            self.assertEqual(len(calls),1)
        finally: await ai.close()

    async def test_definite_401_can_be_retried_by_new_action_after_key_repair(self):
        calls = []
        def handler(r):
            calls.append(r)
            if len(calls)==1: return httpx.Response(401,json={'error':{'type':'authentication_error'}})
            return httpx.Response(200,json={'output':[{'content':[{'type':'output_text','text':'OK'}]}]})
        ai = ResearchAI(self.db,self.config,self.management,httpx.MockTransport(handler))
        try:
            with self.assertRaisesRegex(BudgetUnavailable,'HTTP 401'): await ai.generate(100,'test','id')
            self.assertEqual(len(calls),1)
            self.assertEqual(await ai.generate(100,'test','id'),'OK')
            self.assertEqual(len(calls),2)
            self.assertEqual(await ai.generate(100,'test','id'),'OK')
            self.assertEqual(len(calls),2)
        finally: await ai.close()

    async def test_admin_analysis_separate_key_and_redacted_data(self):
        calls = []
        def handler(r):
            calls.append(r)
            return httpx.Response(200,json={'output':[{'content':[{'type':'output_text','text':'analysis'}]}]})
        ai = ResearchAI(self.db,self.config,self.management,httpx.MockTransport(handler))
        try:
            await ai.analyze(1,[{'rating':2,'comment':'mail user@example.org phone +79991234567 @handle','user_id':9999}],'feedback')
            body = json.loads(calls[0].content)
            self.assertNotIn('9999',body['input'])
            self.assertNotIn('user@example.org',body['input'])
            self.assertNotIn('79991234567',body['input'])
            with self.db.connect() as c: self.assertIsNotNone(c.execute("SELECT 1 FROM llm_keys WHERE user_id='feedback-analysis'").fetchone())
            with self.assertRaises(ValueError): await ai.analyze(99,[],'other')
        finally: await ai.close()

    async def test_rejected_request_cannot_be_credited_with_old_confirmation(self):
        rid = self.db.request_limit(100,0.5,'test')
        op = await self.management.prepare_increase(1,100,0.5,rid)
        with self.db.connect() as c: c.execute("UPDATE limit_requests SET status='rejected' WHERE id=?",(rid,))
        with self.assertRaises(ValueError): await self.management.confirm_increase(1,op['id'])
        self.assertEqual(self.proxy.key_updates,0)
