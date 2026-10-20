"""Compatible v4 migration; unified feedback and versioned search snapshots."""
import hashlib
import json
import re
import time
from uuid import uuid4

from db import Database


class LabDatabase(Database):
    # Lab 1 tables and user_version remain owned by Database. A separate version
    # allows its original tests and original entry point to open this same DB.
    def initialize(self):
        super().initialize()
        with self.connect() as c:
            c.execute("BEGIN IMMEDIATE")
            version = c.execute("SELECT value FROM app_settings WHERE key='lab_schema'").fetchone()
            if version and int(version[0]) > 4:
                raise RuntimeError("Схема Lab 2/3 новее приложения.")
            statements = [
                "CREATE TABLE IF NOT EXISTS cache (key TEXT PRIMARY KEY, value TEXT NOT NULL, expires REAL NOT NULL)",
                "CREATE TABLE IF NOT EXISTS access (user_id INTEGER PRIMARY KEY REFERENCES users(telegram_id), banned INTEGER NOT NULL DEFAULT 0, ai_allowed INTEGER NOT NULL DEFAULT 1)",
                "CREATE TABLE IF NOT EXISTS llm_keys (user_id TEXT PRIMARY KEY, secret TEXT NOT NULL, ready INTEGER NOT NULL DEFAULT 0)",
                """CREATE TABLE IF NOT EXISTS budget_ops (id TEXT PRIMARY KEY, user_id INTEGER NOT NULL,
                    admin_id INTEGER NOT NULL, before_usd REAL NOT NULL, after_usd REAL NOT NULL,
                    amount REAL NOT NULL, status TEXT NOT NULL DEFAULT 'pending', request_id TEXT,
                    error TEXT, created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP, updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP)""",
                """CREATE TABLE IF NOT EXISTS limit_requests (id TEXT PRIMARY KEY, user_id INTEGER NOT NULL,
                    amount REAL NOT NULL, comment TEXT NOT NULL, status TEXT NOT NULL DEFAULT 'pending',
                    response TEXT, created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP)""",
                """CREATE TABLE IF NOT EXISTS feedback (id TEXT PRIMARY KEY, user_id INTEGER NOT NULL,
                    search_id TEXT NOT NULL, material_id TEXT REFERENCES materials(id), rating INTEGER NOT NULL CHECK(rating BETWEEN 1 AND 5),
                    comment TEXT NOT NULL DEFAULT '', handled INTEGER NOT NULL DEFAULT 0,
                    reply TEXT, created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP, updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP)""",
                """CREATE TABLE IF NOT EXISTS feedback_history (id INTEGER PRIMARY KEY, feedback_id TEXT NOT NULL REFERENCES feedback(id),
                    rating INTEGER NOT NULL, comment TEXT NOT NULL, created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP)""",
                "CREATE TABLE IF NOT EXISTS audit (id INTEGER PRIMARY KEY, actor INTEGER, event TEXT NOT NULL, target TEXT, created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP)",
                "CREATE TABLE IF NOT EXISTS delivery (update_id INTEGER PRIMARY KEY, status TEXT NOT NULL, created_at REAL NOT NULL)",
                "CREATE TABLE IF NOT EXISTS ai_calls (id TEXT PRIMARY KEY, status TEXT NOT NULL, result TEXT, created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP)",
                """CREATE TABLE IF NOT EXISTS result_payloads (search_id TEXT NOT NULL REFERENCES searches(id) ON DELETE CASCADE,
                    position INTEGER NOT NULL, payload TEXT NOT NULL, PRIMARY KEY(search_id,position))""",
                """CREATE TABLE IF NOT EXISTS search_details (search_id TEXT PRIMARY KEY REFERENCES searches(id) ON DELETE CASCADE,
                    payload TEXT NOT NULL)""",
                """CREATE TABLE IF NOT EXISTS favorite_payloads (user_id INTEGER NOT NULL,material_id TEXT NOT NULL,
                    payload TEXT NOT NULL,PRIMARY KEY(user_id,material_id),
                    FOREIGN KEY(user_id,material_id) REFERENCES favorites(user_id,material_id) ON DELETE CASCADE)""",
            ]
            for sql in statements:
                c.execute(sql)
            if 'notified' not in {r[1] for r in c.execute('PRAGMA table_info(budget_ops)')}:
                c.execute('ALTER TABLE budget_ops ADD COLUMN notified INTEGER NOT NULL DEFAULT 0')
            if not version or int(version[0]) < 4:
                self._migrate_feedback_v4(c)
            if 'search_id' not in {r[1] for r in c.execute('PRAGMA table_info(feedback_history)')}:
                c.execute('ALTER TABLE feedback_history ADD COLUMN search_id TEXT')
            call_fields = {r[1] for r in c.execute('PRAGMA table_info(ai_calls)')}
            for name, sql_type in (('response_id','TEXT'), ('cost_usd','REAL'), ('usage_json','TEXT'), ('identity','TEXT')):
                if name not in call_fields:
                    c.execute(f'ALTER TABLE ai_calls ADD COLUMN {name} {sql_type}')
            c.execute("INSERT OR REPLACE INTO app_settings VALUES ('lab_schema','4')")

    @staticmethod
    def _migrate_feedback_v4(c):
        # Rebuild both FK-linked tables inside the existing transaction: the
        # v3 NOT NULL constraints cannot represent general/unrated feedback.
        c.execute('''CREATE TABLE feedback_v4 (
            id TEXT PRIMARY KEY, user_id INTEGER NOT NULL,
            search_id TEXT, material_id TEXT REFERENCES materials(id),
            rating INTEGER CHECK(rating IS NULL OR rating BETWEEN 1 AND 5),
            comment TEXT NOT NULL DEFAULT '', handled INTEGER NOT NULL DEFAULT 0,
            reply TEXT, created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
            updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
            categories_json TEXT NOT NULL, material_ids_json TEXT NOT NULL DEFAULT '[]')''')
        c.execute('''INSERT INTO feedback_v4 SELECT id,user_id,search_id,material_id,rating,comment,
            handled,reply,created_at,updated_at,
            CASE WHEN material_id IS NULL THEN '["results"]' ELSE '["publication"]' END,
            CASE WHEN material_id IS NULL THEN '[]' ELSE json_array(material_id) END FROM feedback''')
        c.execute('''CREATE TABLE feedback_history_v4 (
            id INTEGER PRIMARY KEY, feedback_id TEXT NOT NULL REFERENCES feedback_v4(id),
            rating INTEGER, comment TEXT NOT NULL, created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
            categories_json TEXT NOT NULL, material_ids_json TEXT NOT NULL DEFAULT '[]', search_id TEXT)''')
        c.execute('''INSERT INTO feedback_history_v4 SELECT h.id,h.feedback_id,h.rating,h.comment,h.created_at,
            f.categories_json,f.material_ids_json,f.search_id FROM feedback_history h JOIN feedback_v4 f ON f.id=h.feedback_id''')
        c.execute('DROP TABLE feedback_history')
        c.execute('DROP TABLE feedback')
        c.execute('ALTER TABLE feedback_v4 RENAME TO feedback')
        c.execute('ALTER TABLE feedback_history_v4 RENAME TO feedback_history')

    def save_search(self,user_id,filters,result,kind="search"):
        super().save_search(user_id,filters,result,kind)
        # Preserve the provider/version actually selected by this query, while
        # favorites keep the canonical DOI ID shared with the original catalog.
        with self.connect() as c:
            diagnostic = dict(result.diagnostics)
            decisions = dict(diagnostic.get('relevance',{}))
            for pos,item in enumerate(result.items):
                row = c.execute("SELECT material_id FROM search_results WHERE search_id=? AND position=?",(result.search_id,pos)).fetchone()
                if row:
                    payload = item.to_dict()
                    payload['id'] = row[0]
                    if item.id in decisions:decisions[row[0]]=decisions[item.id]
                    c.execute("INSERT OR REPLACE INTO result_payloads VALUES (?,?,?)",(result.search_id,pos,json.dumps(payload,ensure_ascii=False)))
            if decisions:diagnostic['relevance']=decisions
            extra = {'diagnostics': diagnostic,
                     'partial_items': [m.to_dict() for m in result.partial_items]}
            c.execute('INSERT OR REPLACE INTO search_details VALUES (?,?)',
                (result.search_id, json.dumps(extra, ensure_ascii=False)))

    def search_details(self, user_id, search_id):
        self.get_search(user_id, search_id)
        with self.connect() as c:
            row = c.execute('SELECT payload FROM search_details WHERE search_id=?', (search_id,)).fetchone()
        return json.loads(row[0]) if row else {}

    def result_item(self,user_id,search_id,position):
        self.get_search(user_id,search_id)
        with self.connect() as c:
            row = c.execute("SELECT payload FROM result_payloads WHERE search_id=? AND position=?",(search_id,position)).fetchone()
        if row:
            from search import Material
            return Material.from_dict(json.loads(row[0]))
        return super().result_item(user_id,search_id,position)

    def get_page(self,user_id,search_id,page,page_size=5):
        result = super().get_page(user_id,search_id,page,page_size)
        return [(pos,self.result_item(user_id,search_id,pos)) for pos,_ in result]

    def set_favorite(self,user_id,search_id,position,saved):
        item=self.result_item(user_id,search_id,position)
        with self.connect() as c:
            if saved:
                c.execute('INSERT OR IGNORE INTO favorites(user_id,material_id) VALUES (?,?)',(user_id,item.id))
                c.execute('INSERT OR REPLACE INTO favorite_payloads VALUES (?,?,?)',
                    (user_id,item.id,json.dumps(item.to_dict(),ensure_ascii=False)))
            else:c.execute('DELETE FROM favorites WHERE user_id=? AND material_id=?',(user_id,item.id))
        return item

    def saved_search(self,user_id):
        from search import Material,SearchFilters,SearchResult
        with self.connect() as c:
            rows=c.execute('''SELECT COALESCE(p.payload,m.material_json) FROM favorites f
                JOIN materials m ON m.id=f.material_id LEFT JOIN favorite_payloads p
                ON p.user_id=f.user_id AND p.material_id=f.material_id WHERE f.user_id=?
                ORDER BY f.created_at,f.material_id''',(user_id,)).fetchall()
        items=[Material.from_dict(json.loads(row[0])) for row in rows]
        result=SearchResult(items,[],uuid4().hex,len(items))
        self.save_search(user_id,SearchFilters(topic='Избранное'),result,kind='saved')
        return result.search_id

    def register(self, user_id):
        super().register(user_id)
        with self.connect() as c:
            c.execute("INSERT OR IGNORE INTO access(user_id) VALUES (?)", (user_id,))

    def cache_get(self, key):
        with self.connect() as c:
            row = c.execute("SELECT value FROM cache WHERE key=? AND expires>?", (key, time.time())).fetchone()
        return json.loads(row[0]) if row else None

    def cache_put(self, key, value, ttl=3600):
        with self.connect() as c:
            c.execute("DELETE FROM cache WHERE expires<?", (time.time(),))
            c.execute("INSERT OR REPLACE INTO cache VALUES (?,?,?)", (key, json.dumps(value, ensure_ascii=False), time.time()+ttl))

    def access(self, user_id):
        with self.connect() as c:
            row = c.execute("SELECT banned,ai_allowed FROM access WHERE user_id=?", (user_id,)).fetchone()
        return (bool(row[0]), bool(row[1])) if row else (False, True)

    def set_access(self, actor, user_id, field, enabled):
        if field not in ("banned", "ai_allowed"):
            raise ValueError("Неизвестное разрешение.")
        with self.connect() as c:
            if c.execute(f"UPDATE access SET {field}=? WHERE user_id=?", (int(enabled), user_id)).rowcount != 1:
                raise LookupError("Пользователь не найден.")
            c.execute("INSERT INTO audit(actor,event,target) VALUES (?,?,?)", (actor, field+":"+str(enabled), str(user_id)))

    def audit(self, actor, event, target=""):
        with self.connect() as c:
            c.execute("INSERT INTO audit(actor,event,target) VALUES (?,?,?)", (actor, event, str(target)))

    def users(self, offset=0):
        with self.connect() as c:
            return [dict(r) for r in c.execute("SELECT u.telegram_id,a.banned,a.ai_allowed FROM users u LEFT JOIN access a ON a.user_id=u.telegram_id ORDER BY u.telegram_id LIMIT 10 OFFSET ?", (offset,))]

    def feedback_save(self, user_id, search_id, position, rating, comment=None):
        """Compatibility for already delivered v3 Telegram buttons."""
        self.get_search(user_id, search_id)
        mid = self.result_item(user_id, search_id, position).id if position is not None else None
        with self.connect() as c:
            row = c.execute('SELECT id,comment FROM feedback WHERE user_id=? AND search_id=? AND material_id IS ? AND categories_json=? ORDER BY rowid DESC LIMIT 1',
                (user_id, search_id, mid, json.dumps(['publication'] if mid else ['results']))).fetchone()
        return self.feedback_submit(user_id, ('publication',) if mid else ('results',),
            row['comment'] if row and comment is None else comment or '', rating, search_id,
            (position,) if position is not None else (), row['id'] if row else None)

    def feedback_submit(self, user_id, categories, comment='', rating=None, search_id=None,
                        positions=None, feedback_id=None):
        allowed = ('bot', 'results', 'publication')
        if not isinstance(categories, (tuple, list)) or not categories or set(categories) - set(allowed):
            raise ValueError('Выберите одну, две или три категории отзыва.')
        categories = tuple(k for k in allowed if k in categories)
        if rating is not None and (type(rating) is not int or not 1 <= rating <= 5):
            raise ValueError('Оценка 1–5 или без оценки.')
        if not isinstance(comment, str) or len(comment) > 2000 or not comment.strip() and rating is None:
            raise ValueError('Добавьте комментарий до 2000 символов или оценку.')
        old = self.feedback_get(feedback_id) if feedback_id else None
        if old and old['user_id'] != user_id:
            raise ValueError('Чужой отзыв.')
        mids = []
        if 'results' in categories or 'publication' in categories:
            if not search_id:
                raise ValueError('Для оценки результатов выберите доступную выдачу.')
            preserving = old and old['search_id'] == search_id and positions is None
            if not preserving:
                self.get_search(user_id, search_id)
            if 'publication' in categories:
                if preserving:
                    mids = old['material_ids']
                else:
                    if not positions or len(positions) > 10:
                        raise ValueError('Выберите от 1 до 10 публикаций.')
                    mids = list(dict.fromkeys(self.result_item(user_id, search_id, pos).id for pos in positions))
                if not mids:
                    raise ValueError('Выберите публикацию.')
        else:
            search_id = None
        fid = feedback_id or uuid4().hex
        values = (rating, comment.strip(), search_id, mids[0] if mids else None,
                  json.dumps(categories), json.dumps(mids))
        with self.connect() as c:
            c.execute('BEGIN IMMEDIATE')
            if old:
                current = c.execute('SELECT * FROM feedback WHERE id=? AND user_id=?', (fid, user_id)).fetchone()
                same = tuple(current[k] for k in ('rating','comment','search_id','material_id','categories_json','material_ids_json'))
                if same == values:
                    return fid
                c.execute('''UPDATE feedback SET rating=?,comment=?,search_id=?,material_id=?,categories_json=?,material_ids_json=?,
                    handled=0,updated_at=CURRENT_TIMESTAMP WHERE id=? AND user_id=?''', values + (fid, user_id))
            else:
                c.execute('''INSERT INTO feedback(rating,comment,search_id,material_id,categories_json,material_ids_json,id,user_id)
                    VALUES (?,?,?,?,?,?,?,?)''', values + (fid, user_id))
            c.execute('''INSERT INTO feedback_history(feedback_id,rating,comment,categories_json,material_ids_json,search_id)
                VALUES (?,?,?,?,?,?)''', (fid, rating, comment.strip(), values[4], values[5], search_id))
        return fid

    @staticmethod
    def _feedback_dict(row):
        item = dict(row)
        item['categories'] = json.loads(item['categories_json'])
        item['material_ids'] = json.loads(item['material_ids_json'])
        return item

    def feedback_list(self, offset=0):
        with self.connect() as c:
            return [self._feedback_dict(r) for r in c.execute("SELECT * FROM feedback ORDER BY created_at DESC,rowid DESC LIMIT 10 OFFSET ?", (offset,))]

    def feedback_for_user(self, user_id):
        with self.connect() as c:
            return [self._feedback_dict(r) for r in c.execute('SELECT * FROM feedback WHERE user_id=? ORDER BY rowid DESC LIMIT 5', (user_id,))]

    def feedback_get(self, fid):
        with self.connect() as c:
            row = c.execute("SELECT * FROM feedback WHERE id=?", (fid,)).fetchone()
        if not row:
            raise LookupError("Отзыв не найден.")
        return self._feedback_dict(row)

    def request_limit(self, user_id, amount, comment):
        if not 0.01 <= amount <= 10 or len(comment) > 1000:
            raise ValueError("Сумма от $0.01 до $10, комментарий до 1000 символов.")
        with self.connect() as c:
            c.execute("BEGIN IMMEDIATE")
            if c.execute("SELECT 1 FROM limit_requests WHERE user_id=? AND status='pending'", (user_id,)).fetchone():
                raise ValueError("Предыдущая заявка ещё ожидает рассмотрения.")
            rid = uuid4().hex
            c.execute("INSERT INTO limit_requests(id,user_id,amount,comment) VALUES (?,?,?,?)", (rid,user_id,amount,comment))
        return rid

    def claim_delivery(self, update_id):
        with self.connect() as c:
            c.execute("DELETE FROM delivery WHERE created_at<?", (time.time()-7*86400,))
            return c.execute("INSERT OR IGNORE INTO delivery VALUES (?,'claimed',?)", (update_id,time.time())).rowcount == 1

    def finish_delivery(self, update_id, success=True):
        with self.connect() as c:
            if success:
                c.execute("UPDATE delivery SET status='done' WHERE update_id=?", (update_id,))
            else:
                c.execute("DELETE FROM delivery WHERE update_id=?", (update_id,))
