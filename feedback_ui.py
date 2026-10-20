"""One owned draft and one feedback record for any combination of categories."""
from uuid import uuid4

from telegram import InlineKeyboardButton as B, InlineKeyboardMarkup as K

from handlers import clipped

CATEGORY_LABELS = {'bot': 'Работа бота', 'results': 'Результаты поиска', 'publication': 'Конкретная публикация'}


class FeedbackForm:
    def _draft_key(self, uid, nonce):
        return f'feedback-draft:{uid}:{nonce}'

    def _draft(self, uid, nonce):
        draft = self.db.cache_get(self._draft_key(uid, nonce))
        if not draft:
            raise ValueError('Форма недоступна или истекла. Откройте /feedback.')
        return draft

    def _put_draft(self, uid, nonce, draft):
        self.db.cache_put(self._draft_key(uid, nonce), draft, 3600)

    def _feedback_search(self, uid, draft):
        sid = draft.get('search_id')
        if not sid:
            with self.db.connect() as c:
                row = c.execute("SELECT id FROM searches WHERE user_id=? AND kind='search' ORDER BY created_at DESC,rowid DESC LIMIT 1", (uid,)).fetchone()
            if not row:
                raise ValueError('Для этой категории сначала выполните /search. Общий отзыв о боте доступен без поиска.')
            sid = row[0]
        self.db.get_search(uid, sid)
        draft['search_id'] = sid
        return sid

    async def feedback_open(self, message, uid, sid=None, position=None, feedback_id=None):
        draft = dict(categories=['results'] if sid else ['bot'], search_id=sid,
                     positions=[], material_ids=[], rating=None, comment='', feedback_id=feedback_id)
        if feedback_id:
            record = self.db.feedback_get(feedback_id)
            if record['user_id'] != uid:
                raise ValueError('Чужой отзыв.')
            draft.update(categories=record['categories'], search_id=record['search_id'],
                positions=None, material_ids=record['material_ids'], rating=record['rating'], comment=record['comment'])
        elif sid:
            self.db.get_search(uid, sid)
            if position is not None:
                self.db.result_item(uid, sid, position)
                draft.update(categories=['publication'], positions=[position])
        nonce = uuid4().hex[:16]
        self._put_draft(uid, nonce, draft)
        await self.feedback_render(message, uid, nonce)

    async def feedback_render(self, message, uid, nonce):
        draft = self._draft(uid, nonce)
        rows = [[B(('✓ ' if key in draft['categories'] else '○ ') + label,
            callback_data=f'fb:cat:{nonce}:{key}')] for key, label in CATEGORY_LABELS.items()]
        selected = len(draft.get('positions') or draft.get('material_ids') or [])
        if 'publication' in draft['categories']:
            rows.append([B(f'Выбрать публикации ({selected})', callback_data=f'fb:pick:{nonce}:0')])
        rows += [[B('Комментарий', callback_data=f'fb:comment:{nonce}')],
            [B(('✓ ' if draft['rating'] == n else '') + str(n), callback_data=f'fb:rate:{nonce}:{n}') for n in range(1, 6)],
            [B('Без оценки', callback_data=f'fb:rate:{nonce}:0')],
            [B('Отправить отзыв', callback_data=f'fb:submit:{nonce}'), B('Отмена', callback_data=f'fb:cancel:{nonce}')]]
        if not draft.get('feedback_id'):
            rows += [[B('Изменить: ' + clipped(r['comment'] or 'Отзыв с оценкой', 35),
                callback_data='fb:edit:' + r['id'])] for r in self.db.feedback_for_user(uid)]
        text = ('Один отзыв: выберите одну, две или все три категории.\n'
            'Комментарий общий; оценка 1–5 по желанию. До отправки запись не создаётся.\n'
            f"Оценка: {draft['rating'] or 'без оценки'}. Публикаций: {selected}.\n"
            'Комментарий: ' + clipped(draft['comment'] or 'ещё не добавлен', 500))
        if self.filters(uid)[0].ui_language=='en':
            text=('One feedback record: select one, two or all three categories.\n'
                'One shared comment is required; a 1–5 rating is optional. Nothing is saved before submission.\n'
                f"Rating: {draft['rating'] or 'none'}. Publications: {selected}.\n"
                'Comment: '+clipped(draft['comment'] or 'not added yet',500))
        await self.say(message, uid, text, K(rows))

    async def feedback_comment(self, message, uid, nonce, text):
        if not 1 <= len(text.strip()) <= 2000:
            raise ValueError('Комментарий от 1 до 2000 символов.')
        draft = self._draft(uid, nonce)
        draft['comment'] = text.strip()
        self._put_draft(uid, nonce, draft)
        f, _ = self.filters(uid)
        self.db.set_settings(uid, f)
        await self.feedback_render(message, uid, nonce)

    async def feedback_callback(self, q, uid, data):
        parts = data.split(':')
        action, nonce = parts[1:3]
        if action == 'edit':
            return await self.feedback_open(q.message, uid, feedback_id=nonce)
        draft = self._draft(uid, nonce)
        if draft.get('submitted'):
            return await q.message.reply_text('Этот отзыв уже сохранён. /feedback — новый или редактирование.')
        if action == 'cat':
            category = parts[3]
            if category not in CATEGORY_LABELS:
                raise ValueError('Неизвестная категория.')
            selected = set(draft['categories'])
            selected.symmetric_difference_update({category})
            if category in selected and category != 'bot':
                self._feedback_search(uid, draft)
            draft['categories'] = [c for c in CATEGORY_LABELS if c in selected]
            if 'publication' not in selected:
                draft.update(positions=[], material_ids=[])
        elif action == 'comment':
            f, _ = self.filters(uid)
            self.db.set_settings(uid, f, 'fb_comment:' + nonce)
            return await q.message.reply_text('Введите один общий комментарий до 2000 символов. /cancel — отменить ввод.')
        elif action == 'rate':
            value = int(parts[3])
            if value not in range(6):
                raise ValueError('Оценка 1–5 или без оценки.')
            draft['rating'] = value or None
        elif action in ('pick', 'item'):
            sid = self._feedback_search(uid, draft)
            if 'publication' not in draft['categories']:
                raise ValueError('Сначала выберите категорию публикации.')
            if action == 'item':
                position, page = int(parts[3]), int(parts[4])
                self.db.result_item(uid, sid, position)
                positions = draft['positions']
                if positions is None:
                    record = self.db.get_search(uid, sid)
                    positions = [p for p in range(record['total_count'])
                        if self.db.result_item(uid, sid, p).id in draft['material_ids']]
                if position in positions:
                    positions.remove(position)
                elif len(positions) < 10:
                    positions.append(position)
                else:
                    raise ValueError('Не более десяти публикаций в одном отзыве.')
                draft.update(positions=sorted(positions), material_ids=[])
                self._put_draft(uid, nonce, draft)
            else:
                page = int(parts[3])
            record = self.db.get_search(uid, sid)
            items = self.db.get_page(uid, sid, page)
            rows = [[B(('✓ ' if p in (draft['positions'] or []) or m.id in draft['material_ids'] else '○ ')
                + f'{p + 1}. ' + clipped(m.title, 45), callback_data=f'fb:item:{nonce}:{p}:{page}')] for p, m in items]
            if page > 0: rows.append([B('Назад', callback_data=f'fb:pick:{nonce}:{page - 1}')])
            if (page + 1) * 5 < record['total_count']: rows.append([B('Далее', callback_data=f'fb:pick:{nonce}:{page + 1}')])
            rows.append([B('Готово', callback_data=f'fb:form:{nonce}')])
            self._put_draft(uid, nonce, draft)
            return await self.say(q.message, uid, 'Выберите публикации и нажмите «Готово».', K(rows))
        elif action == 'submit':
            if not draft['comment'].strip():
                raise ValueError('Добавьте общий комментарий перед отправкой.')
            fid = self.db.feedback_submit(uid, draft['categories'], draft['comment'], draft['rating'],
                draft['search_id'], draft['positions'], draft['feedback_id'])
            draft['submitted'] = fid
            self._put_draft(uid, nonce, draft)
            f, _ = self.filters(uid)
            self.db.set_settings(uid, f)
            return await q.message.reply_text('Отзыв сохранён одной записью. Спасибо.',
                reply_markup=K([[B('Изменить отзыв', callback_data='fb:edit:' + fid)]]))
        elif action == 'cancel':
            draft['submitted'] = 'cancelled'
            self._put_draft(uid, nonce, draft)
            f, _ = self.filters(uid)
            self.db.set_settings(uid, f)
            return await q.message.reply_text('Отзыв не отправлен.')
        elif action != 'form':
            raise ValueError('Форма устарела.')
        self._put_draft(uid, nonce, draft)
        await self.feedback_render(q.message, uid, nonce)
