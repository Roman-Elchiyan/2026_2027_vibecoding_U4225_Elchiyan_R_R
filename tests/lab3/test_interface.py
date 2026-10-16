from dataclasses import replace
import json
from pathlib import Path
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock, patch

from telegram import Update
from telegram.ext import Application
from bot import build_application
from config import ROOT
from lab_config import LabConfig
from lab_handlers import LabController
from lab_storage import LabDatabase
from research import ResearchFilters
from search import Material, SearchResult
from sources import CsvSource
from tests.lab1.test_handlers import OfflineTelegram


class InterfaceTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        import asyncio
        asyncio.get_running_loop().slow_callback_duration = 5
        self.temp = tempfile.TemporaryDirectory()
        self.db = LabDatabase(Path(self.temp.name)/'bot.db')
        self.db.initialize()
        self.config = LabConfig(bot_token='123456:'+'x'*30,db_path=self.db.path,
            admin_telegram_ids=frozenset({100}),semantic_enabled=False,translation_enabled=False)
        self.controller = LabController(self.config,self.db,CsvSource(ROOT/'data/ploswater.csv'))
        self.transport = OfflineTelegram()
        builder = Application.builder().request(self.transport).get_updates_request(self.transport)
        with patch('bot.Application.builder',return_value=builder): self.app = build_application(self.config,self.controller)
        await self.app.initialize()
        self.serial = 0
        self.errors = []
        async def capture(update,context): self.errors.append(type(context.error).__name__)
        self.app.add_error_handler(capture)
        self.item = Material('test:1','journal-article','Water supply',year=2024,doi='10.1234/water',source='Crossref',
            authors=('Detailed Author',),abstract_or_snippet='Long abstract '*200,metadata={'language':'en','category':'scientific'})

    async def asyncTearDown(self):
        await self.app.shutdown()
        await self.controller.close()
        self.temp.cleanup()
        self.assertEqual(self.errors,[])

    async def send(self,text=None,callback=None,user_id=100,update_id=None):
        self.serial += 1
        user = {'id':user_id,'is_bot':False,'first_name':'Test'}
        message = {'message_id':self.serial,'date':1,'from':user,'chat':{'id':user_id,'type':'private'},'text':text or 'Card'}
        if text and text.startswith('/'): message['entities'] = [{'type':'bot_command','offset':0,'length':len(text.split()[0])}]
        data = {'update_id':update_id if update_id is not None else self.serial}
        if callback: data['callback_query'] = {'id':str(self.serial),'from':user,'chat_instance':'1','data':callback,'message':message}
        else: data['message'] = message
        await self.app.process_update(Update.de_json(data,self.app.bot))

    def output(self):
        return [p.get('text','') for action,p in self.transport.calls if action in ('sendMessage','editMessageText')]

    def last_markup(self):
        for action,p in reversed(self.transport.calls):
            if action in ('sendMessage','editMessageText') and p.get('reply_markup'): return p['reply_markup']
        return {}

    def snapshot(self,uid=100,filters=None):
        self.db.register(uid)
        result = SearchResult([self.item],[],'a'*32,1)
        self.db.save_search(uid,filters or ResearchFilters(topic='water',sources=('crossref',)),result)
        return result.search_id

    async def test_compact_cards_keep_doi_and_move_abstract_authors_into_detail(self):
        sid = self.snapshot()
        await self.send(callback=f'p:{sid}:0')
        card = self.output()[-1]
        self.assertIn(self.item.doi,card)
        self.assertNotIn('Long abstract',card)
        self.assertNotIn('Detailed Author',card)
        self.assertIn('Фильтры:',card)
        await self.send(callback=f'd:{sid}:0:0')
        self.assertIn('Long abstract',self.output()[-1])
        self.assertIn('Detailed Author',self.output()[-1])
        self.assertLess(len(self.output()[-1]),4096)

    async def test_reset_actually_changes_next_search_and_retry_never_pays_ai(self):
        sid = self.snapshot(filters=ResearchFilters(topic='water',sources=('crossref',),year_from=2024,year_to=2024,access_status='open',ai_enabled=True))
        seen = []
        async def research(f):
            seen.append(f)
            from uuid import uuid4
            return SearchResult([self.item],[],uuid4().hex,1)
        self.controller.research.search = research
        self.controller.ai_service.plan = AsyncMock(side_effect=AssertionError('No paid retry'))
        await self.send(callback=f'retry:{sid}')
        self.assertEqual(seen[-1].year_from,2024)
        await self.send(callback=f'reset:{sid}')
        self.assertIsNone(seen[-1].year_from)
        self.assertEqual(seen[-1].access_status,'any')
        self.assertEqual(seen[-1].topic,'water')
        self.controller.ai_service.plan.assert_not_awaited()

    async def test_outage_keeps_results_warns_and_exposes_retry(self):
        result = SearchResult([self.item],['OpenAlex временно недоступен. Результаты остальных источников сохранены.'],'b'*32,1)
        self.db.register(100)
        self.db.save_search(100,ResearchFilters(topic='water'),result)
        await self.send(callback='p:'+result.search_id+':0')
        self.assertIn('OpenAlex временно недоступен',self.output()[-1])
        self.assertIn(self.item.title,self.output()[-1])
        self.assertIn('retry:',json.dumps(self.last_markup(),ensure_ascii=False))

    async def test_admin_command_and_forged_callback_denied(self):
        await self.send('/admin',user_id=200)
        self.assertIn('Доступ запрещён',self.output()[-1])
        await self.send(callback='admin:pause:1',user_id=200)
        self.assertIn('Доступ запрещён',self.output()[-1])
        self.assertFalse(self.db.get_app_setting('search_paused',False))

    async def test_pause_blocks_search_but_admin_remains_accessible(self):
        await self.send(callback='admin:pause:1')
        self.db.register(200)
        self.db.set_settings(200,ResearchFilters(topic='water'),'form')
        self.controller.research.search = AsyncMock()
        await self.send(callback='lab:run',user_id=200)
        self.assertIn('приостановлены',self.output()[-1])
        self.controller.research.search.assert_not_awaited()
        await self.send('/admin')
        self.assertIn('Администратор',self.output()[-1])

    async def test_ban_and_ai_permission_persist(self):
        self.db.register(200)
        await self.send(callback='admin:access:200:banned:1')
        await self.send('/search',user_id=200)
        self.assertIn('Доступ отключён',self.output()[-1])
        await self.send(callback='admin:access:200:ai_allowed:0')
        reopened = LabDatabase(self.db.path)
        self.assertEqual(reopened.access(200),(True,False))

    async def test_rating_entire_search_publication_comment_and_history(self):
        sid = self.snapshot()
        await self.send(callback=f'rv:{sid}:-1:4')
        await self.send(callback=f'rv:{sid}:0:2')
        reviews = self.db.feedback_list()
        self.assertEqual(len(reviews),2)
        review = next(r for r in reviews if r['material_id'])
        await self.send(callback='rvc:'+review['id'])
        await self.send('Карточка слишком длинная')
        self.assertEqual(self.db.feedback_get(review['id'])['comment'],'Карточка слишком длинная')
        await self.send(callback=f'rv:{sid}:0:5')
        with self.db.connect() as c:
            self.assertEqual(c.execute('SELECT COUNT(*) FROM feedback_history WHERE feedback_id=?',(review['id'],)).fetchone()[0],3)

    async def test_foreign_and_expired_feedback_links_rejected(self):
        sid = self.snapshot()
        await self.send(callback=f'rv:{sid}:-1:4',user_id=200)
        self.assertIn('недоступна',self.output()[-1])
        with self.db.connect() as c: c.execute("UPDATE searches SET created_at=datetime('now','-31 days') WHERE id=?",(sid,))
        await self.send(callback=f'rv:{sid}:-1:4')
        self.assertIn('истёк',self.output()[-1])
        self.assertEqual(self.db.feedback_list(),[])

    async def test_reply_goes_only_to_author_and_marks_handled(self):
        sid = self.snapshot(uid=200)
        fid = self.db.feedback_save(200,sid,None,3,'Слишком длинно')
        await self.send(callback='admin:reply:'+fid)
        await self.send('Добавлены компактные карточки')
        replies = [p for action,p in self.transport.calls if action=='sendMessage' and p.get('text','').startswith('Ответ администратора')]
        self.assertEqual(len(replies),1)
        self.assertEqual(replies[0]['chat_id'],200)
        self.assertTrue(self.db.feedback_get(fid)['handled'])

    async def test_limit_request_and_no_duplicate_pending_request(self):
        await self.send(callback='lab:request',user_id=200)
        await self.send('0.50 Для обзора',user_id=200)
        with self.db.connect() as c: row = c.execute('SELECT * FROM limit_requests').fetchone()
        self.assertEqual((row['amount'],row['comment']),(0.5,'Для обзора'))
        await self.send(callback='lab:request',user_id=200)
        await self.send('0.50 Ещё',user_id=200)
        self.assertIn('ожидает',self.output()[-1])

    async def test_duplicate_telegram_update_does_not_repeat_rating(self):
        sid = self.snapshot()
        for _ in range(2): await self.send(callback=f'rv:{sid}:-1:5',update_id=12345)
        with self.db.connect() as c: self.assertEqual(c.execute('SELECT COUNT(*) FROM feedback_history').fetchone()[0],1)

    async def test_callback_lengths_and_html_bounds(self):
        self.item = replace(self.item,title='💎<&'*4000,doi='10.1234/'+'x'*500,abstract_or_snippet='&'*5000)
        sid = self.snapshot()
        for callback in (f'p:{sid}:0',f'd:{sid}:0:0',f'rvrate:{sid}:0','/admin'):
            if callback.startswith('/'): await self.send(callback)
            else: await self.send(callback=callback)
        for action,p in self.transport.calls:
            if action in ('sendMessage','editMessageText'):
                self.assertLessEqual(len(p.get('text','').encode('utf-16-le'))//2,4096)
                raw = p.get('reply_markup')
                if raw:
                    markup = json.loads(raw) if isinstance(raw,str) else raw
                    for row in markup.get('inline_keyboard',[]):
                        for button in row:
                            if button.get('callback_data'): self.assertLessEqual(len(button['callback_data'].encode()),64)

    async def test_english_interface_keeps_original_title_author_and_doi(self):
        await self.send('/start')
        await self.send(callback='set:ui:en')
        self.assertIn('Query:',self.output()[-1])
        self.assertIn('Filters:',self.output()[-1])
        await self.send('/help')
        self.assertIn('Argos translates',self.output()[-1])
        sid = self.snapshot()
        f,_ = self.controller.filters(100)
        f.ui_language = 'en'
        self.db.set_settings(100,f)
        await self.send(callback=f'p:{sid}:0')
        self.assertIn('Results:',self.output()[-1])
        self.assertIn(self.item.title,self.output()[-1])
        self.assertIn(self.item.doi,self.output()[-1])
        await self.send(callback=f'd:{sid}:0:0')
        self.assertIn('Authors: Detailed Author',self.output()[-1])

    async def test_same_paid_search_plan_uses_same_identity(self):
        self.db.register(100)
        f = ResearchFilters(topic='water supply',sources=('crossref',),ai_enabled=True)
        self.controller.ai_service.plan = AsyncMock(return_value={'query':'water supply','sources':['crossref']})
        async def research(filters):
            from uuid import uuid4
            return SearchResult([self.item],[],uuid4().hex,1)
        self.controller.research.search = research
        await self.controller.run_search(SimpleNamespace(reply_text=AsyncMock()),100,f)
        await self.controller.run_search(SimpleNamespace(reply_text=AsyncMock()),100,f)
        calls = self.controller.ai_service.plan.await_args_list
        self.assertEqual(calls[0].args[2],calls[1].args[2])

    async def test_five_extreme_translated_cards_fit_telegram_limit(self):
        self.db.register(100)
        items = [replace(self.item,id=f'test:{i}',title='💎&'*2000,source='&'*2000,doi=f'10.1234/{i}'+('x'*1000)) for i in range(5)]
        f = ResearchFilters(topic='x'*500,translate_cards=True)
        self.db.save_search(100,f,SearchResult(items,['warning '*1000],'c'*32,5))
        self.controller.translator.translate = AsyncMock(return_value=('Перевод '*1000,''))
        await self.send(callback='p:'+('c'*32)+':0')
        self.assertLessEqual(len(self.output()[-1].encode('utf-16-le'))//2,4096)
