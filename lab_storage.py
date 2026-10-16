"""Additive v3 migration: cache, access, feedback, durable management operations."""
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
            if version and int(version[0]) > 3:
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
            ]
            for sql in statements:
                c.execute(sql)
            if 'notified' not in {r[1] for r in c.execute('PRAGMA table_info(budget_ops)')}:
                c.execute('ALTER TABLE budget_ops ADD COLUMN notified INTEGER NOT NULL DEFAULT 0')
            c.execute("INSERT OR REPLACE INTO app_settings VALUES ('lab_schema','3')")

    def save_search(self,user_id,filters,result,kind="search"):
        super().save_search(user_id,filters,result,kind)
        # Preserve the provider/version actually selected by this query, while
        # favorites keep the canonical DOI ID shared with the original catalog.
        with self.connect() as c:
            for pos,item in enumerate(result.items):
                row = c.execute("SELECT material_id FROM search_results WHERE search_id=? AND position=?",(result.search_id,pos)).fetchone()
                if row:
                    payload = item.to_dict()
                    payload['id'] = row[0]
                    c.execute("INSERT OR REPLACE INTO result_payloads VALUES (?,?,?)",(result.search_id,pos,json.dumps(payload,ensure_ascii=False)))

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
        self.get_search(user_id, search_id)
        mid = self.result_item(user_id, search_id, position).id if position is not None else None
        if type(rating) is not int or not 1 <= rating <= 5 or (comment is not None and len(comment) > 2000):
            raise ValueError("Оценка 1–5, комментарий до 2000 символов.")
        with self.connect() as c:
            c.execute("BEGIN IMMEDIATE")
            row = c.execute("SELECT id,rating,comment FROM feedback WHERE user_id=? AND search_id=? AND material_id IS ?", (user_id, search_id, mid)).fetchone()
            fid = row[0] if row else uuid4().hex
            comment = row['comment'] if row and comment is None else (comment or "")
            if row and row['rating']==rating and row['comment']==comment:
                return fid
            if row:
                c.execute("UPDATE feedback SET rating=?,comment=?,handled=0,updated_at=CURRENT_TIMESTAMP WHERE id=?", (rating, comment, fid))
            else:
                c.execute("INSERT INTO feedback(id,user_id,search_id,material_id,rating,comment) VALUES (?,?,?,?,?,?)", (fid,user_id,search_id,mid,rating,comment))
            c.execute("INSERT INTO feedback_history(feedback_id,rating,comment) VALUES (?,?,?)", (fid,rating,comment))
        return fid

    def feedback_list(self, offset=0):
        with self.connect() as c:
            return [dict(r) for r in c.execute("SELECT * FROM feedback ORDER BY created_at DESC,rowid DESC LIMIT 10 OFFSET ?", (offset,))]

    def feedback_get(self, fid):
        with self.connect() as c:
            row = c.execute("SELECT * FROM feedback WHERE id=?", (fid,)).fetchone()
        if not row:
            raise LookupError("Отзыв не найден.")
        return dict(row)

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
