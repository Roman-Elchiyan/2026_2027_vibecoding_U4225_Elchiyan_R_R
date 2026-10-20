"""Search boundary regressions: shared quotas, identity, SDMX and query logic."""
import asyncio
from dataclasses import replace
from pathlib import Path
import tempfile
from time import perf_counter
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock

import httpx

from external_sources import MetadataHTTP, REQUEST_STATS, SourceUnavailable, AccessEnricher
from lab_config import LabConfig
from lab_storage import LabDatabase
from query_planner import analyze_query, source_query, structured_match
from relevance import merge_candidates
from search import Material
from statistics_sources import validate_dimensions, SDMXSource,read_structure
from research import ResearchFilters,matches


class IdentityTests(unittest.TestCase):
    def test_statistical_dataset_cannot_satisfy_an_unknown_publication_language_filter(self):
        item=Material('imf:WEO','statistical-dataset','WEO',metadata={'category':'statistics','statistical_kind':'dataset'})
        f=ResearchFilters(topic='WEO',sources=('imf',),category='statistics',language='ru')
        self.assertFalse(matches(item,f))
        self.assertTrue(matches(item,replace(f,language=None)))

    def test_sdmx_core_representation_codelists_are_used_when_local_representation_is_absent(self):
        import sdmx
        from sdmx.model import v21 as model
        from sdmx.message import StructureMessage
        agency=model.Agency(id='TEST')
        codes=model.Codelist(id='CL_FREQ',maintainer=agency,version='1.0',
            urn='urn:sdmx:org.sdmx.infomodel.codelist.Codelist=TEST:CL_FREQ(1.0)')
        codes.append(model.Code(id='A',name={'en':'Annual'}))
        concept=model.Concept(id='FREQ');concept.core_representation=model.Representation(enumerated=codes)
        concepts=model.ConceptScheme(id='CS',maintainer=agency,version='1.0');concepts.append(concept)
        dsd=model.DataStructureDefinition(id='DSD',maintainer=agency,version='1.0')
        dsd.dimensions.append(model.Dimension(id='FREQ',order=1,concept_identity=concept))
        message=StructureMessage();message.structure['DSD']=dsd
        message.codelist['CL_FREQ']=codes;message.concept_scheme['CS']=concepts
        _,dims,labels,_=read_structure(sdmx.to_xml(message).decode(),'DSD')
        self.assertEqual(dims,['FREQ'])
        self.assertEqual(labels['FREQ'],{'A':'Annual'})
        with self.assertRaises(ValueError):validate_dimensions(dims,{'FREQ':'UNKNOWN'},labels)

    def test_comtrade_portal_is_not_dataset_identity(self):
        def commodity(code):
            return Material('comtrade:HS:'+code,'statistical-dataset','Wheat',source='UN Comtrade',
                url='https://comtradeplus.un.org/',metadata={'category':'statistics',
                    'dataset_ref':'HS:'+code,'commodity':code})
        a,b=commodity('1001'),commodity('100110')
        self.assertEqual(len(merge_candidates([[a,b]])),2)
        self.assertEqual(len(merge_candidates([[a,b],[a]])),2)

    def test_transitive_bridge_checks_entire_component_for_doi_conflicts(self):
        a=Material('a',None,'Long title',doi='10.1234/a',metadata={'pmid':'123'})
        b=Material('b',None,'Long title',metadata={'pmid':'123','pmcid':'PMC1'})
        c=Material('c',None,'Long title',doi='10.1234/b',metadata={'pmcid':'PMC1'})
        self.assertEqual(len(merge_candidates([[a],[b],[c]])),2)
        self.assertEqual(len(merge_candidates([[a],[b],[replace(c,doi=a.doi)]])),1)

    def test_repeated_provider_does_not_multiply_its_rank_vote(self):
        a=Material('a',None,'A',source='Crossref')
        b=Material('b',None,'B',source='OpenAlex')
        c=Material('c',None,'C',source='Europe PMC')
        self.assertEqual([m.id for m in merge_candidates([[b,c],[a]])],
            [m.id for m in merge_candidates([[b,c],[a],[a],[a]])])

    def test_boolean_not_never_becomes_positive_bibliographic_term(self):
        q='(cotton[Title/Abstract] OR Gossypium[Title/Abstract]) AND (irrigation[Title/Abstract] NOT wastewater[Title/Abstract])'
        tree=analyze_query(q).structured
        self.assertIsNotNone(tree)
        self.assertNotIn('wastewater',source_query(q,'crossref'))
        self.assertIn('NOT',source_query(q,'pubmed'))
        self.assertIn(' ANDNOT ',source_query(q,'arxiv'))
        self.assertTrue(structured_match(tree,Material('a',None,'Gossypium irrigation',abstract_or_snippet='Field trial')))
        self.assertFalse(structured_match(tree,Material('a',None,'Cotton irrigation',abstract_or_snippet='Wastewater experiment')))
        self.assertIsNone(structured_match(tree,Material('a',None,'Cotton irrigation')))

    def test_unknown_topic_keeps_complete_description_without_invented_aspects(self):
        query='Terahertz polariton tomography and error correction in quasicrystals'
        intent=analyze_query(query)
        self.assertEqual(intent.variants,(query,))
        self.assertFalse(intent.constraints)

    def test_dimension_names_and_codes_are_checked_against_real_schema(self):
        dims=['REF_AREA','FREQ','SEX','AGE']
        labels={'REF_AREA':{'DEU':'Germany'},'FREQ':{'A':'Annual'},'SEX':{'SEX_T':'Total'},'AGE':{'AGE_YTHADULT_YGE15':'15+'}}
        self.assertEqual(validate_dimensions(dims,{'ref-area':'DEU','freq':'A'},labels),{'REF_AREA':'DEU','FREQ':'A'})
        with self.assertRaisesRegex(ValueError,'FREQ'):
            validate_dimensions(dims,{'FREQUENCY':'A'},labels)
        with self.assertRaisesRegex(ValueError,'CLASSIF1'):
            validate_dimensions(dims,{'CLASSIF1':'AGE_YTHADULT_YGE15'},labels)
        with self.assertRaisesRegex(ValueError,'WRONG'):
            validate_dimensions(dims,{'AGE':'WRONG'},labels)


class QuotaTests(unittest.IsolatedAsyncioTestCase):
    async def test_one_slow_provider_cannot_reserve_all_global_slots_with_its_waiters(self):
        release=asyncio.Event();started=asyncio.Event()
        async def response(request):
            if request.url.host=='slow.example.org':
                started.set();await release.wait()
            return httpx.Response(200,json={'ok':True})
        with tempfile.TemporaryDirectory() as directory:
            db=LabDatabase(Path(directory)/'test.db');db.initialize()
            http=MetadataHTTP(db,LabConfig(),httpx.MockTransport(response))
            slow=[asyncio.create_task(http.get('openalex','https://slow.example.org/',{'q':i})) for i in range(3)]
            try:
                await started.wait()
                result=await asyncio.wait_for(http.get('crossref','https://fast.example.org/',{}),.5)
                self.assertEqual(result,{'ok':True})
            finally:
                for task in slow:task.cancel()
                await asyncio.gather(*slow,return_exceptions=True)
                await http.close()

    async def test_id_conversion_batches_do_not_mix_identifier_types(self):
        def response(source,url,params,**kwargs):
            self.assertEqual(url,'https://pmc.ncbi.nlm.nih.gov/tools/idconv/api/v1/articles/')
            if params['idtype']=='doi':
                self.assertEqual(params['ids'],'10.1234/a')
                return {'records':[{'doi':'10.1234/a','pmid':'1','pmcid':'PMC1'}]}
            self.assertEqual(params['idtype'],'pmid')
            self.assertEqual(params['ids'],'2')
            return {'records':[{'doi':'10.1234/b','pmid':'2','pmcid':'PMC2'}]}
        http=SimpleNamespace(config=LabConfig(),get=AsyncMock(side_effect=response))
        a=Material('a',None,'A',doi='10.1234/a',metadata={'pmid':'1'})
        b=Material('b',None,'B',metadata={'pmid':'2'})
        rows=await AccessEnricher(http).identifiers([a,b],perf_counter()+1)
        self.assertEqual([m.metadata['pmcid'] for m in rows],['PMC1','PMC2'])
        self.assertEqual(rows[1].doi,'10.1234/b')
        self.assertEqual(http.get.await_count,2)

    async def test_bounded_stream_does_not_decode_gzip_twice(self):
        import gzip
        with tempfile.TemporaryDirectory() as directory:
            db=LabDatabase(Path(directory)/'test.db');db.initialize()
            http=MetadataHTTP(db,LabConfig(),httpx.MockTransport(lambda r:httpx.Response(200,
                headers={'Content-Encoding':'gzip','Content-Type':'application/json'},content=gzip.compress(b'{"results":[1]}'))))
            try:self.assertEqual(await http.get('crossref','https://api.crossref.org/works',{}),{'results':[1]})
            finally:await http.close()

    async def test_budget_is_atomic_after_source_delay_and_also_bounds_retries(self):
        with tempfile.TemporaryDirectory() as directory:
            db=LabDatabase(Path(directory)/'test.db');db.initialize()
            calls=[]
            def response(request):
                calls.append(request.url.host)
                return httpx.Response(429,headers={'Retry-After':'0'})
            http=MetadataHTTP(db,LabConfig(api_request_budget=1),httpx.MockTransport(response))
            http.last={source:perf_counter() for source in ('openalex','crossref','datacite')}
            stats={'http_requests':0,'cache_hits':0,'logical_requests':0}
            token=REQUEST_STATS.set(stats)
            try:
                results=await asyncio.gather(*(http.get(s,'https://'+s+'.example.org',{})
                    for s in http.last),return_exceptions=True)
                self.assertEqual(len(calls),1)
                self.assertEqual(stats['http_requests'],1)
                self.assertTrue(all(isinstance(r,SourceUnavailable) for r in results))
            finally:
                REQUEST_STATS.reset(token);await http.close()

    async def test_credential_headers_are_not_forwarded_to_redirect_target(self):
        with tempfile.TemporaryDirectory() as directory:
            db=LabDatabase(Path(directory)/'test.db');db.initialize()
            hosts=[]
            def response(request):
                hosts.append(request.url.host)
                return httpx.Response(302,headers={'Location':'https://other.example.org/'})
            http=MetadataHTTP(db,LabConfig(),httpx.MockTransport(response))
            try:
                with self.assertRaises(SourceUnavailable):
                    await http.get('comtrade','https://comtradeapi.un.org/data',{},headers={'Ocp-Apim-Subscription-Key':'test-only'})
                self.assertEqual(hosts,['comtradeapi.un.org'])
            finally:await http.close()

    async def test_access_enrichment_respects_remaining_search_deadline(self):
        async def slow(*args,**kwargs):await asyncio.sleep(5)
        http=SimpleNamespace(config=LabConfig(unpaywall_email='research@example.org'),get=slow)
        item=Material('a',None,'Article',doi='10.1234/a')
        started=perf_counter()
        items,warnings=await AccessEnricher(http).enrich([item],started+.03)
        self.assertEqual(items,[item]);self.assertTrue(warnings)
        self.assertLess(perf_counter()-started,.5)
