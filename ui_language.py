"""Static interface labels; publication titles/authors/DOIs are never translated here."""
from telegram import InlineKeyboardButton, InlineKeyboardMarkup, ReplyKeyboardMarkup

LABELS = {
    'Искать':'Search','Запрос':'Query','Годы':'Years','Тип':'Type','Источники':'Sources','Категория':'Category','Доступ':'Access',
    'Язык оригинала':'Original language','Язык интерфейса':'Interface language','Количество':'Result count','Порядок':'Order',
    'Сбросить фильтры':'Reset filters','Перевод карточек: да':'Translate cards: yes','Перевод карточек: нет':'Translate cards: no',
    'ИИ: вкл.':'AI: on','ИИ: выкл.':'AI: off','Оценка результата':'Rate results','Повторить поиск':'Retry search',
    'Назад':'Back','Далее':'Next','Русский перевод':'Russian translation','Оценить публикацию':'Rate publication',
    'Сохранить':'Save','Убрать из избранного':'Remove favorite','К исходной странице':'Back to results','Открыть источник':'Open source',
    'Любая':'Any','Любой':'Any','Научные':'Scientific','Аналитические':'Analytical','Открытый':'Open','Ограниченный':'Restricted',
    'Неизвестно':'Unknown','Русский':'Russian','Английский':'English','Готово':'Done','Релевантность источника':'Provider relevance',
    'Сначала новые':'Newest first','Всю выдачу':'Entire result set','Конкретную публикацию':'Specific publication',
    'Добавить комментарий':'Add comment','Запросить увеличение':'Request more budget','Выключить ИИ':'Disable AI','Включить ИИ':'Enable AI',
    'ИИ-поиск: вкл.':'AI search: on','ИИ-поиск: выкл.':'AI search: off',
    'Показать частично подходящие':'Show partially relevant', 'Комментарий':'Comment',
    'Без оценки':'No rating', 'Отправить отзыв':'Submit feedback', 'Отмена':'Cancel', 'Изменить отзыв':'Edit feedback',
    'Расширенный веб-поиск':'Extended web search',
    'Показать неподтверждённые':'Show unconfirmed candidates',
    'Предварительный ранжированный список':'Preliminary ranked candidates',
    'Измерения и допустимые коды':'Dimensions and allowed codes',
    'Получить значения':'Get observations','Территории':'Territories',
    'Измерения ряда':'Series dimensions','Точный ключ ряда':'Exact series key','Статистика':'Statistics',
    'Тема или название':'Topic or title','Тематический поиск':'Topic search','Поиск по названию':'Title search',
}


def markup_english(markup):
    if not isinstance(markup,InlineKeyboardMarkup): return markup
    rows = []
    for row in markup.inline_keyboard:
        translated = []
        for button in row:
            data = button.to_dict()
            text = data['text']
            if text.endswith('. Подробнее'): text = text.replace('. Подробнее','. Details')
            elif text.startswith(('✓ ', '○ ')) and text[2:] in ('Работа бота','Результаты поиска','Конкретная публикация'):
                text = text[:2] + {'Работа бота':'Bot behavior','Результаты поиска':'Search results','Конкретная публикация':'Specific publications'}[text[2:]]
            else: text = LABELS.get(text,text)
            data['text'] = text
            translated.append(InlineKeyboardButton(**data))
        rows.append(translated)
    return InlineKeyboardMarkup(rows)


ENGLISH_MENU = ReplyKeyboardMarkup([['🔎 Search materials','⭐ Favorites'],['🤖 AI search','📊 My budget'],['📝 Feedback','❔ Help']],resize_keyboard=True)
