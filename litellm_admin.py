"""Only this server-side management client sends the master key."""
import asyncio
import logging
import secrets
from uuid import uuid4

import httpx

logger = logging.getLogger(__name__)


class BudgetUnavailable(ValueError):
    pass


class LiteLLMAdmin:
    def __init__(self,db,config,transport=None,analysis_scope='feedback-analysis'):
        self.db,self.config = db,config
        self.analysis_scope = analysis_scope
        self.client = httpx.AsyncClient(base_url=config.litellm_url,timeout=config.llm_timeout,transport=transport)
        self.locks = {}

    def lock(self,user_id):
        return self.locks.setdefault(str(user_id),asyncio.Lock())

    async def request(self,method,path,**kwargs):
        if not self.config.master_key:
            raise BudgetUnavailable("LiteLLM не настроен. Бесплатный поиск доступен; остаток бюджета неизвестен.")
        try:
            r = await self.client.request(method,path,headers={"Authorization":"Bearer "+self.config.master_key},**kwargs)
            r.raise_for_status()
            return r.json()
        except (httpx.HTTPError,ValueError):
            raise BudgetUnavailable("LiteLLM временно недоступен. Бесплатный поиск доступен; остаток бюджета неизвестен.") from None

    async def _ensure(self,user_id,admin=False):
        uid = self.analysis_scope if admin else "telegram-"+str(user_id)
        with self.db.connect() as c:
            row = c.execute("SELECT secret,ready FROM llm_keys WHERE user_id=?", (uid,)).fetchone()
            if not row:
                secret = "sk-"+secrets.token_urlsafe(32)
                c.execute("INSERT INTO llm_keys VALUES (?,?,0)",(uid,secret))
            else:
                secret = row[0]
        if row and row[1]:
            return uid,secret
        budget = self.config.admin_budget if admin else self.config.default_budget
        # Look up before creating: an uncertain POST must never mint another key.
        try:
            existing = await self.request("GET","/key/info",params={"key":secret})
        except BudgetUnavailable:
            existing = None
        if existing is None:
            try:
                await self.request("POST","/user/new",json={"user_id":uid,"user_role":"internal_user","max_budget":budget})
            except BudgetUnavailable:
                # A previous uncertain create can have succeeded; verify its existence.
                await self.request("GET","/user/info",params={"user_id":uid})
            await self.request("POST","/key/generate",json={"key":secret,"user_id":uid,
                "models":["research-basic"],"max_budget":budget,"max_parallel_requests":1,
                "metadata":{"purpose":"feedback-analysis" if admin else "research"}})
        with self.db.connect() as c:
            c.execute("UPDATE llm_keys SET ready=1 WHERE user_id=?",(uid,))
        return uid,secret

    async def ensure(self,user_id,admin=False):
        async with self.lock("admin-analysis" if admin else user_id):
            return await self._ensure(user_id,admin)

    async def _limits(self,user_id,admin=False):
        uid,key = await self._ensure(user_id,admin)
        data = await self.request("GET","/key/info",params={"key":key})
        info = data.get("info",data)
        if info.get("max_budget") is None or info.get("spend") is None:
            raise BudgetUnavailable("LiteLLM не вернул spend/max_budget. Остаток неизвестен.")
        maximum,spend = float(info["max_budget"]),float(info["spend"])
        return {"allocated":maximum,"spent":spend,"remaining":max(0,maximum-spend),"user_id":uid}

    async def get_limits(self,user_id,admin=False):
        async with self.lock("admin-analysis" if admin else user_id):
            return await self._limits(user_id,admin)

    async def prepare_increase(self,actor,user_id,amount=0.50,request_id=None):
        if actor not in self.config.admin_telegram_ids:
            raise ValueError("Доступ запрещён.")
        if not 0.01 <= amount <= 10:
            raise ValueError("Сумма от $0.01 до $10.")
        async with self.lock(user_id):
            limits = await self._limits(user_id)
            with self.db.connect() as c:
                c.execute("BEGIN IMMEDIATE")
                if request_id:
                    req = c.execute("SELECT * FROM limit_requests WHERE id=? AND user_id=? AND status='pending'",(request_id,user_id)).fetchone()
                    if not req:
                        raise ValueError("Заявка уже обработана или недоступна.")
                active = c.execute("SELECT id FROM budget_ops WHERE user_id=? AND status IN ('pending','applying','unknown')",(user_id,)).fetchone()
                if active:
                    raise ValueError("Предыдущее изменение ожидает подтверждения/сверки. Откройте список операций.")
                oid = uuid4().hex
                c.execute("INSERT INTO budget_ops(id,user_id,admin_id,before_usd,after_usd,amount,request_id) VALUES (?,?,?,?,?,?,?)",
                    (oid,user_id,actor,limits['allocated'],round(limits['allocated']+amount,2),amount,request_id))
            return self.operation(oid)

    def operation(self,oid):
        with self.db.connect() as c:
            row = c.execute("SELECT * FROM budget_ops WHERE id=?",(oid,)).fetchone()
        if not row: raise LookupError("Операция не найдена.")
        return dict(row)

    async def confirm_increase(self,actor,oid):
        op = self.operation(oid)
        if actor not in self.config.admin_telegram_ids or op['admin_id'] != actor:
            raise ValueError("Подтверждение доступно автору операции.")
        async with self.lock(op['user_id']):
            op = self.operation(oid)
            if op['status'] in ('done','cancelled'):
                return op
            if op['request_id']:
                with self.db.connect() as c:
                    request = c.execute("SELECT status FROM limit_requests WHERE id=?",(op['request_id'],)).fetchone()
                if not request or request[0] not in ('pending','processing'):
                    raise ValueError("Заявка уже отклонена или недоступна. Изменение не выполнено.")
            limits = await self._limits(op['user_id'])
            current = limits['allocated']
            if abs(current-op['after_usd']) < 0.000001 and op['status'] in ('applying','unknown'):
                self._finish(op)
                return self.operation(oid)
            if abs(current-op['before_usd']) > 0.000001:
                raise BudgetUnavailable("Лимит изменился другим действием; требуется сверка. Повторное начисление запрещено.")
            with self.db.connect() as c:
                c.execute('BEGIN IMMEDIATE')
                if op['request_id']:
                    changed = c.execute("UPDATE limit_requests SET status='processing' WHERE id=? AND status IN ('pending','processing')",(op['request_id'],)).rowcount
                    if not changed: raise ValueError("Заявка уже обработана; изменение не выполнено.")
                c.execute("UPDATE budget_ops SET status='applying',updated_at=CURRENT_TIMESTAMP WHERE id=?",(oid,))
            uid,key = await self._ensure(op['user_id'])
            try:
                # Key budget is authoritative. Keep user ceiling aligned without resetting spend.
                await self.request("POST","/user/update",json={"user_id":uid,"max_budget":op['after_usd']})
                await self.request("POST","/key/update",json={"key":key,"max_budget":op['after_usd']})
                actual = await self._limits(op['user_id'])
                if abs(actual['allocated']-op['after_usd']) > 0.000001:
                    raise BudgetUnavailable("Изменение лимита ещё не подтверждено API.")
            except BudgetUnavailable:
                with self.db.connect() as c:
                    c.execute("UPDATE budget_ops SET status='unknown',error='management_unconfirmed',updated_at=CURRENT_TIMESTAMP WHERE id=?",(oid,))
                raise
            self._finish(op)
            logger.info("event=budget_increased operation=%s",oid)
            return self.operation(oid)

    def _finish(self,op):
        with self.db.connect() as c:
            c.execute("UPDATE budget_ops SET status='done',error=NULL,updated_at=CURRENT_TIMESTAMP WHERE id=?",(op['id'],))
            if op['request_id']:
                c.execute("UPDATE limit_requests SET status='approved',response='Лимит увеличен' WHERE id=?",(op['request_id'],))
        self.db.audit(op['admin_id'],"budget_increased",op['id'])

    async def close(self):
        await self.client.aclose()
