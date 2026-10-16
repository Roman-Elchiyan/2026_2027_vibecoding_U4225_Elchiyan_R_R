"""Common search contracts and local-only Lab 1 search."""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
import re
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
    def __init__(self, source: MaterialSource):
        self.source = source

    def search(self, filters: SearchFilters) -> SearchResult:
        filters.validate()
        items = self.source.load()
        if filters.material_types and not self.source.supports_types:
            raise ValueError("В этом CSV нет типа материала; фильтр не применён. Снимите его.")
        doi = normalize_doi(filters.topic)
        words = filters.topic.casefold().split()
        matches = []
        for item in items:
            if doi:
                if normalize_doi(item.doi) != doi:
                    continue
            else:
                text = (item.title + " " + (item.metadata.get("keywords") or "")).casefold()
                if not all(word in text for word in words):
                    continue
            if filters.year_from is not None and (item.year is None or item.year < filters.year_from):
                continue
            if filters.year_to is not None and (item.year is None or item.year > filters.year_to):
                continue
            if filters.material_types and item.material_type not in filters.material_types:
                continue
            matches.append(item)
        matches = deduplicate(matches)
        return SearchResult(matches, [], uuid4().hex, len(matches))
