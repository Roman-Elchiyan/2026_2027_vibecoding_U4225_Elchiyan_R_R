# Рабочее развёртывание Lab 2/3: polling и платный ИИ

Все команды выполняются из корня проекта. Нужна существующая `.venv`; не переустанавливайте CUDA/PyTorch или модели. `.env` остаётся приватным. Сначала остановите ранее запущенный экземпляр бота: для запущенного агентом процесса — `powershell -NoProfile -ExecutionPolicy Bypass -File scripts/stop_bot.ps1`, для процесса в своём терминале — Ctrl+C. Блокировка `runtime/bot.lock` предотвращает второй экземпляр.

## Принятый запуск после перезагрузки ПК

Рабочий режим зафиксирован: **polling на Windows, платный GPT-6 Luna через LiteLLM, PostgreSQL в Docker**. Ngrok и webhook выключены и не требуются. Владелец подтвердил внешний поиск, ИИ, расходы и лимиты в Telegram.

Запустите Docker Desktop, дождитесь готовности движка и из корня проекта выполните:

```powershell
powershell -NoProfile -ExecutionPolicy Bypass -File scripts/run.ps1 -Mode polling -AI
```

Скрипт подготовит приватные env-файлы, запустит LiteLLM/PostgreSQL, дождётся healthcheck и запустит Windows-бота. В локальном `.env` сохранены `BOT_RUN_MODE=polling`, `ENABLE_AI=true`, `ENABLE_EXTERNAL_SOURCES=true`, `ENABLE_SOL_FALLBACK=false`. Основной алиас — `research-basic → openai/gpt-6-luna`; дорогого автоматического переключения нет. Пользовательская настройка `/ai` и административный доступ сохраняются в SQLite.

В терминале остановите бот через Ctrl+C. Windows `.venv` создаёт launcher и дочерний Python — это один экземпляр приложения; блокировка файла исключает второй. Для нового клона используйте `.env.example` с пустыми секретами. Не перезаписывайте существующий `.env`. Локальные веса и пары Argos в Git не входят; при отсутствии моделей остаётся поиск с доступными текстовыми механизмами.

[Финальные проверки](../lab3/evidence/final_checks.md). [Подготовка к GitHub](github_delivery.md).

## Бесплатный поиск без Docker и OpenAI

Это необязательный диагностический запуск; он временно отключает ИИ только в текущем PowerShell-процессе.

```powershell
$env:ENABLE_AI='false'
.\.venv\Scripts\python.exe bot.py --check --external
.\.venv\Scripts\python.exe bot.py --external --mode polling
```

`--external` включает научные API. В принятой конфигурации ИИ включён; при диагностическом запуске его отключает переменная выше. Для исходной реализации `bot.py --stage lab1` нужны исходные флаги Lab 1; её код и конфигурационный контракт сохранены.

## LiteLLM и PostgreSQL

```powershell
.\.venv\Scripts\python.exe scripts/prepare_services.py
docker compose config --quiet
docker compose up -d
docker compose ps
.\.venv\Scripts\python.exe scripts/service_status.py
```

Скрипт подготовки не изменяет `.env`. Постоянные сгенерированные секреты сохраняются в `runtime/services.env`, отдельные env-файлы передают каждому контейнеру только необходимые значения. Не удаляйте их и не меняйте соль при обычном перезапуске. OpenAI API key читается из локального `.env`; приватный восстановленный исходный шаблон также поддержан как резервный источник. После замены OpenAI-ключа повторите подготовку и `docker compose up -d --force-recreate litellm`.

```powershell
docker compose restart
docker compose down
```

Не добавляйте `-v`: это удалит том с ключами и расходами. Для сырых логов доступны `docker compose logs -f --tail 100 litellm` и аналогично `postgres`; при передаче диагностики предпочтителен `service_status.py`, который маскирует известные локальные секреты. Бот сохраняет собственные UTC-логи в `runtime/logs/bot.log` с ротацией 5 MiB + 3.

SQLite — `runtime/bot.db`; проверенная online backup перед подготовкой к GitHub — `runtime/backups/release-20261016.db`. База, резервная копия, логи и модели исключены из Git. Не удаляйте `runtime/services.env` и не меняйте соль при штатном перезапуске.

Для разрешения платного ИИ на время запуска:

```powershell
$env:ENABLE_AI='true'
.\.venv\Scripts\python.exe bot.py --external --mode polling
```

Затем пользователь самостоятельно включает ИИ через `/ai`. У администратора должны быть числовые ID в `ADMIN_TELEGRAM_IDS`. `/limits` показывает фактические данные Proxy. После замены недействующего ключа реальная генерация GPT-6 Luna проверена: HTTP 200, стоимость записана в spend и PostgreSQL, остаток уменьшился. Sol и web_search сейчас недоступны.

## Необязательный вариант: ngrok и webhook

По решению владельца эти компоненты в рабочем развёртывании выключены; ниже оставлена справка для отдельного выбора webhook в будущем. Authtoken передаётся из **локального** `.env` через окружение дочернего процесса, без аргумента с секретом. Предыдущая диагностика показала таймаут CRL/TLS (`ERR_NGROK_8003/8008`); это не блокирует принятый polling. Проверки сертификатов не отключались. При финальной проверке ngrok не запускался.

```powershell
powershell -NoProfile -ExecutionPolicy Bypass -File scripts/run.ps1 -Mode webhook
```

Скрипт подготовит приватные секреты, запустит локальный ngrok к порту 8080, бот получит HTTPS URL из `127.0.0.1:4040/api/tunnels`. `NGROK_PUBLIC_URL` можно оставить пустым; при заданном значении оно должно совпасть с агентом. Telegram webhook регистрируется только после preflight. Путь и secret token отдельные, случайные; LiteLLM никогда не туннелируется. При ошибке ngrok используется polling.

Ручная альтернатива:

```powershell
.\.venv\Scripts\python.exe scripts/ngrok_agent.py start
.\.venv\Scripts\python.exe bot.py --external --mode webhook
```

Для возврата остановите бот, остановите собственный агент и запустите polling:

```powershell
.\.venv\Scripts\python.exe scripts/ngrok_agent.py stop
.\.venv\Scripts\python.exe bot.py --external --mode polling
```

Перед polling бот сам вызывает `deleteWebhook(drop_pending_updates=False)` и проверяет пустой URL. Не запускайте отдельный `getUpdates` smoke параллельно работающему polling: это создаёт конфликт потребителей. Настоящую доставку проверяйте сообщением пользователя в Telegram, а не подставным update.

## Проверки

```powershell
$env:RUN_E5_TESTS='1'
.\.venv\Scripts\python.exe -m unittest discover -v
.\.venv\Scripts\python.exe -m unittest discover -s tests/lab2 -v
.\.venv\Scripts\python.exe -m unittest discover -s tests/lab3 -v
.\.venv\Scripts\python.exe -m pip check
.\.venv\Scripts\python.exe scripts/smoke.py --apis --translation
.\.venv\Scripts\python.exe scripts/live_search.py
.\.venv\Scripts\python.exe scripts/smoke.py --litellm --management
```

`--management` использует изолированного тестового пользователя, увеличивает его лимит один раз и проверяет повторное подтверждение. Добавление `--paid` явно запускает один платный микрозапрос. `--telegram` удаляет webhook и проверяет `getUpdates`: применяйте только при остановленном боте. Автоматические тесты никогда не используют настоящие API-ключи и не оплачивают генерацию.

В Telegram проверьте `/search` → русский запрос → источники/год → выдача → «Подробнее» → избранное → оценка/комментарий. «Сбросить фильтры» реально меняет следующий запрос, «Повторить поиск» не вызывает платный ИИ. `/admin` позволяет проверить пользователя, паузу, заявки и отзывы. После добавления ngrok authtoken отдельно подтвердите реальный входящий update и повторите переход обратно к polling.
