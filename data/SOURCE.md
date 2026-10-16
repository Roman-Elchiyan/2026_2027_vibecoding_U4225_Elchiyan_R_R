# Происхождение ploswater.csv

- Проект: [openwashdata/washopenresearch](https://github.com/openwashdata/washopenresearch), организация **openwashdata**.
- Авторы версии 0.5.0: **Mian Zhong, Ludwig Luz, Lars Schöbitz, Yash Dubey**; источники: сохранённые рядом `upstream-description.txt` и `upstream-citation.cff`.
- [Исходный CSV](https://raw.githubusercontent.com/openwashdata/washopenresearch/main/inst/extdata/ploswater.csv).
- Последняя изменившая CSV ревизия при скачивании: `656df291609d63c6002ffd83f494487ba6d3fe0a`.
- [CSV в закреплённой ревизии](https://raw.githubusercontent.com/openwashdata/washopenresearch/656df291609d63c6002ffd83f494487ba6d3fe0a/inst/extdata/ploswater.csv).
- Фактическая дата скачивания: **16.10.2026**, Europe/Moscow.
- Размер: **950187 байт**. CSV скачан без перекодирования и модификаций. Правило data/ploswater.csv -text в .gitattributes запрещает Git менять окончания строк при core.autocrlf=true.
- SHA-256: `20d8c589f616870cfa9be75b9db022bb35813817dc3df4a1a5f42a50b94d841c`.
- Лицензия данных: [CC BY 4.0](https://creativecommons.org/licenses/by/4.0/), [лицензия проекта](https://github.com/openwashdata/washopenresearch/blob/main/LICENSE.md).
- При распространении указывайте авторов, проект, источник и лицензию, сохраняйте уведомления об авторстве и обозначайте изменения. CSV не изменён; преобразование метаданных происходит в памяти и снимках SQLite. MIT корневого репозитория относится к коду и не заменяет CC BY для данных.
- Рекомендуемая ссылка из CITATION.cff: Zhong M., Luz L., Schöbitz L., Dubey Y. (2026). *washopenresearch: Dataset about open research data information in Water, Sanitation, and Hygiene*, v0.5.0. [DOI 10.5281/zenodo.11185699](https://doi.org/10.5281/zenodo.11185699).

## Проверенная схема

UTF-8, разделитель запятая, стандартные CSV-кавычки. При скачивании прочитаны **434 строки данных**, годы 2022–2026. Это описание снимка: приложение вычисляет количество из файла при каждом чтении.

Фактические заголовки:

```text
paperid,volume,issue,paper_url,journal,title,published_year,is_supp,num_supp,supp_file_type,supp_url,num_authors,first_author_name,first_author_affiliation,first_author_affiliation_country,first_author_orcid,correspondence_author_name,correspondence_author_affiliation,correspondence_author_affiliation_country,correspondence_author_orcid,has_das,das,das_type,das_repo_url,das_repo_name,keywords,url_source,doi,article_type,publication_date
```

`article_type`: Research Article, Opinion, Review, Correction, Essay, Editorial. Это типы публикаций; дополнительных наборов данных или отчётов бот не придумывает.

`first_author_name` содержит только первого автора. `das` — заявление о доступности исследовательских данных, **не аннотация**. `keywords` — ключевые слова. `NA` преобразуется в `None`. Языка, аннотации, проверенного открытого доступа и ссылки на полный текст в схеме нет; эти поля не вычисляются из догадок.

Примеры из загруженного файла:

- *Water remains a blind spot in climate change policies*, Hervé Douville (первый автор), 2022, Review, DOI `10.1371/journal.pwat.0000058`.
- *Understanding household self-supply use and management using a mixed-methods approach in urban Indonesia*, Franziska Genter (первый автор), 2023, Research Article, DOI `10.1371/journal.pwat.0000070`.
