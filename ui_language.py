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
            else: text = LABELS.get(text,text)
            data['text'] = text
            translated.append(InlineKeyboardButton(**data))
        rows.append(translated)
    return InlineKeyboardMarkup(rows)


ENGLISH_MENU = ReplyKeyboardMarkup([['🔎 Search materials','⭐ Favorites'],['🤖 AI search','📊 My budget'],['📝 Feedback','❔ Help']],resize_keyboard=True)
