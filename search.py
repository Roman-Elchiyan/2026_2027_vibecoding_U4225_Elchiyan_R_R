"""Common search contracts and local-only Lab 1 search."""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
import asyncio
from collections import defaultdict
import math
import re
import sqlite3
import threading
from typing import TYPE_CHECKING, Any
from urllib.parse import unquote
from uuid import uuid4

if TYPE_CHECKING:
    from sources import MaterialSource


def normalize_doi(value: str | None) -> str | None:
    if not value:
        return None
    value = unquote(value.strip())
    value = re.sub(r"^https?://(?:dx\.)?doi\.org/", "", value, flags=re.I)
    value = re.sub(r"^doi:\s*", "", value, flags=re.I).strip().casefold()
    return value if re.fullmatch(r"10\.\d{4,9}/\S+", value) else None


@dataclass
class Material:
    id: str
    material_type: str | None
    title: str
    authors: tuple[str, ...] = ()
    year: int | None = None
    doi: str | None = None
    url: str | None = None
    open_url: str | None = None
    access_status: str | None = None
    abstract_or_snippet: str | None = None
    source: str = ""
    metadata: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> Material:
        values = dict(data)
        values["authors"] = tuple(values.get("authors", ()))
        return cls(**values)


@dataclass
class SearchFilters:
    topic: str = ""
    material_types: tuple[str, ...] = ()
    year_from: int | None = None
    year_to: int | None = None
    language: str | None = None
    open_only: bool = False
    scope: str = "local"
    sort: str = "source"
    ai_enabled: bool = False

    def validate(self) -> None:
        if not self.topic.strip() or len(self.topic) > 500:
            raise ValueError("Введите запрос длиной от 1 до 500 символов.")
        for year in (self.year_from, self.year_to):
            if year is not None and (type(year) is not int or not 1000 <= year <= 9999):
                raise ValueError("Год должен состоять из четырёх цифр.")
        if self.year_from is not None and self.year_to is not None and self.year_from > self.year_to:
            raise ValueError("Начальный год не может быть больше конечного.")
        if self.language or self.open_only or self.scope != "local" or self.ai_enabled or self.sort != "source":
            raise ValueError("Этот фильтр пока недоступен в Lab 1; поиск не выполнен.")

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> SearchFilters:
        values = dict(data)
        values["material_types"] = tuple(values.get("material_types", ()))
        return cls(**values)


@dataclass
class SearchResult:
    items: list[Material]
    warnings: list[str]
    search_id: str
    total_count: int


def search_text(item: Material) -> str:
    return item.title + " " + str(item.metadata.get("keywords") or "")


def reciprocal_rank_fusion(rankings: list[list[str]], weights: list[float], k: int = 60) -> list[str]:
    """Only ranks are combined; BM25, edit distance and cosine never share a score scale."""
    scores: dict[str, float] = defaultdict(float)
    for ranking, weight in zip(rankings, weights):
        if weight <= 0:
            continue
        for rank, material_id in enumerate(dict.fromkeys(ranking), 1):
            scores[material_id] += weight / (k + rank)
    return sorted(scores, key=lambda mid: (-scores[mid], mid))


STOP_WORDS = frozenset("a an the and or of in on for to with by from is are as at into how does do".split())


def tokens(text: str) -> list[str]:
    return list(dict.fromkeys(word for word in re.findall(r"[^\W_]+", text.casefold())
                             if word not in STOP_WORDS))


def deduplicate(items: list[Material]) -> list[Material]:
    seen_dois: set[str] = set()
    seen_ids: set[str] = set()
    result = []
    for item in items:
        doi = normalize_doi(item.doi)
        if item.id in seen_ids or (doi is not None and doi in seen_dois):
            continue
        seen_ids.add(item.id)
        if doi:
            seen_dois.add(doi)
        result.append(item)
    return result


class SearchService:
    def __init__(self, source: MaterialSource, config=None, semantic=None):
        from config import Config
        self.source = source
        self.config = config or Config()
        self.semantic = semantic
        self._fts = None
        self._signature = None
        self._document_words = {}
        self._vocabulary = set()
        self._lock = threading.Lock()

    def close(self):
        with self._lock:
            if self._fts:
                self._fts.close()
                self._fts = None
                self._signature = None

    def __del__(self):
        self.close()

    def _candidates(self, filters: SearchFilters) -> list[Material]:
        filters.validate()
        items = self.source.load()
        if filters.material_types and not self.source.supports_types:
            raise ValueError("В этом CSV нет типа материала; фильтр не применён. Снимите его.")
        return deduplicate([item for item in items
            if (filters.year_from is None or item.year is not None and item.year >= filters.year_from)
            and (filters.year_to is None or item.year is not None and item.year <= filters.year_to)
            and (not filters.material_types or item.material_type in filters.material_types)])

    def _rank_text(self, items: list[Material], topic: str) -> tuple[list[str], list[str]]:
        from rapidfuzz import fuzz
        from rapidfuzz.distance import DamerauLevenshtein
        words = tokens(topic)
        if not words:
            return [], []
        all_items = self.source.load()
        signature = tuple((m.id, search_text(m)) for m in all_items)
        allowed = {m.id for m in items}
        with self._lock:
            if signature != self._signature:
                if self._fts:
                    self._fts.close()
                self._fts = sqlite3.connect(":memory:", check_same_thread=False)
                self._fts.execute("CREATE VIRTUAL TABLE catalog USING fts5(id UNINDEXED,title,keywords, tokenize='unicode61')")
                self._fts.executemany("INSERT INTO catalog VALUES (?,?,?)", [
                    (m.id, m.title, str(m.metadata.get("keywords") or "")) for m in all_items])
                self._document_words = {m.id: set(tokens(search_text(m))) for m in all_items}
                self._vocabulary = set().union(*self._document_words.values())
                self._signature = signature
            expression = " OR ".join('"' + word + '"' for word in words)
            ranked = self._fts.execute("SELECT id FROM catalog WHERE catalog MATCH ? ORDER BY bm25(catalog,0,3,1),id",
                                       (expression,)).fetchall()
            document_words = self._document_words
            vocabulary = self._vocabulary
        # OR retrieves candidates, then coverage prevents one generic word from filling the output.
        wordsets = {m.id: document_words[m.id] for m in items}
        required = math.ceil(len(words) * 0.75)
        fts = [row[0] for row in ranked if row[0] in allowed
               and sum(w in wordsets[row[0]] for w in words) >= required]
        if fts and all(word in vocabulary for word in words):
            return fts, []
        from rapidfuzz import process
        matches = []
        for word in words:
            scores = {other: score for other, score, _ in process.extract(word, vocabulary, scorer=fuzz.ratio,
                score_cutoff=self.config.fuzzy_min_score, limit=None)}
            if len(word) >= 4 and self.config.fuzzy_min_score <= 90:
                for other in vocabulary:
                    if len(other) >= 4 and abs(len(word)-len(other)) <= 1 and DamerauLevenshtein.distance(word, other, score_cutoff=1) <= 1:
                        scores[other] = max(scores.get(other, 0), 90.0)
            matches.append(scores)
        fuzzy = []
        for item in items:
            document_words = wordsets[item.id]
            scores = [max((match.get(other, 0) for other in document_words), default=0) for match in matches]
            if sum(score >= self.config.fuzzy_min_score for score in scores) >= math.ceil(len(words)*0.85):
                fuzzy.append((sum(scores)/len(scores), item.id))
        fuzzy.sort(key=lambda pair: (-pair[0], pair[1]))
        return fts, [mid for _, mid in fuzzy]

    def search(self, filters: SearchFilters, semantic_ranking: list[str] | None = None,
               warnings: list[str] | None = None) -> SearchResult:
        items = self._candidates(filters)
        doi = normalize_doi(filters.topic)
        doi_like = bool(re.match(r"^(?:https?://(?:dx\.)?doi\.org/|doi:|10\.\d{4,9}/)", filters.topic.strip(), re.I))
        if doi_like or doi:
            matches = [m for m in items if doi and normalize_doi(m.doi) == doi]
            return SearchResult(matches, [], uuid4().hex, len(matches))
        exact = [m for m in items if m.title.casefold().strip() == filters.topic.casefold().strip()]
        if exact:
            return SearchResult(exact, [], uuid4().hex, len(exact))
        fts, fuzzy = self._rank_text(items, filters.topic)
        ids = reciprocal_rank_fusion([fts, fuzzy, semantic_ranking or []],
            [self.config.fts_weight, self.config.fuzzy_weight, self.config.semantic_weight], self.config.rrf_k)
        by_id = {m.id: m for m in items}
        matches = [by_id[mid] for mid in ids if mid in by_id]
        return SearchResult(matches, warnings or [], uuid4().hex, len(matches))

    async def async_search(self, filters: SearchFilters) -> SearchResult:
        # DOI and exact titles stay exact and never trigger semantic substitution.
        if normalize_doi(filters.topic) or re.match(r"^(?:https?://(?:dx\.)?doi\.org/|doi:|10\.\d{4,9}/)", filters.topic.strip(), re.I):
            return await asyncio.to_thread(self.search, filters)
        candidates = await asyncio.to_thread(self._candidates, filters)
        if any(m.title.casefold().strip() == filters.topic.casefold().strip() for m in candidates):
            return await asyncio.to_thread(self.search, filters)
        if self.semantic:
            ranking, warning = await self.semantic.rank(filters.topic, {m.id for m in candidates})
        else:
            ranking, warning = [], "E5 отключена. Межъязыковой поиск ограничен; используются FTS5 и RapidFuzz."
        return await asyncio.to_thread(self.search, filters, ranking, [warning] if warning else [])
