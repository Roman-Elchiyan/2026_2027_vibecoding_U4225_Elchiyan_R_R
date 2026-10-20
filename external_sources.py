"""Bounded, cached HTTP metadata adapters. No generated bibliography."""
import asyncio
from contextvars import ContextVar
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
from query_planner import analyze_query

logger = logging.getLogger(__name__)
SOURCE_NAMES = {"local":"PLOS Water CSV", "openalex":"OpenAlex", "crossref":"Crossref", "datacite":"DataCite", "worldbank":"World Bank", "core":"CORE", "zenodo":"Zenodo", "pubmed":"PubMed/PMC", "arxiv":"arXiv", "europepmc":"Europe PMC", "oecd":"OECD", "imf":"IMF", "comtrade":"UN Comtrade", "eurostat":"Eurostat", "ilostat":"ILOSTAT"}
REQUEST_STATS = ContextVar('metadata_request_stats', default=None)
SOURCE_PARTIALS = ContextVar('metadata_source_partials', default=None)


def retain_page(items):
    retained=SOURCE_PARTIALS.get()
    if retained is not None:retained[:]=items


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
        self.client = httpx.AsyncClient(timeout=config.api_timeout, transport=transport,
            follow_redirects=False, headers={"User-Agent":"BrilliantDetective/3.0"})
        self.slots = asyncio.Semaphore(3)
        self.locks, self.last = {}, {}

    async def get(self, source, url, params, *, headers=None, ttl=3600):
        return await self._read(source, url, params, headers=headers, ttl=ttl)

    async def get_text(self, source, url, params, *, headers=None, ttl=3600):
        return await self._read(source, url, params, headers=headers, ttl=ttl, text=True)

    async def _read(self, source, url, params, *, headers=None, ttl=3600, text=False):
        stats = REQUEST_STATS.get()
        if stats is not None:
            stats["logical_requests"] = stats.get("logical_requests", 0) + 1
        secret_fields = {"api_key", "apikey", "access_token", "key", "subscription-key"}
        public = {k:v for k,v in params.items() if k.lower() not in secret_fields}
        # Different credential contexts never share private cached metadata.
        credentials = {k:v for k,v in params.items() if k.lower() in secret_fields}
        credentials.update({k:v for k,v in (headers or {}).items()
            if k.lower() in ("authorization","ocp-apim-subscription-key")})
        scope = hashlib.sha256(json.dumps(credentials,sort_keys=True).encode()).hexdigest()[:16]
        identity = json.dumps([source,url,public,scope,text,(headers or {}).get("Accept","")],sort_keys=True)
        key = "http:" + hashlib.sha256(identity.encode()).hexdigest()
        cached = self.db.cache_get(key)
        if cached is not None:
            if stats is not None: stats["cache_hits"] = stats.get("cache_hits",0) + 1
            return cached
        # Waiters for the same provider must not occupy all global slots and
        # starve a healthy independent provider until the search deadline.
        async with self.locks.setdefault(source,asyncio.Lock()), self.slots:
            # Recheck after waiting for a matching provider request.
            cached = self.db.cache_get(key)
            if cached is not None:
                if stats is not None: stats["cache_hits"] = stats.get("cache_hits",0) + 1
                return cached
            interval = {"arxiv":3.1,"core":1.1,"oecd":1.1,"imf":1.1,"ilostat":1.1}.get(source,0.35)
            if source == "pubmed" and getattr(self.config,"ncbi_api_key",""): interval = 0.11
            for attempt in range(2):
                maximum = getattr(self.config,"api_request_budget",48)
                delay = interval - (perf_counter() - self.last.get(source,0))
                if delay > 0: await asyncio.sleep(delay)
                # No await between checking and reserving: concurrent sources
                # and retries share this search's exact request boundary.
                if stats is not None:
                    if stats.get("http_requests",0) >= maximum:
                        raise SourceUnavailable("Достигнут общий лимит обращений к API; сохранены полученные материалы.")
                    stats["http_requests"] = stats.get("http_requests",0) + 1
                self.last[source] = perf_counter()
                started = perf_counter()
                try:
                    async with self.client.stream('GET',url,params=params,headers=headers) as stream:
                        content = bytearray()
                        async for chunk in stream.aiter_bytes():
                            content.extend(chunk)
                            if len(content) > 16*1024*1024:
                                raise SourceUnavailable('Ответ источника слишком велик; уточните набор или измерения.')
                        decoded_headers={k:v for k,v in stream.headers.items() if k.lower() not in ('content-encoding','content-length')}
                        response = httpx.Response(stream.status_code, headers=decoded_headers,
                            content=bytes(content), request=stream.request)
                    logger.info("event=external_api source=%s status=%d elapsed_ms=%.1f",
                        source,response.status_code,(perf_counter()-started)*1000)
                    if response.status_code in (404,204): return "" if text else {}
                    if response.status_code == 429 or response.status_code >= 500:
                        if attempt == 0:
                            try: delay = float(response.headers.get("Retry-After","0.5"))
                            except ValueError:
                                from email.utils import parsedate_to_datetime
                                from datetime import datetime, timezone
                                try:delay=(parsedate_to_datetime(response.headers['Retry-After'])-datetime.now(timezone.utc)).total_seconds()
                                except (KeyError,TypeError,ValueError):delay=0.5
                            if delay > 10:
                                raise SourceUnavailable("Источник ограничил частоту; автоматический ранний повтор не выполняется.")
                            await asyncio.sleep(max(delay,0.1))
                            continue
                    response.raise_for_status()
                    if len(response.content) > 16*1024*1024:
                        raise SourceUnavailable("Ответ источника слишком велик; уточните набор или измерения.")
                    data = response.text if text else response.json()
                    self.db.cache_put(key,data,ttl)
                    return data
                except (httpx.HTTPError,ValueError) as exc:
                    logger.warning("event=external_failed source=%s error=%s",source,type(exc).__name__)
                    status=f' HTTP {exc.response.status_code}.' if isinstance(exc,httpx.HTTPStatusError) else ''
                    raise SourceUnavailable(SOURCE_NAMES.get(source,source)+" временно недоступен."+status) from None
        raise SourceUnavailable(SOURCE_NAMES.get(source,source)+" недоступен после повторной попытки.")

    async def close(self):
        await self.client.aclose()


class OpenAlex:
    name = "openalex"
    def __init__(self,http): self.http = http

    async def search(self,query,doi=None,limit=20,*,filters=None):
        size,pages=page_options(self.http,limit)
        params={"per-page":size}
        if self.http.config.openalex_key:params["api_key"]=self.http.config.openalex_key
        params["filter" if doi else "search"]="doi:https://doi.org/"+doi if doi else query
        if filters is not None and not doi:
            conditions=[]
            if filters.year_from:conditions.append(f"from_publication_date:{filters.year_from}-01-01")
            if filters.year_to:conditions.append(f"to_publication_date:{filters.year_to}-12-31")
            if filters.language:conditions.append("language:"+filters.language)
            native_types={'article','book','book-chapter','dataset','dissertation','preprint','report','review','standard','other'}
            if filters.material_types and set(filters.material_types)<=native_types:
                conditions.append('type:'+'|'.join(filters.material_types))
            if filters.open_only or filters.access_status=="open":conditions.append("is_oa:true")
            if conditions:params["filter"]=",".join(conditions)
        output=[];cursor="*"
        for _ in range(1 if doi else pages):
            if not doi:params["cursor"]=cursor
            data=await self.http.get(self.name,"https://api.openalex.org/works",params)
            rows=data.get("results") or []
            output.extend(self.normalize(v) for v in rows if v.get("title"))
            retain_page(output)
            nxt=(data.get("meta") or {}).get("next_cursor")
            if len(output)>=limit or len(rows)<size or not nxt or nxt==cursor:break
            cursor=nxt
        return output[:limit]

    @staticmethod
    def normalize(v):
        index = v.get("abstract_inverted_index") or {}
        positions = {pos:word for word,indices in index.items() for pos in indices}
        abstract = " ".join(positions[p] for p in sorted(positions)) or None
        oa = v.get("open_access") or {}
        status = "open" if oa.get("is_oa") is True else None
        loc = v.get("primary_location") or {}
        ids=v.get('ids') or {}
        pmid=re.search(r'(?:^|/)(\d+)/?$',str(ids.get('pmid') or ''))
        pmcid=re.search(r'PMC\d+',str(ids.get('pmcid') or ''),re.I)
        return Material(id="openalex:"+v["id"].rsplit("/",1)[-1], material_type=v.get("type"),title=v["title"],
            authors=tuple(a["author"]["display_name"] for a in v.get("authorships",[]) if a.get("author",{}).get("display_name")),
            year=v.get("publication_year"),doi=normalize_doi(v.get("doi")),url=safe_url(loc.get("landing_page_url")) or safe_url(v["id"]),
            open_url=safe_url(oa.get("oa_url")),access_status=status,abstract_or_snippet=abstract,source="OpenAlex",
            metadata={"source_id":v["id"],"language":v.get("language"),"category":"scientific","publication_date":v.get("publication_date"),
                'pmid':pmid[1] if pmid else None,'pmcid':pmcid[0].upper() if pmcid else None,
                'keywords':'; '.join(dict.fromkeys(k.get('display_name','') for k in (v.get('keywords') or []) if k.get('display_name'))), **pdf_metadata((v.get('best_oa_location') or {}).get('pdf_url') or loc.get('pdf_url'), 'OpenAlex', documented=True)})


class Crossref:
    name = "crossref"
    def __init__(self,http): self.http = http

    async def search(self,query,doi=None,limit=20,*,filters=None):
        size,pages=page_options(self.http,limit)
        params={"rows":size,"query.bibliographic":query,"sort":"relevance"} if not doi else {}
        if filters and getattr(filters,'query_mode','topic')=='title' and not doi:
            params['query.title']=params.pop('query.bibliographic').strip('"')
        if self.http.config.contact_email:params["mailto"]=self.http.config.contact_email
        if filters is not None and not doi:
            conditions=[]
            if filters.year_from:conditions.append(f"from-pub-date:{filters.year_from}-01-01")
            if filters.year_to:conditions.append(f"until-pub-date:{filters.year_to}-12-31")
            if len(filters.material_types)==1 and filters.material_types[0] in ("journal-article","book","report","proceedings-article","posted-content"):
                conditions.append("type:"+filters.material_types[0])
            if conditions:params["filter"]=",".join(conditions)
        url="https://api.crossref.org/works"+("/"+quote(doi,safe="") if doi else "")
        output=[];cursor="*"
        for _ in range(1 if doi else pages):
            if not doi:params["cursor"]=cursor
            data=await self.http.get(self.name,url,params)
            block=data.get("message") or {}
            rows=[block] if doi and block else block.get("items",[])
            output.extend(self.normalize(v) for v in rows if v.get("title"))
            retain_page(output)
            nxt=block.get("next-cursor")
            if len(output)>=limit or len(rows)<size or not nxt or nxt==cursor:break
            cursor=nxt
        return output[:limit]

    @staticmethod
    def normalize(v):
        date = (v.get("published") or v.get("published-print") or v.get("issued") or {}).get("date-parts",[[]])[0]
        doi = normalize_doi(v.get("DOI"))
        # A Crossref license/link alone does not establish open/restricted access.
        return Material(id="crossref:"+str(v["DOI"]).lower(),material_type=v.get("type"),title=plain(v.get("title")),
            authors=tuple(" ".join(filter(None,(a.get("given"),a.get("family")))) or a.get("name","") for a in v.get("author",[])),
            year=date[0] if date else None,doi=doi,url=safe_url(v.get("URL")) or ("https://doi.org/"+doi if doi else None),
            abstract_or_snippet=plain(v.get("abstract")) or None,source="Crossref",
            metadata={"source_id":v["DOI"],"language":v.get("language"),"category":"scientific", "licenses":v.get("license",[]),
                'keywords':'; '.join(str(s) for s in (v.get('subject') or []))})


class DataCite:
    name = "datacite"
    def __init__(self,http): self.http = http

    @staticmethod
    def query_string(query):
        return query.strip()[:1500]

    async def search(self,query,doi=None,limit=20,*,filters=None):
        size,pages=page_options(self.http,limit)
        params={} if doi else {"query":self.query_string(query),"page[size]":size,"sort":"relevance"}
        if filters is not None and not doi:
            if len(filters.material_types)==1:
                typ=filters.material_types[0]
                if typ in ("Dataset","Text","Software","Collection","Image"):params["resource-type-id"]=typ.casefold()
            if filters.year_from is not None or filters.year_to is not None:
                params["query"]="("+params["query"]+") AND publicationYear:["+str(filters.year_from or "*")+" TO "+str(filters.year_to or "*")+"]"
        url="https://api.datacite.org/dois"+("/"+quote(doi,safe="") if doi else "")
        output=[]
        for page in range(1,1+(1 if doi else pages)):
            if not doi:params["page[number]"]=page
            data=await self.http.get(self.name,url,params)
            rows=data.get("data") or []
            if isinstance(rows,dict):rows=[rows]
            output.extend(self.normalize(v) for v in rows if (v.get("attributes") or {}).get("titles"))
            retain_page(output)
            if len(output)>=limit or len(rows)<size:break
        return output[:limit]

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
            metadata={"source_id":v["id"],"language":a.get("language"),"category":"scientific","rights":rights,
                'related_identifiers':a.get('relatedIdentifiers') or [],
                'version':a.get('version'),'version_created':a.get('created'),
                'keywords':'; '.join(s.get('subject','') for s in (a.get('subjects') or []) if s.get('subject'))})


class WorldBank:
    name = "worldbank"
    def __init__(self,http): self.http = http

    async def search(self,query,doi=None,limit=20,*,filters=None):
        if doi:return []
        size,pages=page_options(self.http,limit);output=[]
        for page in range(pages):
            params={"format":"json","qterm":query,"rows":size,"os":page*size,
                "fl":"id,display_title,docdt,docty,authr,abstracts,lang,disclstat,pdfurl,url,docna"}
            if filters:
                if filters.year_from:params['strdate']=f'{filters.year_from}-01-01'
                if filters.year_to:params['enddate']=f'{filters.year_to}-12-31'
                if filters.language:params['lang_exact']={'en':'English','ru':'Russian'}[filters.language]
                if len(filters.material_types)==1:params['docty_exact']=filters.material_types[0]
            data=await self.http.get(self.name,"https://search.worldbank.org/api/v3/wds",params)
            rows=[(k,v) for k,v in (data.get("documents") or {}).items() if isinstance(v,dict) and v.get("display_title")]
            output.extend(self.normalize(k,v) for k,v in rows)
            retain_page(output)
            if len(output)>=limit or len(rows)<size:break
        return output[:limit]

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
            metadata={"source_id":v.get("id") or key,"language":language,"category":"analytical","publication_date":v.get("docdt"), **pdf_metadata(pdf,"World Bank",documented=True)})


def page_options(http, limit):
    config = getattr(http, "config", None)
    return min(max(1,limit),getattr(config,"api_page_size",30)), getattr(config,"api_max_pages",2)


def date_clause(filters, field, pattern="range"):
    if not filters or not (filters.year_from or filters.year_to): return ""
    lo, hi = filters.year_from or 1500, filters.year_to or 2100
    if pattern == "pubmed": return f'("{lo}/01/01"[Date - Publication] : "{hi}/12/31"[Date - Publication])'
    return f"{field}:[{lo}-01-01 TO {hi}-12-31]"


def identifier_query(value):
    value=value.strip()
    m=re.fullmatch(r'https?://(?:pmc\.ncbi\.nlm\.nih\.gov|www\.ncbi\.nlm\.nih\.gov/pmc)/articles/(PMC\d+)/?',value,re.I)
    if m:return 'pmcid',m[1].upper()
    m=re.fullmatch(r"(?:PMID[:\s]*|https?://pubmed\.ncbi\.nlm\.nih\.gov/)(\d+)/?",value,re.I)
    if m:return "pmid",m[1]
    m=re.fullmatch(r"(?:PMCID[:\s]*)?(PMC\d+)",value,re.I)
    if m:return "pmcid",m[1].upper()
    m=re.fullmatch(r"(?:arxiv[:\s]*|https?://arxiv\.org/(?:abs|pdf)/)?((?:\d{4}\.\d{4,5}|[a-z.-]+/\d{7})(?:v\d+)?)(?:\.pdf)?",value,re.I)
    if m:return "arxiv",m[1]
    return None,None


def pdf_metadata(url, provider, *, documented=False):
    from urllib.parse import urlsplit
    url=safe_url(url)
    if not url:return {}
    if documented or urlsplit(url).path.casefold().endswith(".pdf"):
        return {"pdf_url":url,"pdf_provenance":provider,"pdf_verified":False}
    return {}


class Core:
    name="core"
    def __init__(self,http):self.http=http

    async def search(self,query,doi=None,limit=20,*,filters=None):
        key=getattr(self.http.config,"core_api_key","")
        if not key:raise SourceUnavailable("CORE требует CORE_API_KEY в основном .env.")
        size,pages=page_options(self.http,limit)
        q=f'doi:"{doi}"' if doi else query
        if filters and not doi:
            if filters.year_from:q+=f" AND yearPublished>={filters.year_from}"
            if filters.year_to:q+=f" AND yearPublished<={filters.year_to}"
        output=[]
        for page in range(1 if doi else pages):
            data=await self.http.get(self.name,"https://api.core.ac.uk/v3/search/works",
                {"q":q,"limit":min(size,limit-len(output)),"offset":page*size},
                headers={"Authorization":"Bearer "+key})
            rows=data.get("results") or []
            output.extend(self.normalize(v) for v in rows if v.get("title"))
            retain_page(output)
            if len(rows)<size or len(output)>=limit:break
        return output[:limit]

    @staticmethod
    def normalize(v):
        url=safe_url(next((x.get("url") for x in v.get("links",[]) if x.get("type")=="display"),None))
        url=url or "https://core.ac.uk/works/"+str(v["id"])
        full=safe_url(v.get("downloadUrl"))
        lang=v.get("language") or {}
        if isinstance(lang,dict):lang=lang.get("code")
        return Material("core:"+str(v["id"]),v.get("documentType") or "journal-article",plain(v["title"]),
            tuple(plain(a.get("name")) if isinstance(a,dict) else plain(a) for a in v.get("authors",[])),
            year_of(v.get("yearPublished") or v.get("publishedDate")),normalize_doi(v.get("doi")),
            url,full,"open" if full else None,plain(v.get("abstract")) or None,"CORE",
            {"source_id":v["id"],"language":lang,"category":"scientific",
             **pdf_metadata(full,"CORE")})


class Zenodo:
    name="zenodo"
    def __init__(self,http):self.http=http

    async def search(self,query,doi=None,limit=20,*,filters=None):
        size,pages=page_options(self.http,limit)
        q=f'doi:"{doi}"' if doi else query
        clause=date_clause(filters,"publication_date")
        if clause and not doi:q="("+q+") AND "+clause
        if filters and (filters.open_only or filters.access_status=="open"):
            q="("+q+") AND access_right:open"
        # Public records search is intentionally unauthenticated. No deposit APIs.
        output=[]
        for page in range(1,1+(1 if doi else pages)):
            data=await self.http.get(self.name,"https://zenodo.org/api/records",
                {"q":q,"page":page,"size":min(size,limit-len(output)),"sort":"bestmatch"})
            rows=(data.get("hits") or {}).get("hits") or []
            output.extend(self.normalize(v) for v in rows if (v.get("metadata") or {}).get("title"))
            retain_page(output)
            if len(rows)<size or len(output)>=limit:break
        return output[:limit]

    @staticmethod
    def normalize(v):
        m=v["metadata"]; links=v.get("links") or {}
        access=m.get("access_right") or ((v.get("access") or {}).get("files"))
        opened=access in ("open","public")
        files=v.get("files") or []
        if isinstance(files,dict):files=list((files.get("entries") or files).values())
        pdf=next((safe_url((f.get("links") or {}).get("self") or (f.get("links") or {}).get("content"))
            for f in files if str(f.get("key") or f.get("name") or "").casefold().endswith(".pdf")),None)
        typ=m.get("resource_type") or m.get("upload_type")
        if isinstance(typ,dict):typ=typ.get("type")
        url=safe_url(links.get("self_html") or links.get("html")) or "https://zenodo.org/records/"+str(v["id"])
        creators=m.get("creators") or []
        concept_doi=normalize_doi(v.get('conceptdoi'))
        if not concept_doi and str(v.get('conceptrecid') or '').isdigit():
            concept_doi='10.5281/zenodo.'+str(v['conceptrecid'])
        return Material("zenodo:"+str(v["id"]),typ or "dataset",plain(m["title"]),
            tuple(plain(c.get("name") or (c.get("person_or_org") or {}).get("name")) for c in creators),
            year_of(m.get("publication_date")),normalize_doi(v.get("doi") or m.get("doi")),
            url,url if opened else None,"open" if opened else "restricted" if access in ("restricted","closed") else None,
            plain(m.get("description")) or None,"Zenodo",
            {"source_id":v["id"],"category":"scientific","language":m.get("language"),
             'concept_doi':concept_doi,'related_identifiers':m.get('related_identifiers') or [],
             'version':m.get('version'),'version_created':v.get('created'),
             "keywords":plain(m.get("keywords")),"files":files,
             **(pdf_metadata(pdf,"Zenodo",documented=True) if opened else {})})


class PubMed:
    name="pubmed"
    base="https://eutils.ncbi.nlm.nih.gov/entrez/eutils/"
    def __init__(self,http):self.http=http

    def params(self,values):
        p=dict(values,tool="BrilliantDetective")
        if getattr(self.http.config,"ncbi_api_key",""):p["api_key"]=self.http.config.ncbi_api_key
        if getattr(self.http.config,"contact_email",""):p["email"]=self.http.config.contact_email
        return p

    async def search(self,query,doi=None,limit=20,*,filters=None):
        kind,ident=identifier_query(query)
        if kind=="arxiv":return []
        size,pages=page_options(self.http,limit)
        term=f'"{doi}"[AID]' if doi else ident+"[PMID]" if kind=="pmid" else query
        exact=bool(doi or kind)
        ids=[]
        if kind=="pmcid":
            data=await self.http.get(self.name,self.base+"elink.fcgi",
                self.params({"dbfrom":"pmc","db":"pubmed","id":ident[3:],"retmode":"json"}))
            ids=[str(x) for group in data.get("linksets",[]) for links in group.get("linksetdbs",[])
                 if links.get("dbto")=="pubmed" for x in links.get("links",[])]
        else:
            clause=date_clause(filters,"",pattern="pubmed")
            if clause and not exact:term="("+term+") AND "+clause
            if filters and filters.language and not exact:
                term+=f" AND { {'en':'english','ru':'russian'}[filters.language]}[Language]"
            if filters and filters.material_types and not exact:
                types=['Journal Article' if t=='journal-article' else t for t in filters.material_types]
                term+=' AND ('+' OR '.join('"'+t.replace('"','')+'"[Publication Type]' for t in types)+')'
            if filters and not exact and (filters.open_only or filters.access_status=="open"):
                term+=" AND free full text[sb]"
            for page in range(1 if exact else pages):
                data=await self.http.get(self.name,self.base+"esearch.fcgi",
                    self.params({"db":"pubmed","term":term,"retmode":"json",
                        "retstart":page*size,"retmax":min(size,limit-len(ids)),"sort":"relevance"}))
                block=data.get("esearchresult") or {}
                if block.get("ERROR") or block.get("errorlist"):raise SourceUnavailable("PubMed отклонил поисковое выражение.")
                batch=block.get("idlist") or []
                ids+=batch
                if len(batch)<size or len(ids)>=limit:break
        if not ids:return []
        import xml.etree.ElementTree as ET
        text=await self.http.get_text(self.name,self.base+"efetch.fcgi",
            self.params({"db":"pubmed","id":",".join(dict.fromkeys(ids[:limit])),"retmode":"xml","rettype":"abstract"}))
        if not text:return []
        root=ET.fromstring(text)
        return [self.normalize(v) for v in root.findall(".//PubmedArticle")]

    @staticmethod
    def normalize(v):
        def text(path):return "".join(v.find(path).itertext()).strip() if v.find(path) is not None else ""
        pmid=text("./MedlineCitation/PMID")
        ids={n.get("IdType"):n.text for n in v.findall("./PubmedData/ArticleIdList/ArticleId")}
        abstract="\n".join((n.get("Label","")+": " if n.get("Label") else "")+"".join(n.itertext())
            for n in v.findall("./MedlineCitation/Article/Abstract/AbstractText"))
        authors=tuple(" ".join(filter(None,("".join(a.find("ForeName").itertext()) if a.find("ForeName") is not None else "",
                    "".join(a.find("LastName").itertext()) if a.find("LastName") is not None else "")))
            or "".join(a.itertext()) for a in v.findall("./MedlineCitation/Article/AuthorList/Author"))
        pmcid=ids.get("pmc")
        full="https://pmc.ncbi.nlm.nih.gov/articles/"+pmcid+"/" if pmcid else None
        return Material("pubmed:"+pmid,"journal-article",text("./MedlineCitation/Article/ArticleTitle"),authors,
            year_of(text("./MedlineCitation/Article/Journal/JournalIssue/PubDate/Year")
                or text("./MedlineCitation/Article/Journal/JournalIssue/PubDate/MedlineDate")),
            normalize_doi(ids.get("doi")),"https://pubmed.ncbi.nlm.nih.gov/"+pmid+"/",full,
            None,abstract or None,"PubMed",
            {"source_id":pmid,"pmid":pmid,"pmcid":pmcid,"category":"scientific",
             "language":text("./MedlineCitation/Article/Language"),
             "publication_types":[n.text for n in v.findall(".//PublicationType")],
             "keywords":"; ".join(n.text or "" for n in v.findall(".//MeshHeading/DescriptorName"))})


class Arxiv:
    name="arxiv"
    def __init__(self,http):self.http=http

    async def search(self,query,doi=None,limit=20,*,filters=None):
        import xml.etree.ElementTree as ET
        kind,ident=identifier_query(query)
        if kind in ("pmid","pmcid"):return []
        size,pages=page_options(self.http,limit)
        if re.search(r"\b(?:ti|au|abs|cat|id|all|co|jr):",query):
            q=query
        else:
            terms=re.findall(r"[^\W_]+",doi or query)
            q=" AND ".join('all:"'+t+'"' for t in terms)
        if filters and not (doi or kind) and (filters.year_from or filters.year_to):
            q+=f" AND submittedDate:[{filters.year_from or 1991}01010000 TO {filters.year_to or 2100}12312359]"
        output=[]
        for page in range(1 if kind or doi else pages):
            params={"id_list":ident,"max_results":1} if kind=="arxiv" else {
                "search_query":q,"start":page*size,"max_results":min(size,limit-len(output)),
                "sortBy":"relevance","sortOrder":"descending"}
            text=await self.http.get_text(self.name,"https://export.arxiv.org/api/query",params)
            if not text:break
            rows=ET.fromstring(text).findall("{http://www.w3.org/2005/Atom}entry")
            if any((v.findtext("{http://www.w3.org/2005/Atom}id") or "").endswith("/errors") for v in rows):
                raise SourceUnavailable("arXiv отклонил поисковое выражение.")
            output.extend(self.normalize(v) for v in rows)
            retain_page(output)
            if len(rows)<size or len(output)>=limit:break
        return output[:limit]

    @staticmethod
    def normalize(v):
        ns={"a":"http://www.w3.org/2005/Atom","x":"http://arxiv.org/schemas/atom"}
        ident=(v.findtext("a:id",default="",namespaces=ns)).rsplit("/abs/",1)[-1]
        pdf=next((l.get("href") for l in v.findall("a:link",ns) if l.get("type")=="application/pdf"),None)
        if pdf:pdf=re.sub(r"^http:","https:",pdf)
        url="https://arxiv.org/abs/"+ident
        return Material("arxiv:"+ident,"preprint",plain(v.findtext("a:title",default="",namespaces=ns)),
            tuple(a.findtext("a:name",default="",namespaces=ns) for a in v.findall("a:author",ns)),
            year_of(v.findtext("a:published",namespaces=ns)),normalize_doi(v.findtext("x:doi",namespaces=ns)),
            url,url,"open",plain(v.findtext("a:summary",namespaces=ns)) or None,"arXiv",
            {"source_id":ident,"arxiv_id":ident,"category":"scientific","language":"en",
             "keywords":"; ".join(c.get("term","") for c in v.findall("a:category",ns)),
             **pdf_metadata(pdf,"arXiv",documented=True)})


class EuropePMC:
    name="europepmc"
    def __init__(self,http):self.http=http

    async def search(self,query,doi=None,limit=20,*,filters=None):
        kind,ident=identifier_query(query)
        if kind=="arxiv":return []
        q=f'DOI:"{doi}"' if doi else f"EXT_ID:{ident} AND SRC:MED" if kind=="pmid" else f"PMCID:{ident}" if kind=="pmcid" else query
        exact=bool(doi or kind)
        clause=date_clause(filters,"FIRST_PDATE")
        if clause and not exact:q="("+q+") AND "+clause
        if filters and not exact:
            if filters.language:q+=" AND LANG:"+{"en":"eng","ru":"rus"}[filters.language]
            if filters.material_types:
                types=['Journal Article' if t=='journal-article' else t for t in filters.material_types]
                q+=' AND ('+' OR '.join('PUB_TYPE:"'+t.replace('"','')+'"' for t in types)+')'
            if filters.open_only or filters.access_status=="open":q+=" AND OPEN_ACCESS:Y"
        size,pages=page_options(self.http,limit)
        cursor="*";output=[]
        for _ in range(1 if exact else pages):
            data=await self.http.get(self.name,"https://www.ebi.ac.uk/europepmc/webservices/rest/search",
                {"query":q,"format":"json","resultType":"core","pageSize":min(size,limit-len(output)),"cursorMark":cursor})
            rows=(data.get("resultList") or {}).get("result") or []
            output.extend(self.normalize(v) for v in rows if v.get("title"))
            retain_page(output)
            nxt=data.get("nextCursorMark")
            if len(rows)<size or len(output)>=limit or not nxt or nxt==cursor:break
            cursor=nxt
        return output[:limit]

    @staticmethod
    def normalize(v):
        urls=(v.get("fullTextUrlList") or {}).get("fullTextUrl") or []
        available=[u for u in urls if str(u.get("availability","")).casefold() in ("open access","free")]
        pdf=next((u.get("url") for u in available if u.get("documentStyle")=="pdf"),None)
        full=next((u.get("url") for u in available if u.get("documentStyle")=="html"),None)
        pmcid=v.get("pmcid")
        if not full and pmcid:full="https://pmc.ncbi.nlm.nih.gov/articles/"+pmcid+"/"
        opened=v.get("isOpenAccess")=="Y"
        source=v.get("source","MED");ident=str(v["id"])
        authors=(v.get("authorList") or {}).get("author") or []
        return Material("europepmc:"+source+":"+ident,"preprint" if source=="PPR" else "journal-article",
            plain(v["title"]),tuple(plain(a.get("fullName")) for a in authors),
            year_of(v.get("firstPublicationDate") or v.get("pubYear")),normalize_doi(v.get("doi")),
            "https://europepmc.org/article/"+source+"/"+ident,safe_url(full),
            "open" if opened else None,plain(v.get("abstractText")) or None,"Europe PMC",
            {"source_id":ident,"pmid":v.get('pmid') or (ident if source=="MED" else None),"pmcid":pmcid,
             'publication_date':v.get('firstPublicationDate'),'source_publication_year':v.get('pubYear'),
             "language":v.get("language"),"category":"scientific",
             'publication_types':(v.get('pubTypeList') or {}).get('pubType',[]),
             "keywords":plain((v.get("keywordList") or {}).get("keyword")),
             **pdf_metadata(pdf,"Europe PMC",documented=True)})


def adapters(http):
    from statistics_sources import statistical_adapters
    return {**{a.name:a for a in (OpenAlex(http),Crossref(http),DataCite(http),WorldBank(http),
        Core(http),Zenodo(http),PubMed(http),Arxiv(http),EuropePMC(http))},**statistical_adapters(http)}


class AccessEnricher:
    """Unpaywall is DOI enrichment, never a thematic search source."""
    def __init__(self,http,pdf_transport=None):self.http,self.pdf_transport=http,pdf_transport

    async def identifiers(self, items, deadline=None):
        """Adapt paper-fetch idconv.enrich to the shared async transport.

        Copyright (c) 2026 PolarizeTech, MIT; notice in
        data/third_party_licenses.txt. ID-conversion covers biomedical records
        and a bounded DOI sample; it never overwrites a conflicting known ID.
        """
        from dataclasses import replace
        selected = [m for m in items if m.metadata.get('pmid') or m.metadata.get('pmcid')][:20]
        if getattr(self.http.config,'contact_email',''):
            selected += [m for m in items if m.doi and not m.metadata.get('pmid') and not m.metadata.get('pmcid')][
                :getattr(self.http.config,'enrich_dois',4)]
        if not selected or deadline is not None and perf_counter() >= deadline:
            return items
        groups={}
        for m in selected:
            kind='doi' if m.doi else 'pmid' if m.metadata.get('pmid') else 'pmcid'
            groups.setdefault(kind,[]).append(m.doi if kind=='doi' else str(m.metadata[kind]))
        records=[]
        for kind,values in groups.items():
            if deadline is not None and perf_counter()>=deadline:break
            params={'ids':','.join(dict.fromkeys(values)),'idtype':kind,'format':'json','tool':'BrilliantDetective'}
            if self.http.config.contact_email:params['email']=self.http.config.contact_email
            try:
                call = self.http.get('ncbi_idconv','https://pmc.ncbi.nlm.nih.gov/tools/idconv/api/v1/articles/',params,ttl=7*86400)
                data=await asyncio.wait_for(call,max(.01,deadline-perf_counter())) if deadline is not None else await call
                records.extend(data.get('records') or [])
            except (SourceUnavailable,asyncio.TimeoutError):continue
        output = []
        for item in items:
            ids = {'doi': normalize_doi(item.doi), 'pmid': item.metadata.get('pmid'), 'pmcid': item.metadata.get('pmcid')}
            record = next((r for r in records if r.get('status') != 'error' and
                any(v and str(r.get(k, '')).casefold() == str(v).casefold() for k, v in ids.items())), None)
            if record and not any(v and record.get(k) and str(v).casefold()!=str(record[k]).casefold() for k,v in ids.items()):
                md = dict(item.metadata, identifier_provenance='NCBI ID Converter')
                for k in ('pmid','pmcid'):
                    if record.get(k) and not md.get(k): md[k] = str(record[k])
                item = replace(item, doi=item.doi or normalize_doi(record.get('doi')), metadata=md)
            output.append(item)
        return output

    async def enrich(self,items,deadline=None):
        from dataclasses import replace
        output=list(items);notes=[];lookups=0
        maximum=getattr(self.http.config,"access_lookup_limit",5)
        email=getattr(self.http.config,"unpaywall_email","")
        for pos,item in enumerate(output):
            if deadline is not None and perf_counter() >= deadline:break
            if not item.doi or (item.open_url and item.metadata.get("pdf_url")):continue
            if lookups>=maximum or not email:break
            lookups+=1
            try:
                call=self.http.get("unpaywall","https://api.unpaywall.org/v2/"+quote(item.doi,safe=""),
                    {"email":email},ttl=7*86400)
                record=await asyncio.wait_for(call,max(.01,deadline-perf_counter())) if deadline is not None else await call
                loc=record.get("best_oa_location") or {}
                pdf=safe_url(loc.get("url_for_pdf"))
                full=safe_url(loc.get("url_for_landing_page") or loc.get("url"))
                md=dict(item.metadata)
                md["access_provenance"]="Unpaywall"
                md["oa_status"]=record.get("oa_status")
                if pdf:md.update(pdf_metadata(pdf,"Unpaywall",documented=True))
                output[pos]=replace(item,open_url=item.open_url or full or pdf,
                    access_status="open" if record.get("is_oa") is True else item.access_status,
                    metadata=md)
            except (SourceUnavailable, asyncio.TimeoutError):
                notes.append("Сведения Unpaywall временно недоступны; сохранены исходные ссылки.")
                break
        if items and any(m.doi and not m.open_url for m in items) and not email:
            notes.append("Unpaywall требует контактный email в UNPAYWALL_EMAIL или CROSSREF_CONTACT_EMAIL.")
        return output,list(dict.fromkeys(notes))

    async def verify_pdf(self,item):
        from dataclasses import replace
        try:return await asyncio.wait_for(self._verify_pdf(item),6.0)
        except asyncio.TimeoutError:
            data={'pdf_verified':False,'pdf_checked':True}
            url=safe_url(item.metadata.get('pdf_url'))
            if url:self.http.db.cache_put('pdf-check:'+hashlib.sha256(url.encode()).hexdigest(),data,900)
            return replace(item,metadata={**item.metadata,**data})

    async def _verify_pdf(self,item):
        from dataclasses import replace
        from urllib.parse import urlsplit,urljoin
        import ipaddress
        url=safe_url(item.metadata.get("pdf_url"))
        if not url:return item
        key="pdf-check:"+hashlib.sha256(url.encode()).hexdigest()
        cached=self.http.db.cache_get(key)
        if cached is not None:
            return replace(item,metadata={**item.metadata,**cached})
        async def public(value):
            p=urlsplit(value)
            if p.scheme not in ("http","https") or not p.hostname or p.username:return False
            if p.hostname.lower() in ("localhost","localhost.localdomain"):return False
            try:return ipaddress.ip_address(p.hostname).is_global
            except ValueError:
                if '.' not in p.hostname:return False
                if self.pdf_transport is not None:return True
                import socket
                try:
                    addresses=await asyncio.get_running_loop().getaddrinfo(p.hostname,p.port or (443 if p.scheme=='https' else 80),type=socket.SOCK_STREAM)
                    return bool(addresses) and all(ipaddress.ip_address(a[4][0]).is_global for a in addresses)
                except (OSError,ValueError):return False
        success=False;final=url
        try:
            async with httpx.AsyncClient(timeout=5,follow_redirects=False,transport=self.pdf_transport) as client:
                for _ in range(4):
                    if not await public(final):break
                    stats=REQUEST_STATS.get()
                    if stats is not None:
                        if stats.get("http_requests",0)>=getattr(self.http.config,"api_request_budget",48):break
                        stats["http_requests"]=stats.get("http_requests",0)+1
                    # Stream only the magic prefix, even when a server ignores Range.
                    async with client.stream("GET",final,headers={"Range":"bytes=0-7","Accept":"application/pdf"}) as r:
                        if r.is_redirect and r.headers.get("location"):
                            final=urljoin(final,r.headers["location"]);continue
                        if r.status_code not in (200,206):break
                        prefix=b""
                        async for chunk in r.aiter_bytes(chunk_size=8):
                            prefix+=chunk
                            if len(prefix)>=5:break
                        success=prefix.startswith(b"%PDF-")
                        break
        except httpx.HTTPError:
            pass
        data={"pdf_verified":success,"pdf_checked":True}
        if success:data["pdf_url"]=final
        self.http.db.cache_put(key,data,86400 if success else 900)
        return replace(item,metadata={**item.metadata,**data})
