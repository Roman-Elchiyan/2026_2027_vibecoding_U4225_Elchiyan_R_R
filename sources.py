"""CSV adapter. Future source adapters implement MaterialSource without changing UX."""

from __future__ import annotations

import csv
import hashlib
from pathlib import Path
from typing import Protocol
from urllib.parse import urlsplit

from search import Material, normalize_doi


class CatalogError(RuntimeError):
    """A missing or invalid catalog; never replaced by generated records."""


class MaterialSource(Protocol):
    supports_types: bool

    def load(self) -> list[Material]: ...


def clean(value: str | None) -> str | None:
    if value is None or value.strip() in ("", "NA", "N/A", "NULL"):
        return None
    return value.strip()


def safe_url(value: str | None) -> str | None:
    if not value:
        return None
    try:
        parsed = urlsplit(value)
        return value if parsed.scheme in ("http", "https") and parsed.hostname and not parsed.username else None
    except ValueError:
        return None


class CsvSource:
    def __init__(self, path: Path):
        self.path = Path(path)
        self.supports_types = False
        self.row_count = 0

    def load(self) -> list[Material]:
        row_count = 0
        try:
            with self.path.open(encoding="utf-8-sig", newline="") as stream:
                reader = csv.DictReader(stream, delimiter=",")
                headers = set(reader.fieldnames or ())
                if not {"paperid", "title", "published_year", "doi", "paper_url"} <= headers:
                    raise CatalogError("CSV имеет неподдерживаемую схему. Нужен исходный ploswater.csv.")
                supports_types = "article_type" in headers
                items = []
                for row in reader:
                    if None in row or any(value is None for value in row.values()):
                        raise CatalogError("Повреждённая строка CSV. Восстановите исходный файл.")
                    row_count += 1
                    metadata = {key: clean(value) for key, value in row.items()}
                    title = metadata["title"]
                    paper_id = metadata["paperid"]
                    if not title or not paper_id:
                        raise CatalogError("В CSV отсутствует заголовок или стабильный ID записи.")
                    raw_year = metadata["published_year"]
                    if raw_year and (not raw_year.isdigit() or not 1000 <= int(raw_year) <= 9999):
                        raise CatalogError("В CSV некорректный год публикации.")
                    doi = normalize_doi(metadata["doi"])
                    identity = doi or paper_id
                    material_id = hashlib.sha256(("ploswater:" + identity).encode()).hexdigest()[:24]
                    first_author = metadata.get("first_author_name")
                    items.append(Material(
                        id=material_id,
                        material_type=metadata.get("article_type"),
                        title=title,
                        authors=(first_author,) if first_author else (),
                        year=int(raw_year) if raw_year else None,
                        doi=doi,
                        url=safe_url(metadata["paper_url"]),
                        source=metadata.get("journal") or "ploswater",
                        metadata=metadata,
                    ))
                if not items:
                    raise CatalogError("CSV не содержит записей.")
                # Publish completed diagnostics only; concurrent readers must not reset each other's count/types.
                self.row_count = row_count
                self.supports_types = supports_types
                return items
        except (OSError, UnicodeError, csv.Error) as exc:
            raise CatalogError("Каталог CSV отсутствует или не читается. Проверьте data/ploswater.csv.") from exc


# Lab 2: each adapter will return the same Material model. No HTTP clients here.
EXTERNAL_SOURCE_CONTRACTS = {
    "crossref": "DOI / title / topic → Material (Lab 2)",
    "openalex": "DOI / title / topic → Material (Lab 2)",
    "datacite": "DOI / title / topic → Material (Lab 2)",
    "worldbank": "World Bank Documents & Reports: title / topic → Material (Lab 2)",
}
