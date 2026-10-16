import asyncio
from dataclasses import replace
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import AsyncMock, patch

import httpx
from config import ROOT
from external_sources import MetadataHTTP, OpenAlex, Crossref, DataCite, WorldBank, SourceUnavailable
from lab_config import LabConfig
from lab_storage import LabDatabase
from research import ResearchFilters, ResearchService, matches
from search import Material, SearchFilters, SearchResult, SearchService
from sources import CsvSource
from translation import Translator


class ResearchTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        asyncio.get_running_loop().slow_callback_duration = 5
        self.temp = tempfile.TemporaryDirectory()
        self.db = LabDatabase(Path(self.temp.name)/'test.db')
        self.db.initialize()
        self.db.register(100)
        self.config = LabConfig(semantic_enabled=False,translation_enabled=False)
        self.local = SearchService(CsvSource(ROOT/'data/ploswater.csv'),self.config)
        self.translator = Translator(self.db,enabled=False)
        self.service = ResearchService(self.local,self.db,self.config,self.translator)
        self.item = Material('crossref:1','journal-article','Water supply',year=2024,doi='10.1234/water',source='Crossref',
            metadata={'category':'scientific','language':'en'})

    async def asyncTearDown(self):
        await self.service.close()
        self.local.close()
        self.temp.cleanup()

    async def test_selected_source_only(self):
        for adapter in self.service.sources.values(): adapter.search = AsyncMock(return_value=[self.item])
        result = await self.service.search(ResearchFilters(topic='water',sources=('crossref',)))
        self.assertEqual(result.items,[self.item])
        self.service.sources['crossref'].search.assert_awaited_once()
        self.service.sources['openalex'].search.assert_not_awaited()

    async def test_partial_failure_retains_other_api_and_warning(self):
        self.service.sources['openalex'].search = AsyncMock(side_effect=SourceUnavailable('timeout'))
        self.service.sources['crossref'].search = AsyncMock(return_value=[self.item])
        result = await self.service.search(ResearchFilters(topic='water',sources=('openalex','crossref')))
        self.assertEqual(result.items,[self.item])
        self.assertIn('OpenAlex',result.warnings[0])
        self.assertIn('Повторить поиск',result.warnings[0])

    async def test_strict_doi_does_not_return_similar_work(self):
        self.service.sources['crossref'].search = AsyncMock(return_value=[self.item])
        result = await self.service.search(ResearchFilters(topic='10.1234/absent',sources=('crossref',)))
        self.assertEqual(result.items,[])

    async def test_malformed_doi_never_becomes_semantic_query(self):
        self.service.sources['crossref'].search = AsyncMock(return_value=[self.item])
        result = await self.service.search(ResearchFilters(topic='doi: invalid',sources=('crossref',)))
        self.assertEqual(result.items,[])
        self.service.sources['crossref'].search.assert_not_awaited()

    async def test_dedup_keeps_provenance(self):
        self.service.sources['crossref'].search = AsyncMock(return_value=[self.item])
        self.service.sources['openalex'].search = AsyncMock(return_value=[replace(self.item,id='oa:1',source='OpenAlex')])
        result = await self.service.search(ResearchFilters(topic='water',sources=('crossref','openalex')))
        self.assertEqual(len(result.items),1)
        with self.db.connect() as c:
            self.assertEqual(c.execute('SELECT COUNT(*) FROM materials').fetchone()[0],1)
            self.assertEqual(c.execute('SELECT COUNT(*) FROM material_sources').fetchone()[0],2)

    async def test_year_language_category_type_access_filters(self):
        f = ResearchFilters(topic='water',sources=('crossref',),year_from=2024,year_to=2024,language='en',category='scientific',material_types=('journal-article',))
        self.assertTrue(matches(self.item,f))
        for field,value in [('year_from',2025),('language','ru'),('category','analytical'),('access_status','restricted'),('material_types',('Dataset',))]:
            self.assertFalse(matches(self.item,replace(f,**{field:value})))
        self.assertTrue(matches(self.item,replace(f,access_status='unknown')))
        self.assertFalse(matches(self.item,replace(f,access_status='open')))

    async def test_snapshots_keep_selected_provider_and_favorites_after_restart(self):
        local = replace(self.item,id='local:1',source='PLOS Water',access_status=None)
        first = SearchResult([local],[],'a'*32,1)
        self.db.save_search(100,SearchFilters(topic='water'),first)
        external = replace(self.item,id='external:1',access_status='open')
        result = SearchResult([external],[],'b'*32,1)
        self.db.save_search(100,ResearchFilters(topic='water',sources=('crossref',)),result)
        self.assertEqual(self.db.result_item(100,result.search_id,0).source,'Crossref')
        self.assertEqual(self.db.result_item(100,first.search_id,0).source,'PLOS Water')
        self.db.set_favorite(100,result.search_id,0,True)
        reopened = LabDatabase(self.db.path)
        reopened.initialize()
        self.assertTrue(reopened.is_favorite(100,external.id))
        self.assertEqual(reopened.get_page(100,result.search_id,0)[0][1].access_status,'open')

    async def test_translation_uses_english_for_api(self):
        self.translator.query = AsyncMock(return_value=('water supply',''))
        self.service.sources['crossref'].search = AsyncMock(return_value=[self.item])
        await self.service.search(ResearchFilters(topic='водоснабжение',sources=('crossref',)))
        self.assertEqual(self.service.sources['crossref'].search.call_args.args[0],'water supply')

    async def test_no_openai_or_proxy_required_for_local(self):
        result = await self.service.search(ResearchFilters(topic='water',sources=('local',)))
        self.assertTrue(result.items)

    async def test_http_429_retries_once_then_caches(self):
        count = 0
        def handler(request):
            nonlocal count
            count += 1
            return httpx.Response(429,headers={'Retry-After':'0'}) if count == 1 else httpx.Response(200,json={'results':[]})
        http = MetadataHTTP(self.db,self.config,httpx.MockTransport(handler))
        try:
            self.assertEqual(await http.get('openalex','https://api.openalex.org/works',{'search':'water','api_key':'private'}),{'results':[]})
            await http.get('openalex','https://api.openalex.org/works',{'search':'water','api_key':'private'})
            self.assertEqual(count,2)
            with self.db.connect() as c: self.assertNotIn('private',str([tuple(r) for r in c.execute('SELECT * FROM cache')]))
        finally: await http.close()

    async def test_http_5xx_is_bounded_and_404_empty(self):
        count = 0
        def handler(request):
            nonlocal count
            count += 1
            return httpx.Response(503)
        http = MetadataHTTP(self.db,self.config,httpx.MockTransport(handler))
        try:
            with self.assertRaises(SourceUnavailable): await http.get('crossref','https://api.crossref.org/works',{})
            self.assertEqual(count,2)
        finally: await http.close()

    async def test_argos_cache_and_unavailable_fallback(self):
        translator = Translator(self.db,True)
        try:
            with patch.object(translator,'_translate',return_value='water supply') as call:
                for _ in range(2): self.assertEqual(await translator.translate('водоснабжение'),('water supply',''))
                self.assertEqual(call.call_count,1)
            with patch.object(translator,'_translate',side_effect=ImportError):
                result,warning = await translator.translate('другой запрос')
                self.assertEqual(result,'другой запрос')
                self.assertTrue(warning)
            with self.assertRaises(ValueError): await translator.translate('x','de','en')
        finally: translator.close()

    async def test_argos_timeout_does_not_queue_workers(self):
        import threading
        release = threading.Event()
        translator = Translator(self.db,True,0.02)
        try:
            with patch.object(translator,'_translate',side_effect=lambda *a: release.wait(1)) as call:
                _,warning = await translator.translate('занято')
                self.assertTrue(warning)
                _,warning = await translator.translate('ещё запрос')
                self.assertIn('занят',warning)
                self.assertEqual(call.call_count,1)
        finally:
            release.set()
            translator.close()


class NormalizationTests(unittest.TestCase):
    def test_openalex_abstract_and_explicit_access(self):
        item = OpenAlex.normalize({'id':'https://openalex.org/W1','title':'A','publication_year':2024,'doi':'https://doi.org/10.1234/X',
            'abstract_inverted_index':{'Water':[0],'quality':[1]},'open_access':{'is_oa':False}})
        self.assertEqual(item.abstract_or_snippet,'Water quality')
        self.assertEqual(item.doi,'10.1234/x')
        self.assertEqual(item.access_status,'restricted')

    def test_crossref_does_not_infer_access_from_license(self):
        item = Crossref.normalize({'DOI':'10.1234/x','title':['<i>Water</i>'],'published':{'date-parts':[[2023]]},'license':[{'URL':'https://example.org'}]})
        self.assertEqual(item.title,'Water')
        self.assertEqual(item.year,2023)
        self.assertIsNone(item.access_status)

    def test_datacite_dataset_and_explicit_cc(self):
        item = DataCite.normalize({'id':'10.1234/x','attributes':{'titles':[{'title':'Data'}],'types':{'resourceTypeGeneral':'Dataset'},'rightsList':[{'rightsIdentifier':'cc-by-4.0'}]}})
        self.assertEqual(item.material_type,'Dataset')
        self.assertEqual(item.access_status,'open')

    def test_worldbank_requires_disclosed_and_download(self):
        item = WorldBank.normalize('1',{'display_title':'Water','docdt':'2024-01-01','lang':'English','pdfurl':'https://documents1.worldbank.org/a.pdf','disclstat':'Disclosed'})
        self.assertEqual(item.year,2024)
        self.assertEqual(item.metadata['language'],'en')
        self.assertEqual(item.access_status,'open')
        self.assertIsNone(WorldBank.normalize('2',{'display_title':'Water','url':'https://worldbank.org'}).access_status)

    def test_filter_validation_and_roundtrip(self):
        f = ResearchFilters(topic='water',sources=('crossref',),language='en',max_results=10)
        f.validate()
        self.assertEqual(ResearchFilters.from_dict(f.to_dict()),f)
        for options in ({'sources':()},{'language':'fr'},{'max_results':100},{'access_status':'paid'},{'category':'made-up'}):
            with self.assertRaises(ValueError): replace(f,**options).validate()
