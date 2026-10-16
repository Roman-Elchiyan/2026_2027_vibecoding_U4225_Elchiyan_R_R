# Подготовка Lab 2/3 к GitHub

Рабочий режим зафиксирован: Windows-бот в polling, платный ИИ через LiteLLM, PostgreSQL в Docker. Исходный README, готовый отчёт и файлы Lab 1 сохранены. Коммит, tag, push и отправка файлов на GitHub не выполнялись; подготовленные изменения в индекс не добавлялись.

## Состав изменений для последующего просмотра

| Группа | Файлы |
| --- | --- |
| Запуск и безопасная конфигурация | `bot.py`, `requirements.txt`, `.env.example`, `.gitignore`, `.dockerignore`, `compose.yaml`, `litellm/config.yaml`, `deployment.py`, `lab_config.py` |
| Реализованные Lab 2/3 | `lab_storage.py`, `external_sources.py`, `external_semantic.py`, `research.py`, `translation.py`, `litellm_admin.py`, `research_ai.py`, `lab_handlers.py`, `lab_admin.py`, `ui_language.py` |
| Служебные команды | Python- и PowerShell-файлы `scripts/` |
| Проверки | `tests/lab2/test_budgets.py`, `tests/lab2/test_research.py`, `tests/lab3/test_deployment.py`, `tests/lab3/test_interface.py` |
| Задание, отчёты и запуск | `codex_lab2_lab3_FINAL_fast.md`, `lab2/`, `lab3/`, `docs/lab23_operations.md`, этот файл |

Существовавшая до разработки папка `lab1/lab1/images/` содержит три копии исходных изображений Lab 1. Она не изменялась и в список Lab 2/3 не включается. Другие ранее отслеживаемые посторонние файлы не удалялись.

## Что остаётся только локально

- `.env`, `env.lab23.template`, все сервисные env-файлы и персональные ключи;
- `runtime/`, SQLite, журналы, резервные копии, сырые тестовые протоколы и скачанный ngrok;
- `.venv`, кеши E5 и Argos, веса и векторы моделей;
- данные именованного PostgreSQL-тома Docker.

Проверены правила `.gitignore`, фактически отслеживаемые файлы, новые неигнорируемые файлы и доступная история Git. Совпадений с известными секретами не обнаружено. Для нового клона используется публичный `.env.example` с пустыми секретами; приватный `env.lab23.template` исключён из Git.

## Перед будущей отправкой

После отдельного разрешения владельца выбирайте только перечисленные группы и просмотрите `git diff --cached --stat` и содержимое будущего коммита. Не добавляйте игнорируемые файлы принудительно. Финальный протокол содержит агрегированные результаты; сырые логи, базы и локальные ключи к нему не прикладываются.

[Рабочая команда запуска](lab23_operations.md). [Результаты проверок](../lab3/evidence/final_checks.md).
