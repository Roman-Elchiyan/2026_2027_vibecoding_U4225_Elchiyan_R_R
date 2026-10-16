"""Bounded, cached HTTP metadata adapters. No generated bibliography."""
import asyncio
import hashlib
from html import unescape
import json
import logging
import re
from time import perf_counter
from urllib.parse import quote

import httpx
from search import Material, normalize_doi
from sources import safe_url

logger = logging.getLogger(__name__)
SOURCE_NAMES = {"local":"PLOS Water CSV", "openalex":"OpenAlex", "crossref":"Crossref", "datacite":"DataCite", "worldbank":"World Bank"}


def plain(value):
    if isinstance(value,list):
        value = " ".join(plain(v) for v in value)
    if isinstance(value,dict):
        value = " ".join(plain(v) for v in value.values())
    return unescape(re.sub(r"<[^>]*>", "", str(value or ""))).strip()


def year_of(value):
    match = re.match(r"(\d{4})", str(value or ""))
    return int(match[1]) if match else None


class SourceUnavailable(Exception):
    pass


class MetadataHTTP:
    def __init__(self, db, config, transport=None):
        self.db, self.config = db, config
        self.client = httpx.AsyncClient(timeout=config.api_timeout, transport=transport, follow_redirects=True,
            headers={"User-Agent":"BrilliantDetective/2.0"+(" (mailto:"+config.contact_email+")" if config.contact_email else "")})
        self.slots = asyncio.Semaphore(3)
        self.locks = {}
        self.last = {}

    async def get(self, source, url, params):
        # API credentials never become part of a persisted cache key.
        public_params = {k:v for k,v in params.items() if k != "api_key"}
        key = "http:"+hashlib.sha256((url+json.dumps(public_params,sort_keys=True)).encode()).hexdigest()
        cached = self.db.cache_get(key)
        if cached is not None:
            logger.info("event=api_cache_hit source=%s",source)
            return cached
        lock = self.locks.setdefault(source,asyncio.Lock())
        started = perf_counter()
        async with self.slots, lock:
            # At most 3 requests/second/source, including retries.
            for attempt in range(2):
                delay = 0.34 - (perf_counter()-self.last.get(source,0))
                if delay > 0:
                    await asyncio.sleep(delay)
                try:
                    self.last[source] = perf_counter()
                    response = await self.client.get(url,params=params)
                    logger.info("event=external_api source=%s status=%d elapsed_ms=%.1f",source,response.status_code,(perf_counter()-started)*1000)
                    if response.status_code == 404:
                        return {}
                    if response.status_code == 429 or response.status_code >= 500:
                        if attempt == 0:
                            retry = response.headers.get("Retry-After","0.5")
                            try:
                                retry = float(retry)
                            except ValueError:
                                retry = 0.5
                            await asyncio.sleep(min(max(retry,0.1),1.5))
                            continue
                    response.raise_for_status()
                    data = response.json()
                    self.db.cache_put(key,data,3600)
                    return data
                except (httpx.HTTPError, ValueError) as exc:
                    logger.warning("event=external_failed source=%s error=%s",source,type(exc).__name__)
                    raise SourceUnavailable(f"{SOURCE_NAMES[source]} временно недоступен. Результаты остальных источников сохранены; нажмите «Повторить поиск».") from None
            raise SourceUnavailable(f"{SOURCE_NAMES[source]} недоступен после повторной попытки.")

    async def close(self):
        await self.client.aclose()


class OpenAlex:
    name = "openalex"
    def __init__(self,http): self.http = http

    async def search(self,query,doi=None,limit=20):
        params = {"per-page":limit}
        if self.http.config.openalex_key:
            params["api_key"] = self.http.config.openalex_key
        params["filter" if doi else "search"] = "doi:https://doi.org/"+doi if doi else query
        data = await self.http.get(self.name,"https://api.openalex.org/works",params)
        return [self.normalize(v) for v in data.get("results",[]) if v.get("title")]

    @staticmethod
    def normalize(v):
        index = v.get("abstract_inverted_index") or {}
        positions = {pos:word for word,indices in index.items() for pos in indices}
        abstract = " ".join(positions[p] for p in sorted(positions)) or None
        oa = v.get("open_access") or {}
        status = "open" if oa.get("is_oa") is True else "restricted" if oa.get("is_oa") is False else None
        loc = v.get("primary_location") or {}
        return Material(id="openalex:"+v["id"].rsplit("/",1)[-1], material_type=v.get("type"),title=v["title"],
            authors=tuple(a["author"]["display_name"] for a in v.get("authorships",[]) if a.get("author",{}).get("display_name")),
            year=v.get("publication_year"),doi=normalize_doi(v.get("doi")),url=safe_url(loc.get("landing_page_url")) or safe_url(v["id"]),
            open_url=safe_url(oa.get("oa_url")),access_status=status,abstract_or_snippet=abstract,source="OpenAlex",
            metadata={"source_id":v["id"],"language":v.get("language"),"category":"scientific","publication_date":v.get("publication_date")})


class Crossref:
    name = "crossref"
    def __init__(self,http): self.http = http

    async def search(self,query,doi=None,limit=20):
        params = {"rows":limit,"query.bibliographic":query} if not doi else {}
        if self.http.config.contact_email:
            params["mailto"] = self.http.config.contact_email
        url = "https://api.crossref.org/works"+("/"+quote(doi,safe="") if doi else "")
        data = await self.http.get(self.name,url,params)
        message = data.get("message",{})
        rows = [message] if doi and message else message.get("items",[]) if isinstance(message,dict) else []
        return [self.normalize(v) for v in rows if v.get("title")]

    @staticmethod
    def normalize(v):
        date = (v.get("published") or v.get("published-print") or v.get("issued") or {}).get("date-parts",[[]])[0]
        doi = normalize_doi(v.get("DOI"))
        # A Crossref license/link alone does not establish open/restricted access.
        return Material(id="crossref:"+str(v["DOI"]).lower(),material_type=v.get("type"),title=plain(v.get("title")),
            authors=tuple(" ".join(filter(None,(a.get("given"),a.get("family")))) or a.get("name","") for a in v.get("author",[])),
            year=date[0] if date else None,doi=doi,url=safe_url(v.get("URL")) or ("https://doi.org/"+doi if doi else None),
            abstract_or_snippet=plain(v.get("abstract")) or None,source="Crossref",
            metadata={"source_id":v["DOI"],"language":v.get("language"),"category":"scientific", "licenses":v.get("license",[])})


class DataCite:
    name = "datacite"
    def __init__(self,http): self.http = http

    async def search(self,query,doi=None,limit=20):
        params = {} if doi else {"query":query,"page[size]":limit}
        data = await self.http.get(self.name,"https://api.datacite.org/dois"+("/"+quote(doi,safe="") if doi else ""),params)
        rows = data.get("data",[])
        if isinstance(rows,dict): rows = [rows]
        return [self.normalize(v) for v in rows if v.get("attributes",{}).get("titles")]

    @staticmethod
    def normalize(v):
        a = v["attributes"]
        rights = a.get("rightsList") or []
        # Only explicit Open Access rights identifiers establish access.
        opened = any("info:eu-repo/semantics/openAccess" in str(r.get("rightsUri","")) or
            r.get("rightsIdentifier","").lower().startswith("cc-") for r in rights)
        doi = normalize_doi(a.get("doi") or v["id"])
        url = safe_url(a.get("url"))
        return Material(id="datacite:"+v["id"].lower(),material_type=(a.get("types") or {}).get("resourceTypeGeneral"),
            title=plain(a["titles"][0].get("title")),authors=tuple(c.get("name","") for c in a.get("creators",[])),
            year=year_of(a.get("publicationYear")),doi=doi,url=url or ("https://doi.org/"+doi if doi else None),
            open_url=url if opened else None,access_status="open" if opened else None,
            abstract_or_snippet=plain([d.get("description","") for d in a.get("descriptions",[])]) or None,source="DataCite",
            metadata={"source_id":v["id"],"language":a.get("language"),"category":"scientific","rights":rights})


class WorldBank:
    name = "worldbank"
    def __init__(self,http): self.http = http

    async def search(self,query,doi=None,limit=20):
        if doi:
            return []  # D&R has no documented exact DOI lookup.
        data = await self.http.get(self.name,"https://search.worldbank.org/api/v3/wds",
            {"format":"json","qterm":query,"rows":limit,
             "fl":"id,display_title,docdt,docty,authr,abstracts,lang,disclstat,pdfurl,url,docna"})
        docs = data.get("documents",{})
        return [self.normalize(k,v) for k,v in docs.items() if isinstance(v,dict) and v.get("display_title")]

    @staticmethod
    def normalize(key,v):
        url = safe_url(v.get("url"))
        pdf = safe_url(v.get("pdfurl"))
        opened = str(v.get("disclstat","")).lower() in ("disclosed","public") and bool(pdf)
        language = plain(v.get("lang")).lower()
        language = {"english":"en","russian":"ru"}.get(language,language or None)
        authors = v.get("authr") or []
        if isinstance(authors,str): authors = [authors]
        if isinstance(authors,dict): authors = list(authors.values())
        return Material(id="worldbank:"+str(v.get("id") or key),material_type=plain(v.get("docty")) or None,
            title=plain(v["display_title"]),authors=tuple(plain(a) for a in authors),year=year_of(v.get("docdt")),
            url=url or pdf,open_url=pdf if opened else None,access_status="open" if opened else None,
            abstract_or_snippet=plain(v.get("abstracts")) or None,source="World Bank",
            metadata={"source_id":v.get("id") or key,"language":language,"category":"analytical","publication_date":v.get("docdt")})


def adapters(http):
    return {a.name:a for a in (OpenAlex(http),Crossref(http),DataCite(http),WorldBank(http))}
