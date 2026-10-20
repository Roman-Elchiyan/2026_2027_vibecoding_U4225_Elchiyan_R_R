"""Every admin command, callback and text step checks the allowlist."""
import asyncio
import logging
import hashlib
import json
from uuid import uuid4

from telegram import InlineKeyboardButton as B, InlineKeyboardMarkup as K
from handlers import clipped, html_text
from feedback_ui import CATEGORY_LABELS
from litellm_admin import BudgetUnavailable

logger = logging.getLogger(__name__)


def require_admin(app,uid):
    if uid not in app.config.admin_telegram_ids: raise ValueError("Доступ запрещён: требуется администратор.")


def home_keyboard(app):
    paused = app.db.get_app_setting("search_paused",False)
    return K([[B("Состояние сервисов",callback_data="admin:status"),B("Пользователи",callback_data="admin:users:0")],
        [B("Возобновить поиск" if paused else "Пауза новых поисков",callback_data="admin:pause:"+str(int(not paused)))],
        [B("Заявки",callback_data="admin:requests:0"),B("Отзывы",callback_data="admin:reviews:0")],
        [B("Операции бюджета",callback_data="admin:operations:0")],
        [B("E5 вкл.",callback_data="admin:local:on"),B("E5 выкл.",callback_data="admin:local:off")],
        [B("E5 внешних API вкл.",callback_data="admin:external:on"),B("E5 внешних API выкл.",callback_data="admin:external:off")]])


async def admin_command(update,context):
    app = context.application.bot_data['controller']
    uid = app.register(update)
    require_admin(app,uid)
    await update.effective_message.reply_text("Администратор Lab 3",reply_markup=home_keyboard(app))


async def admin_callback(update,context):
    app = context.application.bot_data['controller']
    uid = app.register(update)
    require_admin(app,uid)
    q = update.callback_query
    await q.answer()
    parts = q.data.split(":")
    action = parts[1]
    if action == "home": return await q.message.reply_text("Администратор",reply_markup=home_keyboard(app))
    if action == "status":
        text = await asyncio.to_thread(app.status_text)
        try:
            data = await app.budgets.request("GET","/health/readiness")
            text += "\nLiteLLM/PostgreSQL: readiness подтверждён."
        except BudgetUnavailable:
            text += "\nLiteLLM/PostgreSQL: readiness не подтверждён."
        with app.db.connect() as c:
            counts = c.execute("SELECT COUNT(*),AVG(rating),SUM(handled=0) FROM feedback").fetchone()
        average = f'{counts[1]:.2f}' if counts[1] is not None else 'нет оценок'
        text += f"\nОтзывы: {counts[0]}, средняя оценка: {average}, не обработано: {counts[2] or 0}."
        return await q.message.reply_text(text)
    if action in ("local","external"):
        if app.search_service.semantic is None: app.initialize_search()
        app.search_service.semantic.set_enabled(parts[2]=='on',external=action=='external')
        app.db.audit(uid,"e5_"+action+"_"+parts[2])
        return await q.message.reply_text(app.search_service.semantic.state())
    if action == "pause":
        app.db.set_app_setting("search_paused",parts[2]=='1')
        app.db.audit(uid,"pause_"+parts[2])
        return await q.message.reply_text("Настройка паузы сохранена.",reply_markup=home_keyboard(app))
    if action == "users":
        offset = max(0,int(parts[2]))
        users = app.db.users(offset)
        rows = [[B(f"{r['telegram_id']} · {'бан' if r['banned'] else 'доступен'}",callback_data=f"admin:user:{r['telegram_id']}")] for r in users]
        nav = []
        if offset: nav.append(B("Назад",callback_data=f"admin:users:{max(0,offset-10)}"))
        if len(users)==10: nav.append(B("Далее",callback_data=f"admin:users:{offset+10}"))
        if nav: rows.append(nav)
        return await q.message.reply_text("Пользователи",reply_markup=K(rows))
    if action == "user":
        target = int(parts[2])
        banned,allowed = app.db.access(target)
        try:
            limits = await app.budgets.get_limits(target)
            text = f"Пользователь {target}\nЛимит ${limits['allocated']:.2f}; потрачено ${limits['spent']:.6f}."
        except BudgetUnavailable as exc: text = f"Пользователь {target}\n{exc}"
        return await q.message.reply_text(text,reply_markup=K([
            [B("+$0.50",callback_data=f"admin:add:{target}:0.50"),B("+$1.00",callback_data=f"admin:add:{target}:1.00"),B("Указать сумму",callback_data=f"admin:amount:{target}")],
            [B("Разблокировать" if banned else "Блокировать",callback_data=f"admin:access:{target}:banned:{int(not banned)}")],
            [B("Выключить ИИ" if allowed else "Включить ИИ",callback_data=f"admin:access:{target}:ai_allowed:{int(not allowed)}")]]))
    if action == "access":
        target = int(parts[2])
        if target in app.config.admin_telegram_ids and parts[3]=='banned': raise ValueError("Администратора блокировать нельзя.")
        app.db.set_access(uid,target,parts[3],parts[4]=='1')
        return await q.message.reply_text("Разрешение сохранено.")
    if action == "amount":
        f,_ = app.filters(uid)
        app.db.set_settings(uid,f,"admin_amount:"+parts[2])
        return await q.message.reply_text("Введите сумму увеличения от $0.01 до $10. Затем потребуется подтверждение.")
    if action == "add":
        op = await app.budgets.prepare_increase(uid,int(parts[2]),float(parts[3]))
        return await show_confirmation(q.message,op)
    if action == "confirm":
        op = await app.budgets.confirm_increase(uid,parts[2])
        if op['status']!='done': return await q.message.reply_text("Операция отменена.")
        await q.message.reply_text(f"API подтвердил лимит ${op['after_usd']:.2f}; ранее потраченное сохранено.")
        if op['request_id'] and not op['notified']:
            await context.bot.send_message(chat_id=op['user_id'],text=f"Заявка одобрена. API подтвердил увеличение лимита на ${op['amount']:.2f}, новый лимит ${op['after_usd']:.2f}. Расход сохранён. /limits")
            with app.db.connect() as c: c.execute('UPDATE budget_ops SET notified=1 WHERE id=?',(op['id'],))
        return
    if action == "opcancel":
        op = app.budgets.operation(parts[2])
        if op['admin_id'] != uid: raise ValueError("Операция другого администратора.")
        with app.db.connect() as c:
            changed = c.execute("UPDATE budget_ops SET status='cancelled',updated_at=CURRENT_TIMESTAMP WHERE id=? AND status='pending'",(op['id'],)).rowcount
        return await q.message.reply_text("Операция отменена." if changed else "Отмена недоступна: операция уже отправлена/обработана; сначала сверьте лимит.")
    if action == "operations":
        offset = max(0,int(parts[2]))
        with app.db.connect() as c:
            ops = [dict(r) for r in c.execute("SELECT * FROM budget_ops WHERE admin_id=? ORDER BY created_at DESC,rowid DESC LIMIT 10 OFFSET ?",(uid,offset))]
        rows = [[B(f"{r['user_id']}: +${r['amount']:.2f} · {r['status']}",callback_data="admin:op:"+r['id'])] for r in ops]
        if offset: rows.append([B("Назад",callback_data=f"admin:operations:{max(0,offset-10)}")])
        if len(ops)==10: rows.append([B("Далее",callback_data=f"admin:operations:{offset+10}")])
        return await q.message.reply_text("Операции (unknown — сначала сверка фактического лимита)",reply_markup=K(rows))
    if action == "op":
        op = app.budgets.operation(parts[2])
        if op['admin_id'] != uid: raise ValueError("Чужая операция.")
        return await show_confirmation(q.message,op)
    if action == "requests":
        offset = max(0,int(parts[2]))
        with app.db.connect() as c:
            requests = [dict(r) for r in c.execute("SELECT * FROM limit_requests WHERE status='pending' ORDER BY created_at LIMIT 10 OFFSET ?",(offset,))]
        rows = [[B(f"{r['user_id']}: +${r['amount']:.2f}",callback_data="admin:req:"+r['id'])] for r in requests]
        if offset: rows.append([B("Назад",callback_data=f"admin:requests:{max(0,offset-10)}")])
        if len(requests)==10: rows.append([B("Далее",callback_data=f"admin:requests:{offset+10}")])
        return await q.message.reply_text("Ожидающие заявки" if requests else "Ожидающих заявок нет.",reply_markup=K(rows))
    if action in ("req","approve","reject"):
        rid = parts[2]
        with app.db.connect() as c:
            request = c.execute("SELECT * FROM limit_requests WHERE id=? AND status='pending'",(rid,)).fetchone()
        if not request: raise ValueError("Заявка уже обработана.")
        if action == "req":
            return await q.message.reply_text(f"Пользователь {request['user_id']}; +${request['amount']:.2f}\n{request['comment']}",reply_markup=K([
                [B("Одобрить запрошенную сумму",callback_data="admin:approve:"+rid)],
                [B("Отклонить с ответом",callback_data="admin:reject:"+rid)]]))
        if action == "approve":
            op = await app.budgets.prepare_increase(uid,request['user_id'],request['amount'],rid)
            return await show_confirmation(q.message,op)
        f,_ = app.filters(uid)
        app.db.set_settings(uid,f,"admin_reject:"+rid)
        return await q.message.reply_text("Введите ответ автору заявки (до 1000 символов). /cancel — отменить.")
    if action == "reviews":
        offset = max(0,int(parts[2]))
        reviews = app.db.feedback_list(offset)
        selected = app.db.cache_get("review_selection:"+str(uid)) or []
        rows = [[B(f"{str(r['rating']) + '/5' if r['rating'] else 'без оценки'} · {'обработан' if r['handled'] else 'новый'} · {clipped(r['comment'],30)}",callback_data="admin:review:"+r['id']),
                 B("✓" if r['id'] in selected else "+ анализ",callback_data="admin:select:"+r['id'])] for r in reviews]
        if offset: rows.append([B("Назад",callback_data=f"admin:reviews:{max(0,offset-10)}")])
        if len(reviews)==10: rows.append([B("Далее",callback_data=f"admin:reviews:{offset+10}")])
        if selected: rows.append([B(f"ИИ-анализ выбранных ({len(selected)}) — платно",callback_data="admin:analyze")])
        return await q.message.reply_text("Отзывы: выберите запись для ответа или + для анализа.",reply_markup=K(rows))
    if action == "review":
        r = app.db.feedback_get(parts[2])
        detail = (f"Автор: {r['user_id']}\nКатегории: {', '.join(CATEGORY_LABELS[c] for c in r['categories'])}\n"
            f"Выдача: {r['search_id'] or 'общий отзыв без поиска'}\nМатериалы: {clipped(', '.join(r['material_ids']) or 'не выбраны', 500)}\n"
            f"Оценка: {str(r['rating']) + '/5' if r['rating'] else 'без оценки'}\nСтатус: {'обработан' if r['handled'] else 'новый'}\n"
            f"{r['comment']}\nОтвет: {r['reply'] or 'нет'}")
        return await q.message.reply_text(html_text(detail, 3800), parse_mode='HTML',
            reply_markup=K([[B("Ответить автору",callback_data="admin:reply:"+r['id'])],
                [B("Отметить обработанным" if not r['handled'] else "Вернуть в необработанные",callback_data=f"admin:handled:{r['id']}:{int(not r['handled'])}")]]))
    if action == "handled":
        app.db.feedback_get(parts[2])
        with app.db.connect() as c: c.execute("UPDATE feedback SET handled=? WHERE id=?",(int(parts[3]),parts[2]))
        app.db.audit(uid,"feedback_handled",parts[2])
        return await q.message.reply_text("Статус отзыва сохранён.")
    if action == "reply":
        app.db.feedback_get(parts[2])
        f,_ = app.filters(uid)
        app.db.set_settings(uid,f,"admin_reply:"+parts[2])
        return await q.message.reply_text("Введите ответ только автору этого отзыва (до 2000 символов). /cancel — отменить.")
    if action == "select":
        app.db.feedback_get(parts[2])
        selected = app.db.cache_get("review_selection:"+str(uid)) or []
        if parts[2] in selected: selected.remove(parts[2])
        elif len(selected)<10: selected.append(parts[2])
        else: raise ValueError("Не более 10 отзывов для одного анализа.")
        app.db.cache_put("review_selection:"+str(uid),selected,3600)
        return await q.message.reply_text(f"Выбрано отзывов: {len(selected)}. Вернитесь к списку /admin → Отзывы.")
    if action == "analyze":
        selected = app.db.cache_get("review_selection:"+str(uid)) or []
        if not selected: raise ValueError("Сначала выберите отзывы.")
        rows = [app.db.feedback_get(fid) for fid in selected]
        nonce = uuid4().hex[:16]
        digest = hashlib.sha256(json.dumps(rows, sort_keys=True, ensure_ascii=False).encode()).hexdigest()
        app.db.cache_put(f'analysis-confirm:{uid}:{nonce}', {'ids': selected, 'digest': digest}, 600)
        return await q.message.reply_text(f'Платный анализ {len(rows)} отзывов: категории, оценки, обезличенные комментарии, запросы и короткие выдержки выбранных публикаций из сохранённой выдачи. '
            'Используется отдельный административный бюджет LiteLLM. Код, права и лимиты не изменяются. Подтвердите расход.',
            reply_markup=K([[B('Подтвердить платный анализ', callback_data='admin:analyzeconfirm:' + nonce)]]))
    if action == 'analyzeconfirm':
        key = f'analysis-confirm:{uid}:{parts[2]}'
        confirmation = app.db.cache_get(key)
        if not confirmation or confirmation.get('sent'):
            raise ValueError('Подтверждение истекло или запрос уже отправлен. Автоматического повтора нет.')
        rows = [app.db.feedback_get(fid) for fid in confirmation['ids']]
        digest = hashlib.sha256(json.dumps(rows, sort_keys=True, ensure_ascii=False).encode()).hexdigest()
        if digest != confirmation['digest']:
            raise ValueError('Отзывы изменились. Выберите их и подтвердите анализ заново.')
        confirmation['sent'] = True
        app.db.cache_put(key, confirmation, 600)
        output = await app.ai_service.analyze(uid, rows, 'feedback:' + digest)
        app.db.audit(uid,"feedback_analysis")
        return await q.message.reply_text(clipped(output,3800))
    raise ValueError("Кнопка администратора устарела.")


async def show_confirmation(message,op):
    await message.reply_text(f"Пользователь: {op['user_id']}\nСтарый лимит: ${op['before_usd']:.2f}\n"
        f"Увеличение: +${op['amount']:.2f}\nНовый лимит: ${op['after_usd']:.2f}\nСтатус: {op['status']}\nПодтвердить изменение?",
        reply_markup=K([[B("Подтвердить / сверить",callback_data="admin:confirm:"+op['id']),B("Отмена",callback_data="admin:opcancel:"+op['id'])]]))


async def admin_text(app,update,context,text,state):
    uid = app.register(update)
    require_admin(app,uid)
    if not 1 <= len(text) <= 2000: raise ValueError("Введите от 1 до 2000 символов.")
    kind,target = state.split(":",1)
    f,_ = app.filters(uid)
    if kind == "admin_amount":
        try: amount = float(text.replace(",","."))
        except ValueError: raise ValueError("Введите сумму числом.") from None
        op = await app.budgets.prepare_increase(uid,int(target),amount)
        app.db.set_settings(uid,f)
        return await show_confirmation(update.effective_message,op)
    if kind == "admin_reply":
        record = app.db.feedback_get(target)
        await context.bot.send_message(chat_id=record['user_id'],text="Ответ администратора на ваш отзыв:\n"+text)
        with app.db.connect() as c:
            c.execute("UPDATE feedback SET reply=?,handled=1,updated_at=CURRENT_TIMESTAMP WHERE id=?",(text,target))
        app.db.audit(uid,"feedback_reply",target)
    elif kind == "admin_reject":
        if len(text)>1000: raise ValueError("Ответ до 1000 символов.")
        with app.db.connect() as c:
            c.execute('BEGIN IMMEDIATE')
            record = c.execute("SELECT * FROM limit_requests WHERE id=? AND status='pending'",(target,)).fetchone()
            active = c.execute("SELECT 1 FROM budget_ops WHERE request_id=? AND status IN ('pending','applying','unknown')",(target,)).fetchone()
            if active: raise ValueError("По заявке уже подготовлено изменение бюджета. Сначала отмените или сверьте операцию.")
        if not record: raise ValueError("Заявка уже обработана.")
        await context.bot.send_message(chat_id=record['user_id'],text="Ответ по заявке на увеличение лимита:\n"+text)
        with app.db.connect() as c:
            c.execute("UPDATE limit_requests SET status='rejected',response=? WHERE id=? AND status='pending'",(text,target))
        app.db.audit(uid,"limit_rejected",target)
    else: raise ValueError("Неизвестный административный ввод.")
    app.db.set_settings(uid,f)
    logger.info("event=admin_action action=%s",kind)
    await update.effective_message.reply_text("Ответ отправлен автору, действие сохранено.")
