# Локальные проверки — 16.10.2026

Среда: Windows PowerShell, Python 3.13.15, python-telegram-bot 22.8, python-dotenv 1.2.2.

```text
python -B -m unittest discover -s tests -v
Ran 36 tests
OK

python -B bot.py --check
Этап: Lab 1. Процесс бота запущен.
SQLite: проверка пройдена.
CSV: существует и прочитан; строк 434, материалов 434.
Внешние API и ИИ: не подключены.
Telegram: отдельная проверка соединения getMe не выполнялась.
exit=0

python -B bot.py
Задайте BOT_TOKEN из BotFather в .env. Значение токена не выводится.
exit=2 (ожидаемо: токен отсутствует)

python -B -m pip check
No broken requirements found.

git diff --check
exit=0

Сравнение исходного CSV с закреплённой ревизией:
upstream_revision_bytes_equal True
sha256 20d8c589f616870cfa9be75b9db022bb35813817dc3df4a1a5f42a50b94d841c
```

Это краткий текстовый протокол выполненных проверок, не скриншот и не подтверждение работы в живом Telegram. HTTP-транспорт Telegram в тестах локальный; публикации читаются из оригинального CSV. Проверка подключения к Telegram требует настоящего токена и пока не выполнена.
