"""Consent-gated live feedback smoke; synthetic data, isolated key and SQLite."""
import argparse
import asyncio
from dataclasses import replace
import json
from pathlib import Path
import sys
from uuid import uuid4

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from lab_config import load_lab_config
from lab_storage import LabDatabase
from litellm_admin import LiteLLMAdmin
from research_ai import ResearchAI

FIXTURES = [
    {'rating':3, 'categories':['bot','results'], 'comment':'ТЕСТОВЫЕ ДАННЫЕ: неудобно повторно выбирать годы.'},
    {'rating':None, 'categories':['publication'], 'comment':'ТЕСТОВЫЕ ДАННЫЕ: отсутствует аннотация, нужен явный индикатор.'},
]


async def run(args):
    if not (args.paid and args.consent):
        print(json.dumps({'prepared': True, 'paid_calls': 0, 'route':'LiteLLM /v1/responses',
            'scope':'isolated synthetic feedback; separate <=$0.01 test key',
            'feedback': FIXTURES, 'requires':'owner consent, then --paid --consent'}, ensure_ascii=False, indent=2))
        return 0
    config = load_lab_config(require_token=False)
    if not config.admin_telegram_ids or not config.ai_enabled:
        raise ValueError('Configure the admin allowlist and ENABLE_AI before an explicitly approved live test.')
    # Never open the real bot database or touch real feedback.
    path = config.db_path.parent / 'feedback-analysis-smoke.db'
    db = LabDatabase(path)
    db.initialize()
    with db.connect() as connection:
        row=connection.execute("SELECT value FROM app_settings WHERE key='smoke_scope'").fetchone()
    scope=row[0] if row else ''
    if not scope:
        scope = 'feedback-analysis-smoke-' + uuid4().hex[:12]
        with db.connect() as connection:
            connection.execute("INSERT INTO app_settings VALUES ('smoke_scope',?)",(scope,))
    config = replace(config, db_path=path, admin_budget=0.01, max_output_tokens=min(config.max_output_tokens, 200))
    management = LiteLLMAdmin(db, config, analysis_scope=scope)
    ai = ResearchAI(db, config, management)
    actor = min(config.admin_telegram_ids)
    try:
        before = await management.get_limits(actor, admin=True)
        with db.connect() as c:
            cached = c.execute("SELECT 1 FROM ai_calls WHERE status='done'").fetchone() is not None
        response = await ai.analyze(actor, FIXTURES, 'explicit-feedback-smoke-v1')
        after = before
        for _ in range(6):
            after = await management.get_limits(actor, admin=True)
            if cached or after['spent'] > before['spent']:
                break
            await asyncio.sleep(0.5)  # GET/read only; no generation retry
        with db.connect() as c:
            call = c.execute("SELECT response_id,cost_usd,usage_json,status FROM ai_calls ORDER BY created_at DESC LIMIT 1").fetchone()
        payload = dict(synthetic_data=True, separate_test_budget=True, cached_response=cached,
            model=config.primary_model, response=response, response_id=call['response_id'],
            reported_cost_usd=call['cost_usd'], usage=json.loads(call['usage_json'] or '{}'),
            spent_before=before['spent'], spent_after=after['spent'],
            delta_spend=after['spent'] - before['spent'], status=call['status'],
            actual_spend_confirmed=after['spent'] > before['spent'] if not cached else None)
        target = config.db_path.parent / 'feedback-analysis-smoke.json'
        target.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding='utf-8')
        print(json.dumps(payload, ensure_ascii=False, indent=2))
        return 0 if cached or payload['actual_spend_confirmed'] else 3
    finally:
        await ai.close()
        await management.close()


if __name__ == '__main__':
    parser = argparse.ArgumentParser()
    parser.add_argument('--paid', action='store_true')
    parser.add_argument('--consent', action='store_true', help='Owner explicitly approved this bounded live charge')
    try:
        raise SystemExit(asyncio.run(run(parser.parse_args())))
    except ValueError as exc:
        print(str(exc), file=sys.stderr)
        raise SystemExit(2)
