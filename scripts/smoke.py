"""Opt-in live checks; never prints credentials or raw upstream errors."""
import argparse
import asyncio
from dataclasses import replace
import json
from pathlib import Path
import sys
from uuid import uuid4

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT))

from lab_config import load_lab_config
from lab_storage import LabDatabase
from external_sources import MetadataHTTP, adapters
from litellm_admin import LiteLLMAdmin
from translation import Translator


async def main(args):
    config = load_lab_config(require_token=False,external=True)
    # Use an isolated DB for smoke keys/feedback, not fake Telegram users in the real DB.
    db = LabDatabase(ROOT/'runtime/smoke.db')
    db.initialize()
    results = {}
    if args.apis:
        http = MetadataHTTP(db,config)
        try:
            for name,adapter in adapters(http).items():
                try:
                    rows = await adapter.search('water supply',limit=3)
                    results[name] = {'ok':bool(rows),'count':len(rows),'doi_count':sum(bool(m.doi) for m in rows),
                        'abstracts':sum(bool(m.abstract_or_snippet) for m in rows)}
                except Exception as exc: results[name] = {'ok':False,'error_type':type(exc).__name__}
        finally: await http.close()
    if args.translation:
        translator = Translator(db,True,60)
        try:
            for text,source,target in [('водоснабжение и качество питьевой воды','ru','en'),('Water quality and climate change','en','ru')]:
                translated,warning = await translator.translate(text,source,target)
                results[source+'_'+target] = {'ok':translated!=text and not warning,'text':translated,'warning':warning}
        finally: translator.close()
    if args.litellm:
        import httpx
        management = LiteLLMAdmin(db,config)
        try:
            ready = await management.request('GET','/health/readiness')
            uid = -10001
            db.register(uid)
            limits = await management.get_limits(uid)
            results['litellm'] = {'ready':True,**{k:v for k,v in limits.items() if k!='user_id'}}
            if args.management:
                actor = next(iter(config.admin_telegram_ids),None)
                if actor is None: raise ValueError('admin required for management smoke')
                cached = db.cache_get('management_smoke_op')
                if cached:
                    op = management.operation(cached)
                else:
                    op = await management.prepare_increase(actor,uid,0.5)
                    db.cache_put('management_smoke_op',op['id'],3650*86400)
                await management.confirm_increase(actor,op['id'])
                await management.confirm_increase(actor,op['id'])
                after = await management.get_limits(uid)
                results['management'] = {'confirmed_twice':True,'old_budget':op['before_usd'],'new_budget':after['allocated'],
                    'exact_increase':abs(after['allocated']-op['after_usd'])<0.000001,'spent_unchanged':limits['spent']==after['spent']}
            _,key = await management.ensure(uid)
            async with httpx.AsyncClient(base_url=config.litellm_url,timeout=30) as c:
                # An isolated zero budget key must be rejected before contacting OpenAI.
                blocked = 'sk-smoke-'+uuid4().hex
                await management.request('POST','/key/generate',json={'key':blocked,'user_id':'budget-denial-smoke','models':['research-basic'],'max_budget':0.000001,'spend':0.01})
                r = await c.post('/v1/responses',headers={'Authorization':'Bearer '+blocked},json={'model':'research-basic','input':'Reply OK','max_output_tokens':16})
                error = r.json().get('error',{})
                message = str(error.get('message','')) if isinstance(error,dict) else str(error)
                results['budget_denial'] = {'http_status':r.status_code,'budget_rejected':r.status_code>=400 and 'budget' in message.lower()}
                await management.request('POST','/key/delete',json={'keys':[blocked]})
                if args.paid:
                    r = await c.post('/v1/responses',headers={'Authorization':'Bearer '+key},json={
                        'model':'research-basic','input':'Reply with the single word OK.','max_output_tokens':32,'reasoning':{'effort':'none'},'store':False})
                    data = r.json()
                    results['paid'] = {'http_status':r.status_code,'ok':r.is_success,'usage':data.get('usage'),
                        'cost_header':r.headers.get('x-litellm-response-cost')}
                    if not r.is_success:
                        error = data.get('error',{})
                        results['paid']['error_type'] = error.get('type') if isinstance(error,dict) else 'provider_error'
                    await asyncio.sleep(3)
                    results['after_paid'] = {k:v for k,v in (await management.get_limits(uid)).items() if k!='user_id'}
        except Exception as exc:
            results['litellm_error'] = {'error_type':type(exc).__name__}
        finally: await management.close()
    if args.telegram:
        from telegram import Bot
        from deployment import polling_ready
        try:
            async with Bot(config.bot_token) as bot:
                me = await bot.get_me()
                await polling_ready(bot)
                updates = await bot.get_updates(timeout=2,allowed_updates=['message','callback_query'])
                results['telegram'] = {'get_me':True,'is_bot':me.is_bot,'webhook_removed':True,'pending_updates':len(updates)}
        except Exception as exc: results['telegram'] = {'ok':False,'error_type':type(exc).__name__}
    output = json.dumps(results,ensure_ascii=False,indent=2)
    # All outputs above are explicitly selected public metadata and numeric metrics.
    print(output)
    output_path = ROOT/'runtime/live-checks.json'
    previous = json.loads(output_path.read_text(encoding='utf-8')) if output_path.exists() else {}
    previous.update(results)
    output_path.write_text(json.dumps(previous,ensure_ascii=False,indent=2),encoding='utf-8')


if __name__ == '__main__':
    for stream in (sys.stdout,sys.stderr):
        if hasattr(stream,'reconfigure'): stream.reconfigure(encoding='utf-8')
    parser = argparse.ArgumentParser()
    for name in ('apis','translation','litellm','paid','telegram','management'): parser.add_argument('--'+name,action='store_true')
    asyncio.run(main(parser.parse_args()))
