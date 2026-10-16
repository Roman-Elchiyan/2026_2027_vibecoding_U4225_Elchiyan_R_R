"""Federated free search; the original local search remains intact."""
import asyncio
from dataclasses import asdict, dataclass, replace
import logging
import re
from uuid import uuid4

from search import SearchFilters, SearchResult, deduplicate, normalize_doi
from external_sources import MetadataHTTP, SOURCE_NAMES, adapters

logger = logging.getLogger(__name__)


@dataclass
class ResearchFilters(SearchFilters):
    sources: tuple[str,...] = ("local","openalex","crossref","datacite","worldbank")
    category: str = "any"
    access_status: str = "any"
    max_results: int = 20
    translate_cards: bool = False
    ui_language: str = "ru"

    def validate(self):
        SearchFilters(topic=self.topic,year_from=self.year_from,year_to=self.year_to).validate()
        if not self.sources or set(self.sources)-SOURCE_NAMES.keys():
            raise ValueError("Выберите хотя бы один известный источник.")
        if self.category not in ("any","scientific","analytical") or self.access_status not in ("any","open","restricted","unknown"):
            raise ValueError("Некорректные фильтры категории/доступа.")
        if self.language not in (None,"ru","en") or self.ui_language not in ("ru","en"):
            raise ValueError("Поддерживаются только ru/en.")
        if self.max_results not in (5,10,20,50) or self.sort not in ("source","year"):
            raise ValueError("Допустимы 5/10/20/50 результатов; порядок: релевантность источника или год.")

    @classmethod
    def from_dict(cls,data):
        data = {k:v for k,v in data.items() if k in cls.__dataclass_fields__}
        for key in ("material_types","sources"):
            if key in data: data[key] = tuple(data[key])
        return cls(**data)


def active_filters(f):
    if f.ui_language == 'en':
        years = f"{f.year_from or '…'}–{f.year_to or '…'}" if f.year_from or f.year_to else 'any year'
        return (f"Filters: {years}; {', '.join(SOURCE_NAMES[s] for s in f.sources)}; type: {', '.join(f.material_types) or 'any'}; "
            f"category: {f.category}; access: {f.access_status}; original language: {f.language or 'any'}; up to {f.max_results}.")
    years = f"{f.year_from or '…'}–{f.year_to or '…'}" if f.year_from or f.year_to else "любые годы"
    return (f"Фильтры: {years}; {', '.join(SOURCE_NAMES[s] for s in f.sources)}; "
        f"тип: {', '.join(f.material_types) or 'любой'}; категория: {f.category}; "
        f"доступ: {f.access_status}; язык оригинала: {f.language or 'любой'}; до {f.max_results}.")


def matches(item,f):
    language = str(item.metadata.get("language") or "").lower()
    language = {"eng":"en","rus":"ru"}.get(language,language)
    category = item.metadata.get("category") or ("scientific" if item.metadata.get("paperid") else None)
    return ((f.year_from is None or item.year is not None and item.year >= f.year_from)
        and (f.year_to is None or item.year is not None and item.year <= f.year_to)
        and (not f.material_types or item.material_type in f.material_types)
        and (f.language is None or language == f.language)
        and (f.category == "any" or category == f.category)
        and (f.access_status == "any" or (item.access_status or "unknown") == f.access_status))


class ResearchService:
    def __init__(self,local,db,config,translator,http=None):
        self.local,self.db,self.config,self.translator = local,db,config,translator
        self.http = http or MetadataHTTP(db,config)
        self.sources = adapters(self.http)

    async def search(self,f):
        f.validate()
        doi = normalize_doi(f.topic)
        doi_like = bool(re.match(r"^(?:https?://(?:dx\.)?doi\.org/|doi:|10\.\d{4,9}/)",f.topic.strip(),re.I))
        if doi_like and not doi:
            return SearchResult([], ["DOI имеет неверный формат; похожие публикации не подставляются."],uuid4().hex,0)
        query, warning = (f.topic,"") if doi else await self.translator.query(f.topic)
        warnings = [warning] if warning else []
        if len(query)>500:
            query = query[:500]
            warnings.append("Переведённый запрос ограничен 500 символами.")
        batches = []
        if "local" in f.sources:
            original = SearchFilters(topic=f.topic,year_from=f.year_from,year_to=f.year_to,material_types=f.material_types)
            local = await self.local.async_search(original)
            warnings.extend(local.warnings)
            items = local.items
            if query != f.topic:
                translated = await asyncio.to_thread(self.local.search, replace(original,topic=query))
                items = deduplicate(items+translated.items)
            batches.append(items)
        external = [s for s in f.sources if s != "local"]
        if external and not self.config.external_sources_enabled:
            warnings.append("Внешние источники отключены в настройках запуска. Доступен локальный CSV.")
        else:
            results = await asyncio.gather(*(self.sources[s].search(query,doi,min(f.max_results,50)) for s in external),return_exceptions=True)
            for name,result in zip(external,results):
                if isinstance(result,BaseException):
                    logger.warning("event=source_search_failed source=%s error=%s",name,type(result).__name__)
                    warnings.append(f"{SOURCE_NAMES[name]} временно недоступен. Результаты остальных источников сохранены. Нажмите «Повторить поиск».")
                else:
                    batches.append(result)
        # Interleave provider rankings so a large local catalog cannot hide all APIs.
        items = [batch[i] for i in range(max(map(len,batches),default=0)) for batch in batches if i < len(batch)]
        if doi:
            items = [m for m in items if normalize_doi(m.doi) == doi]
        items = [m for m in items if matches(m,f)]
        # Retain every provenance record in SQLite even when the DOI is shared.
        with self.db.connect() as c:
            for item in items: self.db._store_material(c,item)
        items = deduplicate(items)
        if self.local.semantic and self.local.semantic.external_enabled and not doi:
            from external_semantic import rerank
            items,warning = await rerank(self.local.semantic,self.db,f.topic,items)
            if warning: warnings.append(warning)
        if f.sort == "year": items.sort(key=lambda m:m.year or 0,reverse=True)
        items = items[:f.max_results]
        return SearchResult(items,list(dict.fromkeys(warnings)),uuid4().hex,len(items))

    async def close(self):
        await self.http.close()
        self.translator.close()
