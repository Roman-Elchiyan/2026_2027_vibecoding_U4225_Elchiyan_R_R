"""Federated free search; the original local search remains intact."""
import asyncio
from dataclasses import asdict, dataclass, field, replace
import logging
import re
from uuid import uuid4
from time import perf_counter

from search import SearchFilters, SearchResult, deduplicate, normalize_doi
from external_sources import MetadataHTTP, SOURCE_NAMES, adapters, REQUEST_STATS, SOURCE_PARTIALS, AccessEnricher, identifier_query, SourceUnavailable
from statistics_sources import STAT_SOURCES
from query_planner import analyze_query, source_query, is_structured, title_query
from relevance import classify, judge, load_calibration, material_text, merge_candidates
from reranker import CrossEncoderRanker, MODEL

logger = logging.getLogger(__name__)


@dataclass
class ResearchFilters(SearchFilters):
    sources: tuple[str,...] = ("local","openalex","crossref","datacite","worldbank")
    category: str = "any"
    access_status: str = "any"
    max_results: int = 20
    translate_cards: bool = False
    ui_language: str = "ru"
    ai_search: bool = False
    territories: tuple[str,...] = ()
    statistics_key: str = ""
    statistics_dimensions: dict = field(default_factory=dict)
    query_mode: str = 'topic'

    def validate(self):
        SearchFilters(topic=self.topic,year_from=self.year_from,year_to=self.year_to).validate()
        if not self.sources or set(self.sources)-SOURCE_NAMES.keys():
            raise ValueError("Выберите хотя бы один известный источник.")
        if self.category not in ("any","scientific","analytical","statistics") or self.access_status not in ("any","open","restricted","unknown"):
            raise ValueError("Некорректные фильтры категории/доступа.")
        if self.language not in (None,"ru","en") or self.ui_language not in ("ru","en"):
            raise ValueError("Поддерживаются только ru/en.")
        if self.max_results not in (5,10,20,50) or self.sort not in ("source","year"):
            raise ValueError("Допустимы 5/10/20/50 результатов; порядок: релевантность источника или год.")
        if type(self.ai_search) is not bool or type(self.ai_enabled) is not bool:
            raise ValueError('Переключатели ИИ должны быть true/false.')
        if self.query_mode not in ('topic','title'):raise ValueError('Режим поиска: topic или title.')
        if not isinstance(self.statistics_key,str) or len(self.statistics_key)>500:
            raise ValueError('Ключ статистического ряда: до 500 символов.')
        if len(self.territories)>10 or any(not re.fullmatch(r'[A-Za-z0-9_-]{1,8}',c) for c in self.territories):
            raise ValueError('Укажите до десяти кодов территорий.')
        if not isinstance(self.statistics_dimensions,dict) or len(self.statistics_dimensions)>12:
            raise ValueError('Некорректные измерения статистики.')
        if any(not re.fullmatch(r'[A-Za-z0-9_-]{1,60}',str(k)) or not re.fullmatch(r'[A-Za-z0-9_+.*,:-]{1,150}',str(v)) for k,v in self.statistics_dimensions.items()):
            raise ValueError('Измерения задаются кодами источника.')

    @classmethod
    def from_dict(cls,data):
        data = {k:v for k,v in data.items() if k in cls.__dataclass_fields__}
        for key in ("material_types","sources","territories"):
            if key in data: data[key] = tuple(data[key])
        return cls(**data)


def active_filters(f):
    statistical=f.category=='statistics' or bool(set(f.sources)&STAT_SOURCES)
    extra=''
    if statistical:
        dimensions='; '.join(f'{k}={v}' for k,v in f.statistics_dimensions.items()) or ('none' if f.ui_language=='en' else 'не заданы')
        extra=(f" Territories: {', '.join(f.territories) or 'none'}; dimensions: {dimensions}; series key: {f.statistics_key or 'none'}."
            if f.ui_language=='en' else f" Территории: {', '.join(f.territories) or 'не заданы'}; измерения: {dimensions}; ключ: {f.statistics_key or 'не задан'}.")
    if f.ui_language == 'en':
        years = f"{f.year_from or '…'}–{f.year_to or '…'}" if f.year_from or f.year_to else 'any year'
        return (f"Filters: {years}; {', '.join(SOURCE_NAMES[s] for s in f.sources)}; type: {', '.join(f.material_types) or 'any'}; "
            f"category: {f.category}; mode: {f.query_mode}; access: {f.access_status}; original language: {f.language or 'any'}; up to {f.max_results}; AI search: {'on' if f.ai_enabled and f.ai_search else 'off'}.")+extra
    years = f"{f.year_from or '…'}–{f.year_to or '…'}" if f.year_from or f.year_to else "любые годы"
    return (f"Фильтры: {years}; {', '.join(SOURCE_NAMES[s] for s in f.sources)}; "
        f"тип: {', '.join(f.material_types) or 'любой'}; категория: {f.category}; режим: {'название' if f.query_mode=='title' else 'тема'}; "
        f"доступ: {f.access_status}; язык оригинала: {f.language or 'любой'}; до {f.max_results}; ИИ-поиск: {'вкл.' if f.ai_enabled and f.ai_search else 'выкл.'}.")+extra


def matches(item,f,allow_unknown=False):
    language = str(item.metadata.get("language") or "").lower()
    language = {"eng":"en","rus":"ru"}.get(language,language)
    category = item.metadata.get("category") or ("scientific" if item.metadata.get("paperid") else None)
    def type_key(value):return str(value).casefold().replace('-',' ')
    publication_types=item.metadata.get('publication_types') or []
    if isinstance(publication_types,str):publication_types=[publication_types]
    kinds={type_key(t) for t in publication_types}
    if item.material_type:kinds.add(type_key(item.material_type))
    matching_type=bool(kinds & {type_key(t) for t in f.material_types})
    if category=='statistics':
        return (f.category in ('any','statistics') and not f.material_types
            and f.language is None
            and f.access_status in ('any','open')
            and (item.metadata.get('statistical_kind')=='dataset' or
                (f.year_from is None or item.year is not None and item.year>=f.year_from)
                and (f.year_to is None or item.year is not None and item.year<=f.year_to)))
    return ((f.year_from is None or item.year is not None and item.year >= f.year_from or allow_unknown and item.year is None)
        and (f.year_to is None or item.year is not None and item.year <= f.year_to or allow_unknown and item.year is None)
        and (not f.material_types or matching_type or allow_unknown and not kinds)
        and (f.language is None or language == f.language or allow_unknown and not language)
        and (f.category == "any" or category == f.category)
        and (not f.open_only or item.access_status == 'open' or allow_unknown and not item.access_status)
        and (f.access_status == "any" or (item.access_status or "unknown") == f.access_status or allow_unknown and not item.access_status))


class ResearchService:
    def __init__(self,local,db,config,translator,http=None):
        self.local,self.db,self.config,self.translator = local,db,config,translator
        self.http = http or MetadataHTTP(db,config)
        self.sources = adapters(self.http)
        self.reranker = CrossEncoderRanker(config)
        self.access = AccessEnricher(self.http)

    async def _fetch(self, jobs, deadline, warnings):
        """Keep every finished provider; cancel only unfinished metadata GETs."""
        if not jobs or perf_counter() >= deadline:
            return []
        async def fetch(name, adapter, query, doi, limit, filters, retained):
            token=SOURCE_PARTIALS.set(retained)
            try:
                rendered=query if doi else title_query(query,name) if filters.query_mode=='title' else source_query(query,name)
                return await adapter.search(rendered, doi, limit, filters=filters)
            finally:SOURCE_PARTIALS.reset(token)
        tasks={}
        for name,adapter,query,doi,limit,f in jobs:
            retained=[]
            tasks[asyncio.create_task(fetch(name,adapter,query,doi,limit,f,retained))]=(name,retained)
        try:
            done, pending = await asyncio.wait(tasks, timeout=max(0, deadline - perf_counter()))
        except asyncio.CancelledError:
            for task in tasks: task.cancel()
            await asyncio.gather(*tasks, return_exceptions=True)
            raise
        for task in pending:
            task.cancel()
        if pending:
            await asyncio.gather(*pending, return_exceptions=True)
            warnings.append('Достигнут срок поиска; сохранены ответы завершившихся источников.')
        batches = []
        for task, (name,retained) in tasks.items():
            if task not in done:
                if retained:batches.append(retained)
                continue
            try:
                batches.append(task.result())
            except Exception as exc:
                if retained:batches.append(retained)
                logger.warning('event=source_search_failed source=%s error=%s', name, type(exc).__name__)
                detail = f': {exc}' if isinstance(exc, SourceUnavailable) else ' временно недоступен.'
                warnings.append(f'{SOURCE_NAMES[name]}{detail} Результаты остальных источников сохранены. Нажмите «Повторить поиск».')
        return batches

    async def search(self, f, plan=None, reviewer=None):
        f.validate()
        if not (f.ai_enabled and f.ai_search and self.config.ai_enabled):
            plan = reviewer = None
        started = perf_counter()
        deadline = started + self.config.search_timeout
        stats = {'http_requests': 0, 'cache_hits': 0, 'logical_requests': 0}
        token = REQUEST_STATS.set(stats)
        try:
            return await self._search(f, plan, reviewer, started, deadline, stats)
        finally:
            REQUEST_STATS.reset(token)

    async def _search(self, f, plan, reviewer, started, deadline, stats):
        doi = normalize_doi(f.topic)
        identifier_kind, identifier = identifier_query(f.topic)
        exact = bool(doi or identifier_kind or f.query_mode=='title')
        doi_like = bool(re.match(r'^(?:https?://(?:dx\.)?doi\.org/|doi:|10\.\d{4,9}/)', f.topic.strip(), re.I))
        if doi_like and not doi:
            return SearchResult([], ['DOI имеет неверный формат; похожие публикации не подставляются.'], uuid4().hex, 0)
        if f.query_mode=='topic' and not identifier_kind and re.match(r'^(?:PMID|PMCID|arxiv)\s*:',f.topic,re.I):
            return SearchResult([],['Идентификатор имеет неверный формат; тематические результаты не подставляются.'],uuid4().hex,0)
        try:
            query, warning = (f.topic, '') if exact or is_structured(f.topic) else await asyncio.wait_for(
                self.translator.query(f.topic),max(.01,deadline-perf_counter()))
        except asyncio.TimeoutError:
            query,warning=f.topic,'Перевод превысил общий срок поиска; сохранён исходный запрос.'
        warnings = [warning] if warning else []
        if len(query) > 500:
            query = query[:500]
            warnings.append('Переведённый запрос ограничен 500 символами.')
        intent = analyze_query(f.topic, query, plan, self.config.query_variants) if not exact else None
        if intent and intent.structured is not None and set(f.sources)&{'openalex','crossref','worldbank','local'}:
            warnings.append('Для части источников поля и Boolean проверяются по доступным метаданным; полнота серверного отбора не гарантируется.')
        queries = intent.variants if intent else (query,)
        batches = []
        semantic = self.local.semantic
        self.reranker.semantic = semantic
        if 'local' in f.sources and f.material_types and not self.local.source.supports_types:
            warnings.append('В CSV тип материала не указан; строгий фильтр исключил локальные записи.')
        elif 'local' in f.sources and not identifier_kind:
            original = SearchFilters(topic=f.topic, year_from=f.year_from, year_to=f.year_to, material_types=f.material_types)
            # One broad E5 retrieval instead of a GPU query for every variant.
            for variant in dict.fromkeys((f.topic,) + tuple(queries)):
                if perf_counter()>=deadline:break
                local = await asyncio.to_thread(self.local.search, replace(original, topic=variant))
                batches.append(local.items)
            if semantic and not doi and perf_counter()<deadline:
                from external_semantic import local_candidates
                allowed = await asyncio.to_thread(self.local._candidates, original)
                try:
                    ids,note=await asyncio.wait_for(local_candidates(semantic,f.topic,{m.id for m in allowed},self.config.reranker_candidates),
                        max(.01,deadline-perf_counter()))
                except asyncio.TimeoutError:ids,note=None,'Достигнут общий срок поиска на локальной E5.'
                by_id = {m.id: m for m in allowed}
                if ids: batches.append([by_id[mid] for mid in ids if mid in by_id])
                if note: warnings.append(note)
        if f.language is not None and set(f.sources)&STAT_SOURCES:
            warnings.append('Фильтр языка публикаций неприменим к статистическим рядам; они исключены. Для статистики выберите любой язык оригинала.')
        external = [s for s in f.sources if s != 'local' and (f.category!='statistics' or s in STAT_SOURCES)
            and not (s in STAT_SOURCES and f.language is not None)]
        if external and not self.config.external_sources_enabled:
            warnings.append('Внешние источники отключены в настройках запуска. Доступен локальный CSV.')
        else:
            per_request = min(100, max(self.config.api_candidates, f.max_results * 2))
            if identifier_kind:
                external=[s for s in external if s in (('arxiv',) if identifier_kind=='arxiv' else ('pubmed','europepmc'))]
            jobs = [(s, self.sources[s], variant, doi, 1 if doi or identifier_kind else per_request, f)
                for s in external for variant in (queries[:1] if s in STAT_SOURCES else queries)]
            batches += await self._fetch(jobs, deadline, warnings)
        # Store provenance before merging DOI records, never generated records.
        with self.db.connect() as c:
            for batch in batches:
                for item in batch:
                    self.db._store_material(c, item)
        if self.config.external_sources_enabled:
            lengths = [len(b) for b in batches]
            converted = await self.access.identifiers([m for batch in batches for m in batch], deadline)
            offset = 0
            for i, length in enumerate(lengths):
                batches[i] = converted[offset:offset+length]
                offset += length
        items = merge_candidates(batches, collapse_versions=False)
        if doi:
            items = [m for m in items if normalize_doi(m.doi) == doi]
        if f.query_mode=='title' and not doi and not identifier_kind:
            from rapidfuzz.fuzz import ratio
            def normalized(value):return ' '.join(re.findall(r'[^\W_]+',value.casefold()))
            items=[m for m in items if ratio(normalized(m.title),normalized(f.topic))>=95]
        items = [m for m in items if matches(m, f,allow_unknown=not exact)][:self.config.search_candidates]
        candidate_count = len(items)
        if not doi and self.config.external_sources_enabled:
            missing = [m for m in items if m.doi and not m.abstract_or_snippet][:self.config.enrich_dois]
            lookups = []
            for item in missing:
                for name in ('openalex', 'crossref', 'datacite'):
                    if name in external and item.source.casefold() != name:
                        lookups.append((name, self.sources[name], item.doi, item.doi, 1, f))
                        break
            enriched = await self._fetch(lookups, deadline, warnings)
            if enriched:
                allowed_dois = {normalize_doi(m.doi) for m in missing}
                enriched = [[m for m in batch if normalize_doi(m.doi) in allowed_dois] for batch in enriched]
                items = [m for m in merge_candidates([items] + enriched, collapse_versions=False) if matches(m, f,allow_unknown=True)]
        if self.config.external_sources_enabled:
            items,access_notes=await self.access.enrich(items,deadline)
            warnings.extend(access_notes)
        items=[m for m in items if matches(m,f)]
        if not doi and not identifier_kind:
            # Select a version only after strict year/access/language filters.
            items = merge_candidates([items])
        statistical=[m for m in items if m.metadata.get('category')=='statistics']
        items=[m for m in items if m.metadata.get('category')!='statistics']
        if identifier_kind:
            field={'pmid':'pmid','pmcid':'pmcid','arxiv':'arxiv_id'}[identifier_kind]
            items=[m for m in items if (re.sub(r'v\d+$','',str(m.metadata.get(field,''))) == identifier
                if identifier_kind=='arxiv' and not re.search(r'v\d+$',identifier)
                else str(m.metadata.get(field,''))==identifier)]
        e5_metrics={}
        if semantic and semantic.external_enabled and not exact and perf_counter()<deadline:
            from external_semantic import rerank
            e5_started=perf_counter()
            try:
                items,warning=await asyncio.wait_for(rerank(semantic,self.db,f.topic,items),max(.01,deadline-perf_counter()))
            except asyncio.TimeoutError:warning='Достигнут общий срок поиска на внешней E5.'
            e5_metrics={'applied':not bool(warning),'seconds':round(perf_counter()-e5_started,4),
                'device':semantic.engine.device,'candidates':len(items)}
            if warning: warnings.append(warning)
        partial, judgments, ce_scores = [], {}, {}
        preliminary, calibration, display_mode = [], None, 'exact' if exact else 'metadata'
        if not exact and items:
            # E5/fused retrieval order selects the bounded CE pool. A small
            # terminology dictionary must not exclude semantic paraphrases.
            if perf_counter() < deadline:
                try:
                    items, ce_scores, note = await asyncio.wait_for(
                        self.reranker.rank(query if self.config.reranker_query_language=='translated' else f.topic, items),
                        max(0.01, deadline - perf_counter()))
                    if note: warnings.append(note)
                except asyncio.TimeoutError:
                    warnings.append('Срок поиска исчерпан на MiniLM; сохранён предварительный отбор.')
            calibration = load_calibration(self.config.relevance_calibration, MODEL, self.config.reranker_revision,
                self.config.reranker_max_length,self.config.reranker_query_language) if ce_scores else None
            if not calibration:
                warnings.append('Предварительная выдача: смысловые пороги не откалиброваны. Доступны также частичные и неподтверждённые материалы.')
            preliminary = list(items)
            relevant, partial, judgments = classify(intent, items, ce_scores, calibration)
            review_applied=False
            if reviewer:
                assessments = await reviewer(items[:8])
                relevant, partial = [], []
                for item in items:
                    decision = judgments[item.id]
                    assessment = assessments.get(item.id)
                    if assessment:
                        evidence = assessment.get('evidence', '')
                        if evidence and evidence.casefold() in material_text(item).casefold():
                            review_applied=True
                            tier = assessment['tier'] if decision.can_promote or assessment['tier'] != 'relevant' else 'partial'
                            decision = replace(decision, tier=tier, reason='ИИ-оценка метаданных: ' + evidence)
                            judgments[item.id] = decision
                    if decision.tier == 'relevant': relevant.append(item)
                    elif decision.tier in ('partial','unconfirmed'): partial.append(item)
            items = relevant
            if ce_scores and not calibration and not review_applied:
                # Scores order candidates; without calibrated cutoffs we must
                # not let a word checklist silently erase semantic matches.
                items=[m for m in preliminary if judgments[m.id].tier!='irrelevant' and judgments[m.id].can_promote]
                display_mode='preliminary'
        items += statistical
        items=[m for m in items if matches(m,f)]
        partial=[m for m in partial if matches(m,f)]
        if f.sort == 'year':
            items.sort(key=lambda m: m.year or 0, reverse=True)
            partial.sort(key=lambda m: m.year or 0, reverse=True)
        diagnostics = dict(stats, elapsed_seconds=round(perf_counter() - started, 4),
            candidates=candidate_count, relevant=sum(j.tier=='relevant' for j in judgments.values()),
            displayed=len(items),partial=len(partial), exact_doi=bool(doi),
            query_mode=f.query_mode,variants=list(queries),
            display_mode=display_mode,
            e5=e5_metrics,
            intent={'objects': [o.name for o in intent.objects], 'subject': intent.subject,
                'aspects': [a.name for a in intent.aspects], 'explicit_terms': list(intent.qualifiers)} if intent else {},
            reranker=self.reranker.state(), model_metrics=dict(self.reranker.metrics),
            relevance={mid: asdict(value) for mid, value in judgments.items()},
            preliminary_items=[m.to_dict() for m in (preliminary if not exact and judgments and not calibration else [])
                if judgments[m.id].tier!='irrelevant'][:f.max_results])
        logger.info('event=research_pipeline candidates=%d relevant=%d partial=%d requests=%d elapsed_ms=%.1f',
            candidate_count, len(items), len(partial), stats['http_requests'], diagnostics['elapsed_seconds'] * 1000)
        return SearchResult(items[:f.max_results], list(dict.fromkeys(warnings)), uuid4().hex,
            min(len(items), f.max_results), partial[:f.max_results], diagnostics)

    async def statistical_data(self,item,filters):
        filters.validate()
        if filters.language is not None:
            raise ValueError('Фильтр языка публикаций неприменим к статистике. Выберите любой язык оригинала.')
        provider=item.id.split(':',1)[0]
        if provider not in STAT_SOURCES or provider not in filters.sources:
            raise ValueError('Источник не выбран.')
        token=REQUEST_STATS.set({'http_requests':0,'cache_hits':0,'logical_requests':0})
        try:
            items=await asyncio.wait_for(self.sources[provider].data(item,filters),self.config.search_timeout)
            items=[m for m in items if matches(m,filters)]
            return SearchResult(items, ['Показана ограниченная выборка первичных статистических наблюдений.'], uuid4().hex,len(items))
        finally:
            REQUEST_STATS.reset(token)

    async def close(self):
        await self.http.close()
        self.translator.close()
