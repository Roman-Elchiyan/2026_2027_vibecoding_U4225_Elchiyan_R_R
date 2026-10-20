from dataclasses import replace
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import AsyncMock

import httpx

from db import Database
from lab_config import LabConfig
from lab_storage import LabDatabase
from research import ResearchFilters
from research_ai import ResearchAI
from search import Material, SearchFilters, SearchResult
import tests.lab3.test_interface as original_ui
from tests.lab2.test_budgets import Proxy
from litellm_admin import LiteLLMAdmin


class FeedbackStorageTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.db = LabDatabase(Path(self.temp.name) / 'bot.db')
        self.db.initialize()
        self.db.register(100)
        self.item = Material('fixture:one', None, 'Water', doi='10.1234/water')
        self.sid = 'a' * 32
        self.db.save_search(100, SearchFilters(topic='water'), SearchResult([self.item], [], self.sid, 1))

    def tearDown(self):
        self.temp.cleanup()

    def test_general_unrated_feedback_needs_no_search(self):
        fid = self.db.feedback_submit(100, ('bot',), 'Удобная навигация')
        row = self.db.feedback_get(fid)
        self.assertIsNone(row['search_id'])
        self.assertIsNone(row['rating'])
        self.assertEqual(row['categories'], ['bot'])

    def test_one_two_three_categories_are_one_record_each(self):
        for categories in (('bot',), ('bot','results'), ('bot','results','publication')):
            fid = self.db.feedback_submit(100, categories, 'Один комментарий', 4, self.sid, (0,))
            row = self.db.feedback_get(fid)
            self.assertEqual(row['categories'], list(categories))
            self.assertEqual(row['comment'], 'Один комментарий')
        self.assertEqual(len(self.db.feedback_list()), 3)

    def test_edits_keep_id_reply_and_full_history(self):
        fid = self.db.feedback_submit(100, ('bot',), 'Первый текст')
        with self.db.connect() as c:
            c.execute("UPDATE feedback SET reply='Ответ',handled=1 WHERE id=?", (fid,))
        same = self.db.feedback_submit(100, ('bot','results'), 'Второй текст', 5, self.sid, (), fid)
        self.assertEqual(same, fid)
        row = self.db.feedback_get(fid)
        self.assertEqual(row['reply'], 'Ответ')
        self.assertFalse(row['handled'])
        with self.db.connect() as c:
            history = c.execute('SELECT * FROM feedback_history WHERE feedback_id=? ORDER BY id', (fid,)).fetchall()
        self.assertEqual(len(history), 2)
        self.assertIsNone(history[0]['rating'])
        self.assertEqual(json.loads(history[1]['categories_json']), ['bot','results'])

    def test_foreign_record_or_foreign_search_cannot_be_changed(self):
        fid = self.db.feedback_submit(100, ('bot',), 'Исходный')
        self.db.register(200)
        with self.assertRaises(ValueError): self.db.feedback_submit(200, ('bot',), 'Чужое изменение', feedback_id=fid)
        with self.assertRaises(LookupError): self.db.feedback_submit(200, ('results',), 'Чужая выдача', search_id=self.sid)
        self.assertEqual(self.db.feedback_get(fid)['comment'], 'Исходный')

    def test_publication_requires_selection_and_empty_feedback_is_rejected(self):
        with self.assertRaises(ValueError): self.db.feedback_submit(100, ('publication',), 'Комментарий', search_id=self.sid)
        with self.assertRaises(ValueError): self.db.feedback_submit(100, ('bot',), '')
        with self.assertRaises(ValueError): self.db.feedback_submit(100, (), 'Комментарий')
        with self.assertRaises(ValueError): self.db.feedback_submit(100, ('bot',), 'Комментарий', True)

    def test_existing_review_can_be_edited_after_search_history_cleanup(self):
        fid = self.db.feedback_submit(100, ('results','publication'), 'Первый', 3, self.sid, (0,))
        with self.db.connect() as c: c.execute('DELETE FROM searches WHERE id=?', (self.sid,))
        self.db.feedback_submit(100, ('results','publication'), 'После очистки', 4, self.sid, None, fid)
        self.assertEqual(self.db.feedback_get(fid)['comment'], 'После очистки')

    def test_old_buttons_do_not_replace_multi_category_review(self):
        fid = self.db.feedback_submit(100, ('bot','results','publication'), 'Общий', 4, self.sid, (0,))
        self.db.feedback_save(100, self.sid, 0, 2)
        self.assertEqual(self.db.feedback_get(fid)['categories'], ['bot','results','publication'])
        self.assertEqual(self.db.feedback_get(fid)['rating'], 4)

    def test_v3_migration_preserves_data_history_and_old_entry_point(self):
        path = Path(self.temp.name) / 'v3.db'
        base = Database(path)
        base.initialize()
        base.register(100)
        base.save_search(100, SearchFilters(topic='water'), SearchResult([self.item], [], self.sid, 1))
        base.set_favorite(100, self.sid, 0, True)
        with base.connect() as c:
            c.execute("INSERT INTO app_settings VALUES ('lab_schema','3')")
            c.execute('''CREATE TABLE feedback (id TEXT PRIMARY KEY,user_id INTEGER NOT NULL,search_id TEXT NOT NULL,
                material_id TEXT REFERENCES materials(id),rating INTEGER NOT NULL CHECK(rating BETWEEN 1 AND 5),
                comment TEXT NOT NULL DEFAULT '',handled INTEGER NOT NULL DEFAULT 0,reply TEXT,
                created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP)''')
            c.execute('''CREATE TABLE feedback_history (id INTEGER PRIMARY KEY,feedback_id TEXT NOT NULL REFERENCES feedback(id),
                rating INTEGER NOT NULL,comment TEXT NOT NULL,created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP)''')
            c.execute("INSERT INTO feedback(id,user_id,search_id,material_id,rating,comment,handled,reply) VALUES ('old',100,?,?,4,'Старый',1,'Ответ')", (self.sid,self.item.id))
            c.execute("INSERT INTO feedback_history VALUES (7,'old',4,'Старый','2026-10-16 12:00:00')")
        migrated = LabDatabase(path)
        for _ in range(2): migrated.initialize()
        row = migrated.feedback_get('old')
        self.assertEqual((row['rating'],row['comment'],row['reply'],row['handled']), (4,'Старый','Ответ',1))
        self.assertEqual(row['categories'], ['publication'])
        with migrated.connect() as c:
            self.assertEqual(c.execute('SELECT id FROM feedback_history').fetchone()[0], 7)
            self.assertEqual(c.execute('PRAGMA foreign_key_check').fetchall(), [])
            self.assertEqual(c.execute('PRAGMA user_version').fetchone()[0], 2)
        Database(path).initialize()
        self.assertTrue(migrated.is_favorite(100, self.item.id))
        self.assertEqual(migrated.result_item(100, self.sid, 0).title, self.item.title)


class FeedbackInterfaceTests(unittest.IsolatedAsyncioTestCase):
    asyncSetUp = original_ui.InterfaceTests.asyncSetUp
    asyncTearDown = original_ui.InterfaceTests.asyncTearDown
    send = original_ui.InterfaceTests.send
    output = original_ui.InterfaceTests.output
    last_markup = original_ui.InterfaceTests.last_markup
    snapshot = original_ui.InterfaceTests.snapshot

    def nonce(self):
        raw = self.last_markup()
        markup = json.loads(raw) if isinstance(raw,str) else raw
        return next(b['callback_data'].split(':')[2] for row in markup['inline_keyboard']
            for b in row if b.get('callback_data','').startswith('fb:cat:'))

    async def test_feedback_without_search_saves_only_after_submit_and_once(self):
        await self.send('/feedback')
        nonce = self.nonce()
        self.assertEqual(self.db.feedback_list(), [])
        await self.send(callback=f'fb:comment:{nonce}')
        await self.send('Общий отзыв о боте')
        await self.send(callback=f'fb:submit:{nonce}')
        await self.send(callback=f'fb:submit:{nonce}')
        rows = self.db.feedback_list()
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]['categories'], ['bot'])
        self.assertIsNone(rows[0]['rating'])
        self.assertIn('уже сохранён', self.output()[-1])

    async def test_all_categories_one_publication_one_comment_one_record(self):
        self.snapshot()
        await self.send('/feedback')
        nonce = self.nonce()
        for category in ('results','publication'):
            await self.send(callback=f'fb:cat:{nonce}:{category}')
        await self.send(callback=f'fb:pick:{nonce}:0')
        await self.send(callback=f'fb:item:{nonce}:0:0')
        await self.send(callback=f'fb:comment:{nonce}')
        await self.send('Один отзыв обо всём')
        await self.send(callback=f'fb:rate:{nonce}:4')
        await self.send(callback=f'fb:submit:{nonce}')
        rows = self.db.feedback_list()
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]['categories'], ['bot','results','publication'])
        self.assertEqual(len(rows[0]['material_ids']), 1)
        self.assertEqual(rows[0]['rating'], 4)

    async def test_foreign_draft_and_edit_callback_are_rejected(self):
        await self.send('/feedback')
        nonce = self.nonce()
        await self.send(callback=f'fb:comment:{nonce}', user_id=200)
        self.assertIn('недоступна', self.output()[-1])
        fid = self.db.feedback_submit(100, ('bot',), 'Отзыв')
        await self.send(callback='fb:edit:' + fid, user_id=200)
        self.assertIn('Чужой', self.output()[-1])

    async def test_ai_permission_alone_is_free_and_search_mode_is_explicit(self):
        self.db.register(100)
        self.controller.config = replace(self.config, ai_enabled=True)
        f = ResearchFilters(topic='water', sources=('crossref',), ai_enabled=True)
        self.db.set_settings(100, f, 'form')
        self.controller.research.search = AsyncMock(side_effect=lambda *a,**kw: SearchResult([self.item], [], __import__('uuid').uuid4().hex, 1))
        self.controller.ai_service.plan = AsyncMock(return_value={'queries':['water']})
        await self.send(callback='lab:run')
        self.controller.ai_service.plan.assert_not_awaited()
        await self.send(callback='lab:ai_search')
        self.assertTrue(self.controller.filters(100)[0].ai_search)
        await self.send(callback='lab:run')
        self.controller.ai_service.plan.assert_awaited_once()
        await self.send(callback='lab:ai')
        f = self.controller.filters(100)[0]
        self.assertFalse(f.ai_enabled)
        self.assertFalse(f.ai_search)

    async def test_admin_analysis_requires_separate_confirmation_and_includes_general_review(self):
        self.db.register(100)
        fid = self.db.feedback_submit(100, ('bot',), 'Явно тестовый отзыв')
        self.controller.ai_service.analyze = AsyncMock(return_value='Тестовый анализ')
        await self.send(callback='admin:select:' + fid)
        await self.send(callback='admin:analyze')
        self.controller.ai_service.analyze.assert_not_awaited()
        raw = self.last_markup()
        markup = json.loads(raw) if isinstance(raw,str) else raw
        callback = markup['inline_keyboard'][0][0]['callback_data']
        await self.send(callback=callback)
        await self.send(callback=callback)
        self.controller.ai_service.analyze.assert_awaited_once()
        self.assertEqual(self.controller.ai_service.analyze.call_args.args[1][0]['categories'], ['bot'])


class AnalysisAccountingTests(unittest.IsolatedAsyncioTestCase):
    async def test_mocked_paid_path_records_cost_usage_and_redacts_categories(self):
        with tempfile.TemporaryDirectory() as directory:
            db = LabDatabase(Path(directory) / 'test.db')
            db.initialize()
            config = LabConfig(admin_telegram_ids=frozenset({1}), ai_enabled=True, master_key='sk-test-only')
            management = LiteLLMAdmin(db, config, httpx.MockTransport(Proxy()))
            seen = []
            def reply(request):
                seen.append(json.loads(request.content))
                return httpx.Response(200, headers={'x-litellm-response-cost':'0.0001'}, json={
                    'id':'fixture-response','usage':{'input_tokens':10,'output_tokens':5},
                    'output':[{'content':[{'type':'output_text','text':'Тестовый анализ'}]}]})
            ai = ResearchAI(db, config, management, httpx.MockTransport(reply))
            try:
                await ai.analyze(1,[{'rating':None,'comment':'test user@example.org','categories':['bot','results'],'user_id':99999}], 'test-analysis')
                self.assertIn('"categories": ["bot", "results"]', seen[0]['input'])
                self.assertNotIn('user@example.org', seen[0]['input'])
                self.assertNotIn('99999', seen[0]['input'])
                with db.connect() as c: row = c.execute('SELECT * FROM ai_calls').fetchone()
                self.assertEqual(row['response_id'], 'fixture-response')
                self.assertEqual(row['cost_usd'], 0.0001)
                self.assertEqual(json.loads(row['usage_json'])['output_tokens'], 5)
                with self.assertRaises(ValueError): await ai.generate(999, 'test', 'bad', admin=True)
            finally:
                await ai.close()
                await management.close()
