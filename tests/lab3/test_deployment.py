import asyncio
from dataclasses import replace
import json
from pathlib import Path
import socket
import tempfile
import unittest
from unittest.mock import AsyncMock

import httpx
from telegram import Update
from telegram.ext import Application
from config import ConfigError
from deployment import polling_ready, single_instance, tunnel_url
from lab_config import LabConfig
from lab_storage import LabDatabase
from tests.lab1.test_handlers import OfflineTelegram


class WebhookTransport(OfflineTelegram):
    async def do_request(self,url,method,request_data=None,**kwargs):
        action = url.rsplit('/',1)[-1]
        if action in ('setWebhook','deleteWebhook'): return 200,json.dumps({'ok':True,'result':True}).encode()
        if action == 'getWebhookInfo': return 200,json.dumps({'ok':True,'result':{'url':'','has_custom_certificate':False,'pending_update_count':0}}).encode()
        return await super().do_request(url,method,request_data,**kwargs)


class DeploymentTests(unittest.IsolatedAsyncioTestCase):
    async def test_real_ptb_http_server_checks_secret_header_and_path(self):
        transport = WebhookTransport()
        app = Application.builder().token('123456:'+'x'*30).request(transport).get_updates_request(transport).build()
        with socket.socket() as s:
            s.bind(('127.0.0.1',0))
            port = s.getsockname()[1]
        await app.initialize()
        try:
            await app.updater.start_webhook(listen='127.0.0.1',port=port,url_path='private-test-path',
                webhook_url='https://example.org/private-test-path',secret_token='separate-secret',drop_pending_updates=False)
            async with httpx.AsyncClient(base_url=f'http://127.0.0.1:{port}') as client:
                body = {'update_id':98765,'message':{'message_id':1,'date':1,'chat':{'id':100,'type':'private'},'from':{'id':100,'is_bot':False,'first_name':'Test'},'text':'/start'}}
                self.assertEqual((await client.post('/private-test-path',json=body)).status_code,403)
                self.assertEqual((await client.post('/private-test-path',json=body,headers={'X-Telegram-Bot-Api-Secret-Token':'wrong'})).status_code,403)
                self.assertEqual((await client.post('/wrong-path',json=body)).status_code,404)
                self.assertEqual((await client.post('/private-test-path',json=body,headers={'X-Telegram-Bot-Api-Secret-Token':'separate-secret'})).status_code,200)
                update = await asyncio.wait_for(app.update_queue.get(),2)
                self.assertIsInstance(update,Update)
                self.assertEqual(update.update_id,98765)
                app.update_queue.task_done()
        finally:
            if app.updater.running: await app.updater.stop()
            await app.shutdown()

    async def test_polling_removes_webhook_without_dropping_updates_and_verifies(self):
        from types import SimpleNamespace
        bot = SimpleNamespace(delete_webhook=AsyncMock(),get_webhook_info=AsyncMock(return_value=SimpleNamespace(url='',pending_update_count=2)))
        await polling_ready(bot)
        bot.delete_webhook.assert_awaited_once_with(drop_pending_updates=False)
        bot.get_webhook_info.return_value.url = 'https://still-active'
        with self.assertRaises(ConfigError): await polling_ready(bot)

    async def test_tunnel_must_target_webhook_and_be_https(self):
        config = LabConfig(webhook_path='p'*32,webhook_secret='s'*32)
        def handler(request):
            if request.url.host=='127.0.0.1': return httpx.Response(200,json={'tunnels':[{'proto':'https','public_url':'https://test.ngrok-free.app','config':{'addr':'http://localhost:8080'}}]})
            return httpx.Response(502)
        self.assertEqual(await tunnel_url(config,httpx.MockTransport(handler)),'https://test.ngrok-free.app')
        with self.assertRaises(ConfigError): await tunnel_url(replace(config,webhook_port=4000),httpx.MockTransport(handler))
        with self.assertRaises(ConfigError): await tunnel_url(replace(config,webhook_host='0.0.0.0'),httpx.MockTransport(handler))

    async def test_missing_ngrok_causes_explicit_fallback(self):
        config = LabConfig(webhook_path='p'*32,webhook_secret='s'*32)
        def fail(request): raise httpx.ConnectError('no agent')
        with self.assertRaisesRegex(ConfigError,'polling'): await tunnel_url(config,httpx.MockTransport(fail))


class PersistenceTests(unittest.TestCase):
    def test_additive_migration_preserves_original_data_and_works_with_lab1(self):
        from db import Database
        from search import Material, SearchFilters, SearchResult
        with tempfile.TemporaryDirectory() as temp:
            path = Path(temp)/'bot.db'
            old = Database(path)
            old.initialize()
            old.register(100)
            old.save_search(100,SearchFilters(topic='water'),SearchResult([Material('x',None,'Water')],[],'a'*32,1))
            old.set_favorite(100,'a'*32,0,True)
            new = LabDatabase(path)
            new.initialize()
            self.assertEqual(new.counts(),old.counts())
            old.initialize()
            self.assertTrue(old.health())
            self.assertTrue(old.is_favorite(100,'x'))
            new.feedback_save(100,'a'*32,None,4,'test')
            with new.connect() as c: c.execute("UPDATE searches SET created_at=datetime('now','-31 days')")
            new.cleanup_history()
            self.assertEqual(len(new.feedback_list()),1)
            self.assertTrue(old.is_favorite(100,'x'))

    def test_only_one_bot_process_can_hold_local_lock(self):
        with tempfile.TemporaryDirectory() as temp:
            path = Path(temp)/'bot.lock'
            with single_instance(path):
                with self.assertRaises(ConfigError):
                    with single_instance(path): pass
            with single_instance(path): pass
