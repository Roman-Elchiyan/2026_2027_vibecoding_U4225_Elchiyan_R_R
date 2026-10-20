"""Protocol, pagination, data meaning and privacy tests for the added sources."""
import asyncio
from dataclasses import replace
import json
import os
from pathlib import Path
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock,patch
import xml.etree.ElementTree as ET
import httpx

from external_sources import (MetadataHTTP,REQUEST_STATS,SourceUnavailable,OpenAlex,Core,Zenodo,
    PubMed,Arxiv,EuropePMC,AccessEnricher,adapters,identifier_query)
from lab_config import LabConfig,load_lab_config
from lab_storage import LabDatabase
from research import ResearchFilters,ResearchService
from search import Material,SearchService
from sources import CsvSource
from config import ROOT
from translation import Translator
from statistics_sources import Eurostat,Comtrade,StatisticalObservation,parse_observations

class MemoryCache:
    def __init__(self):self.entries={}
    def cache_get(self,key):return self.entries.get(key)
    def cache_put(self,key,value,ttl):self.entries[key]=value

class TransportTests(unittest.IsolatedAsyncioTestCase):
    async def test_authentication_not_in_cache_and_request_budget_is_shared(self):
        seen=[]
        def handler(r):
            seen.append(r)
            return httpx.Response(200,json={"results":[]})
        cache=MemoryCache()
        config=LabConfig(api_request_budget=1)
        http=MetadataHTTP(cache,config,httpx.MockTransport(handler))
        token=REQUEST_STATS.set({"http_requests":0})
        try:
            await http.get("core","https://api.core.ac.uk/v3/search/works",{"q":"water"},
                headers={"Authorization":"Bearer synthetic-secret-for-transport"})
            self.assertNotIn("synthetic-secret",repr(cache.entries))
            self.assertEqual(seen[0].headers["Authorization"],"Bearer synthetic-secret-for-transport")
            with self.assertRaises(SourceUnavailable):
                await http.get("core","https://api.core.ac.uk/v3/search/works",{"q":"other"})
            self.assertEqual(len(seen),1)
        finally:
            REQUEST_STATS.reset(token);await http.close()

    async def test_openalex_second_page_uses_cursor_and_filters(self):
        seen=[]
        def handler(r):
            seen.append(r)
            cursor=r.url.params.get("cursor")
            i=1 if cursor=="*" else 2
            return httpx.Response(200,json={"results":[{"id":"https://openalex.org/W"+str(i),"title":"Water "+str(i)}],
                "meta":{"next_cursor":"next" if i==1 else None}})
        http=MetadataHTTP(MemoryCache(),LabConfig(api_page_size=1),httpx.MockTransport(handler))
        try:
            rows=await OpenAlex(http).search("water",limit=2,filters=ResearchFilters(topic="water",year_from=2020))
            self.assertEqual([m.id for m in rows],["openalex:W1","openalex:W2"])
            self.assertEqual(seen[1].url.params["cursor"],"next")
            self.assertIn("from_publication_date:2020",seen[1].url.params["filter"])
        finally:await http.close()

    async def test_unpaywall_no_open_copy_remains_unknown(self):
        http=SimpleNamespace(config=LabConfig(unpaywall_email="synthetic@example.test"),
            get=AsyncMock(return_value={"is_oa":False,"oa_status":"closed"}))
        item=Material("m",None,"Title",doi="10.1234/a")
        rows,_=await AccessEnricher(http).enrich([item])
        self.assertIsNone(rows[0].access_status)
        self.assertEqual(rows[0].metadata["access_provenance"],"Unpaywall")

    async def test_pdf_button_requires_magic_not_a_landing_page(self):
        from handlers import card_keyboard
        item=Material("m",None,"Title",url="https://example.org/landing",
            metadata={"pdf_url":"https://example.org/file.pdf"})
        seen=[]
        def handler(r):
            seen.append(r)
            return httpx.Response(200,content=b"<html>not PDF</html>",headers={"Content-Type":"text/html"})
        http=SimpleNamespace(db=MemoryCache(),config=LabConfig())
        checked=await AccessEnricher(http,httpx.MockTransport(handler)).verify_pdf(item)
        self.assertFalse(checked.metadata["pdf_verified"])
        self.assertFalse(any(b.text=="Открыть PDF" for row in card_keyboard(checked,"x",0,False).inline_keyboard for b in row))
        def pdf(r):return httpx.Response(206,content=b"%PDF-1.7 body",headers={"Content-Type":"application/pdf"})
        http.db=MemoryCache()
        checked=await AccessEnricher(http,httpx.MockTransport(pdf)).verify_pdf(item)
        self.assertTrue(checked.metadata["pdf_verified"])
        self.assertTrue(any(b.text=="Открыть PDF" for row in card_keyboard(checked,"x",0,False).inline_keyboard for b in row))

class ScientificProtocols(unittest.IsolatedAsyncioTestCase):
    async def test_core_requires_key_and_uses_main_config(self):
        http=SimpleNamespace(config=LabConfig(),get=AsyncMock())
        with self.assertRaises(SourceUnavailable):await Core(http).search("water")
        http.config=replace(http.config,core_api_key="synthetic-core")
        http.get.return_value={"results":[]}
        await Core(http).search("water",filters=ResearchFilters(topic="water",year_from=2022))
        call=http.get.call_args
        self.assertEqual(call.kwargs["headers"]["Authorization"],"Bearer synthetic-core")
        self.assertIn("yearPublished>=2022",call.args[2]["q"])

    async def test_zenodo_public_search_needs_no_token_and_paginates(self):
        http=SimpleNamespace(config=LabConfig(api_page_size=1,zenodo_api_token="not-needed"),
            get=AsyncMock(side_effect=[
                {"hits":{"hits":[{"id":1,"metadata":{"title":"A","access_right":"open"}}]}},
                {"hits":{"hits":[{"id":2,"metadata":{"title":"B","access_right":"open"}}]}}]))
        rows=await Zenodo(http).search("water",limit=2)
        self.assertEqual(len(rows),2)
        self.assertEqual(http.get.call_args_list[1].args[2]["page"],2)
        self.assertNotIn("headers",http.get.call_args.kwargs)

    async def test_pubmed_fetches_real_ids_and_parses_abstract_not_author_affiliation(self):
        xml="""<PubmedArticleSet><PubmedArticle><MedlineCitation><PMID>123</PMID>
        <Article><ArticleTitle>Water <i>quality</i></ArticleTitle><Abstract><AbstractText Label="RESULTS">A result.</AbstractText></Abstract>
        <Language>eng</Language><Journal><JournalIssue><PubDate><Year>2024</Year></PubDate></JournalIssue></Journal></Article>
        </MedlineCitation><PubmedData><ArticleIdList><ArticleId IdType="doi">10.1234/A</ArticleId><ArticleId IdType="pmc">PMC1</ArticleId></ArticleIdList></PubmedData>
        </PubmedArticle></PubmedArticleSet>"""
        http=SimpleNamespace(config=LabConfig(ncbi_api_key="synthetic-ncbi"),
            get=AsyncMock(return_value={"esearchresult":{"idlist":["123"]}}),get_text=AsyncMock(return_value=xml))
        rows=await PubMed(http).search("PMID:123",limit=1)
        self.assertEqual(rows[0].doi,"10.1234/a")
        self.assertEqual(rows[0].metadata["pmcid"],"PMC1")
        self.assertEqual(rows[0].abstract_or_snippet,"RESULTS: A result.")
        self.assertEqual(http.get.call_args.args[2]["api_key"],"synthetic-ncbi")
        self.assertEqual(http.get_text.call_args.args[2]["id"],"123")

    async def test_arxiv_id_is_preserved_and_pdf_is_documented_not_verified(self):
        xml="""<feed xmlns="http://www.w3.org/2005/Atom" xmlns:x="http://arxiv.org/schemas/atom">
        <entry><id>http://arxiv.org/abs/2401.12345v2</id><title>A preprint</title><published>2024-01-01</published>
        <summary>Abstract</summary><link href="http://arxiv.org/pdf/2401.12345v2" type="application/pdf"/></entry></feed>"""
        http=SimpleNamespace(config=LabConfig(),get_text=AsyncMock(return_value=xml))
        rows=await Arxiv(http).search("arXiv:2401.12345v2")
        self.assertEqual(http.get_text.call_args.args[2]["id_list"],"2401.12345v2")
        self.assertEqual(rows[0].material_type,"preprint")
        self.assertEqual(rows[0].metadata["pdf_url"],"https://arxiv.org/pdf/2401.12345v2")
        self.assertFalse(rows[0].metadata["pdf_verified"])

    async def test_europepmc_cursor_server_filters_and_identifiers(self):
        http=SimpleNamespace(config=LabConfig(api_page_size=1),get=AsyncMock(side_effect=[
            {"resultList":{"result":[{"id":"1","source":"MED","pmcid":"PMC1","title":"A"}]},"nextCursorMark":"next"},
            {"resultList":{"result":[{"id":"2","source":"MED","title":"B"}]}}]))
        rows=await EuropePMC(http).search("water",limit=2,
            filters=ResearchFilters(topic="water",year_from=2023,language="en",access_status="open"))
        self.assertEqual(len(rows),2)
        params=http.get.call_args.args[2]
        self.assertIn("FIRST_PDATE",params["query"])
        self.assertIn("LANG:eng",params["query"])
        self.assertIn("OPEN_ACCESS:Y",params["query"])
        self.assertEqual(params["cursorMark"],"next")
        self.assertEqual(rows[0].metadata["pmid"],"1")

    async def test_neutral_key_names_loaded_without_exposing_them(self):
        fake={k:"synthetic-"+str(i) for i,k in enumerate((
            "CORE_API_KEY","ZENODO_API_TOKEN","NCBI_API_KEY","UN_COMTRADE_API_KEY_1","UN_COMTRADE_API_KEY_2"))}
        with patch.dict(os.environ,fake,clear=True),patch("lab_config.load_dotenv"),patch("config.load_dotenv"),patch("lab_config.dotenv_values",return_value={}):
            config=load_lab_config(require_token=False)
        self.assertEqual(config.un_comtrade_api_key_2,fake["UN_COMTRADE_API_KEY_2"])
        self.assertEqual(config.core_api_key,fake["CORE_API_KEY"])
        for v in fake.values():self.assertNotIn(v,repr(config))
        self.assertEqual(identifier_query("PMCID:PMC123"),("pmcid","PMC123"))

class StatisticalTests(unittest.IsolatedAsyncioTestCase):
    async def test_sdmx_specific_and_generic_keep_values_and_dimensions(self):
        specific='<Data><DataSet><Series COUNTRY="DEU" INDICATOR="GDP" SCALE="0"><Obs TIME_PERIOD="2023" OBS_VALUE="0"/></Series></DataSet></Data>'
        generic='<GenericData><DataSet><Series><SeriesKey><Value id="REF_AREA" value="FRA"/></SeriesKey><Obs><ObsDimension value="2024"/><ObsValue value="2.5"/></Obs></Series></DataSet></GenericData>'
        rows=parse_observations(specific)
        self.assertEqual(rows[0]["OBS_VALUE"],"0")
        self.assertEqual(rows[0]["COUNTRY"],"DEU")
        self.assertEqual(parse_observations(generic)[0]["TIME_PERIOD"],"2024")

    async def test_eurostat_jsonstat_axes_and_zero(self):
        data={"id":["geo","unit","time"],"size":[2,1,2],"label":"Population",
            "dimension":{"geo":{"category":{"index":{"DE":0,"FR":1},"label":{"DE":"Germany","FR":"France"}}},
              "unit":{"category":{"index":{"NR":0},"label":{"NR":"persons"}}},
              "time":{"category":{"index":{"2023":0,"2024":1}}}},
            "value":{"0":0,"1":3,"2":4,"3":5}}
        http=SimpleNamespace(get=AsyncMock(return_value=data))
        item=Material("eurostat:ESTAT,TPS00001,1.0","statistical-dataset","Population",
            metadata={"dataset_ref":"ESTAT,TPS00001,1.0"})
        provider=Eurostat(http)
        provider.schema=AsyncMock(return_value=(None,['geo','unit'],{'geo':{'DE':'Germany','FR':'France'},'unit':{'NR':'persons'}},{}))
        rows=await provider.data(item,ResearchFilters(topic="population",territories=("DE",),max_results=5,year_from=2023))
        self.assertEqual(rows[0].metadata["value"],0)
        self.assertEqual(rows[0].metadata["unit"],"persons")
        self.assertEqual(rows[0].metadata["territory"],"Germany")
        self.assertEqual(rows[1].metadata["period"],"2024")
        self.assertEqual(rows[0].material_type,"statistical-series")
        self.assertTrue(all(row.metadata['territory']=='Germany' for row in rows))

    async def test_comtrade_explicit_slot_and_no_key_rotation(self):
        config=LabConfig(un_comtrade_api_key_1="synthetic-one",un_comtrade_api_key_2="synthetic-two",un_comtrade_key_slot=2)
        http=SimpleNamespace(config=config,get=AsyncMock(side_effect=[
            {"results":[{"id":276,"reporterCodeIsoAlpha3":"DEU","reporterCodeIsoAlpha2":"DE"}]},
            {"data":[{"primaryValue":0,"cmdDesc":"Wheat","flowCode":"M","reporterCode":276,"reporterDesc":"Germany","period":"2023"},
                {"primaryValue":10,"cmdDesc":"Wheat","flowCode":"M","reporterCode":276,"period":"2022"},
                {"primaryValue":20,"cmdDesc":"Wheat","flowCode":"M","reporterCode":250,"period":"2023"}]}]))
        item=Material("comtrade:HS:1001","statistical-dataset","Wheat",metadata={"dataset_ref":"HS:1001","commodity":"1001"})
        rows=await Comtrade(http).data(item,ResearchFilters(topic="wheat",territories=("DEU",),year_from=2023))
        call=http.get.call_args
        self.assertIn("/data/v1/get/",call.args[1])
        self.assertEqual(call.kwargs["headers"]["Ocp-Apim-Subscription-Key"],"synthetic-two")
        self.assertNotIn("synthetic-two",rows[0].url)
        self.assertEqual(rows[0].metadata["value"],0)
        self.assertEqual(len(rows),1)

    async def test_statistical_search_bypasses_both_models(self):
        with tempfile.TemporaryDirectory() as directory:
            db=LabDatabase(Path(directory)/"test.db");db.initialize()
            config=LabConfig(semantic_enabled=False,external_semantic_enabled=False)
            local=SearchService(CsvSource(ROOT/"data/ploswater.csv"),config)
            service=ResearchService(local,db,config,Translator(db,False))
            item=Material("eurostat:ESTAT,TPS00001,1.0","statistical-dataset","Population",
                access_status="open",metadata={"category":"statistics","statistical_kind":"dataset","dataset_ref":"ESTAT,TPS00001,1.0"})
            service.sources["eurostat"].search=AsyncMock(return_value=[item])
            service.reranker.rank=AsyncMock(side_effect=AssertionError("Statistics must not use CE"))
            try:
                result=await service.search(ResearchFilters(topic="population",sources=("eurostat",),category="statistics",year_from=2020))
                self.assertEqual(result.items,[item])
                service.reranker.rank.assert_not_awaited()
                self.assertIn("core",service.sources)
                self.assertNotIn("semantic_scholar",service.sources)
            finally:await service.close();local.close()
