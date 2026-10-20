"""Retrieval regressions using explicit fixtures, not claims about real weights."""
import asyncio
from dataclasses import replace
import json
import os
from pathlib import Path
import tempfile
import threading
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock, patch

import numpy as np

from config import ROOT
from external_semantic import rerank
from lab_config import LabConfig
from lab_storage import LabDatabase
from query_planner import analyze_query
from relevance import classify, judge, merge_candidates, load_calibration
from research import ResearchFilters, ResearchService
from reranker import CrossEncoderRanker
from search import Material, SearchService
from semantic_search import SemanticSearch
from sources import CsvSource
from translation import Translator

QUERY = 'Cotton production technology and economic efficiency'
GOOD = Material('fixture:good', 'journal-article', 'Technology and economic efficiency of cotton production',
    year=2024, source='Crossref', abstract_or_snippet='Cotton cultivation technology, farming costs and economic efficiency.')
PARTIAL = Material('fixture:partial', 'journal-article', 'Cotton cultivation under drip irrigation',
    year=2024, source='Crossref', abstract_or_snippet='Cotton crop yields and irrigation.')
BAD = Material('fixture:bad', 'journal-article', 'Cotton fibres for efficient wastewater treatment',
    year=2024, source='Crossref', abstract_or_snippet='A cotton fibre sorbent removes dyes from wastewater.')


class IntentTests(unittest.TestCase):
    def test_variants_cover_aspects_without_invented_geography(self):
        intent = analyze_query('Технологии и экономическая эффективность производства хлопка', QUERY)
        self.assertEqual(intent.variants, (QUERY, intent.original))
        self.assertEqual(intent.constraints, ())
        self.assertNotIn('Russia', ' '.join(intent.variants))
        self.assertEqual(intent.aspects, ())  # free prose is not guessed from a closed vocabulary

    def test_paid_plan_cannot_remove_original_criteria_or_geography(self):
        intent = analyze_query(QUERY + ' in India', ai_plan={'queries': ['cotton technology']})
        self.assertEqual(intent.variants[0], QUERY + ' in India')
        self.assertIn('economic', [f.name for f in intent.objects])
        self.assertIn('india', [f.name for f in intent.objects])
        self.assertNotEqual(judge(intent, replace(GOOD, abstract_or_snippet='Cotton production technology economic efficiency in China.')).tier, 'relevant')

    def test_cotton_sorbent_is_not_farming_research(self):
        intent = analyze_query(QUERY)
        self.assertEqual(judge(intent, GOOD).tier, 'relevant')
        self.assertEqual(judge(intent, PARTIAL).tier, 'partial')
        self.assertNotEqual(judge(intent, BAD).tier, 'relevant')

    def test_no_abstract_does_not_establish_missing_aspects(self):
        intent = analyze_query(QUERY)
        self.assertEqual(judge(intent, replace(PARTIAL, abstract_or_snippet=None)).tier, 'partial')

    def test_taxonomy_alone_cannot_claim_full_relevance(self):
        item = Material('f', None, 'Survey methods', metadata={'keywords': 'Cotton production technology economic efficiency'})
        self.assertEqual(judge(analyze_query(QUERY), item).tier, 'partial')

    def test_raw_ce_logits_are_not_tier_probabilities(self):
        items, partial, _ = classify(analyze_query(QUERY), [BAD, PARTIAL, GOOD],
            {BAD.id: 100, PARTIAL.id: 20, GOOD.id: -40})
        self.assertEqual(items, [GOOD])
        self.assertEqual(partial, [BAD, PARTIAL])  # uncalibrated logits cannot silently discard a candidate

    def test_doi_normalization_and_metadata_merge_preserve_source(self):
        a = replace(GOOD, doi='https://doi.org/10.1234/ABC', abstract_or_snippet=None)
        b = replace(GOOD, id='other', source='OpenAlex', doi='10.1234/abc', access_status='open')
        merged = merge_candidates([[a], [b]])
        self.assertEqual(len(merged), 1)
        self.assertEqual(merged[0].id, a.id)
        self.assertEqual(merged[0].doi, '10.1234/abc')
        self.assertEqual(merged[0].abstract_or_snippet, b.abstract_or_snippet)

    def test_identical_url_without_doi_is_not_shown_twice(self):
        a = replace(GOOD, url='https://example.org/paper?utm_source=test')
        b = replace(GOOD, id='other', url='https://example.org/paper')
        self.assertEqual(len(merge_candidates([[a], [b]])), 1)
        # Two short generic titles are insufficient grounds for deduplication.
        self.assertEqual(len(merge_candidates([[Material('a',None,'Water',year=2024)], [Material('b',None,'Water',year=2024)]])), 2)

    def test_unknown_qualifier_cannot_disappear_from_confident_results(self):
        intent = analyze_query(QUERY + ' in Uzbekistan')
        self.assertNotEqual(judge(intent, GOOD).tier, 'relevant')

    def test_correction_is_available_separately_even_with_high_logit(self):
        item = replace(GOOD, title='Correction: ' + GOOD.title)
        relevant, partial, _ = classify(analyze_query(QUERY), [item], {item.id:100},
            {'relevant_min':0.0,'partial_min':-1.0})
        self.assertEqual(relevant, [])
        self.assertEqual(partial, [item])

    def test_calibration_requires_matching_revision_and_multiple_labeled_queries(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / 'calibration.json'
            data = dict(schema=1,model='chosen',revision='r1',labeled_pairs=30,query_count=6,
                dataset_sha256='f'*64,partial_min='-2.0',relevant_min='0.5',
                preprocessing='title-abstract-head-tail-v1',max_length=384)
            path.write_text(json.dumps(data))
            self.assertIsNone(load_calibration(path,'chosen','r2'))
            self.assertEqual(load_calibration(path,'chosen','r1')['relevant_min'],0.5)
            data['query_count'] = 1
            path.write_text(json.dumps(data))
            self.assertIsNone(load_calibration(path,'chosen','r1'))


class PipelineTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.db = LabDatabase(Path(self.temp.name) / 'test.db')
        self.db.initialize()
        self.db.register(100)
        self.config = LabConfig(semantic_enabled=False, translation_enabled=False, reranker_enabled=False, enrich_dois=0)
        self.local = SearchService(CsvSource(ROOT / 'data/ploswater.csv'), self.config)
        self.service = ResearchService(self.local, self.db, self.config, Translator(self.db, False))

    async def asyncTearDown(self):
        await self.service.close()
        self.local.close()
        self.temp.cleanup()

    async def test_multiple_queries_increase_candidates_and_do_not_fill_with_junk(self):
        self.service.sources['crossref'].search = AsyncMock(side_effect=[[BAD, PARTIAL], [GOOD], [GOOD]])
        self.service.config = replace(self.config, ai_enabled=True)
        result = await self.service.search(ResearchFilters(topic=QUERY, sources=('crossref',),ai_enabled=True,ai_search=True),
            plan={'queries':['Cotton cultivation technology economic costs','Cotton farming profitability production methods']})
        self.assertEqual(result.items, [GOOD])
        self.assertEqual(result.partial_items, [BAD, PARTIAL])
        self.assertEqual(result.total_count, 1)
        calls = self.service.sources['crossref'].search.await_args_list
        self.assertEqual(len(calls), 3)
        self.assertGreater(calls[0].args[2], 20)
        self.assertEqual(result.diagnostics['candidates'], 3)

    async def test_deadline_retains_completed_source_and_cancels_slow_one(self):
        self.service.config = replace(self.config, search_timeout=0.03)
        cancelled = asyncio.Event()
        async def slow(*args, **kwargs):
            try:
                await asyncio.sleep(10)
            finally:
                cancelled.set()
        self.service.sources['openalex'].search = slow
        self.service.sources['crossref'].search = AsyncMock(return_value=[replace(GOOD, title='Water')])
        result = await self.service.search(ResearchFilters(topic='water', sources=('openalex','crossref')))
        self.assertEqual(len(result.items), 1)
        self.assertTrue(cancelled.is_set())
        self.assertTrue(any('срок' in warning for warning in result.warnings))

    async def test_strict_filters_survive_reranking_and_review(self):
        self.service.sources['crossref'].search = AsyncMock(return_value=[GOOD, replace(GOOD, id='old', year=2010)])
        result = await self.service.search(ResearchFilters(topic=QUERY, sources=('crossref',), year_from=2020))
        self.assertEqual(result.items, [GOOD])

    async def test_doi_never_calls_thematic_models_or_paid_reviewer(self):
        item = replace(BAD, doi='10.1234/exact')
        self.service.sources['crossref'].search = AsyncMock(return_value=[item])
        self.service.reranker.rank = AsyncMock(side_effect=AssertionError('Must stay exact'))
        reviewer = AsyncMock(side_effect=AssertionError('Must stay free'))
        result = await self.service.search(ResearchFilters(topic=item.doi, sources=('crossref',), ai_enabled=True, ai_search=True), reviewer=reviewer)
        self.assertEqual(result.items, [item])
        self.service.reranker.rank.assert_not_awaited()
        reviewer.assert_not_awaited()

    async def test_permission_without_explicit_paid_search_never_reviews(self):
        self.service.sources['crossref'].search = AsyncMock(return_value=[GOOD])
        self.service.config = replace(self.config, ai_enabled=True)
        reviewer = AsyncMock(side_effect=AssertionError('Hidden paid call'))
        for general, search in ((False,False),(True,False),(False,True)):
            await self.service.search(ResearchFilters(topic=QUERY, sources=('crossref',), ai_enabled=general, ai_search=search), reviewer=reviewer)
        reviewer.assert_not_awaited()

    async def test_enrichment_uses_selected_sources_and_only_matching_doi(self):
        self.service.config = replace(self.config, enrich_dois=1)
        short = replace(PARTIAL, doi='10.1234/cotton', title='Cotton production', abstract_or_snippet=None)
        full = replace(GOOD, doi=short.doi, source='OpenAlex', id='openalex:good')
        self.service.sources['crossref'].search = AsyncMock(return_value=[short])
        self.service.sources['openalex'].search = AsyncMock(side_effect=lambda q, doi, *a, **kw: [full] if doi else [])
        result = await self.service.search(ResearchFilters(topic=QUERY, sources=('crossref','openalex')))
        self.assertEqual(len(result.items), 1)
        self.assertEqual(result.items[0].abstract_or_snippet, full.abstract_or_snippet)
        self.assertEqual(result.items[0].source, 'Crossref')

    async def test_partial_snapshot_is_saved_without_inflating_primary_count(self):
        self.service.sources['crossref'].search = AsyncMock(return_value=[GOOD, PARTIAL, BAD])
        f = ResearchFilters(topic=QUERY, sources=('crossref',))
        result = await self.service.search(f)
        self.db.save_search(100, f, result)
        self.assertEqual(self.db.get_search(100, result.search_id)['total_count'], 1)
        extra = self.db.search_details(100, result.search_id)
        self.assertEqual({m['id'] for m in extra['partial_items']}, {PARTIAL.id, BAD.id})
        with self.assertRaises(LookupError): self.db.search_details(200, result.search_id)

    async def test_title_mode_retains_ambiguous_exact_titles_without_thematic_substitution(self):
        a=replace(GOOD,id='a',title='Technology of cotton production')
        b=replace(a,id='b',year=2020,authors=('Another Author',))
        self.service.sources['crossref'].search=AsyncMock(return_value=[a,b,BAD])
        self.service.reranker.rank=AsyncMock(side_effect=AssertionError('Title lookup stays bibliographic'))
        result=await self.service.search(ResearchFilters(topic=a.title,sources=('crossref',),query_mode='title'))
        self.assertEqual([m.id for m in result.items],['a','b'])
        self.service.reranker.rank.assert_not_awaited()

    async def test_finished_page_is_kept_when_next_page_fails(self):
        from external_sources import Crossref, SourceUnavailable
        from time import perf_counter
        http=SimpleNamespace(config=LabConfig(api_page_size=1),get=AsyncMock(side_effect=[
            {'message':{'items':[{'DOI':'10.1234/one','title':['Water']}],'next-cursor':'next'}},
            SourceUnavailable('next page timeout')]))
        warnings=[]
        f=ResearchFilters(topic='water',sources=('crossref',))
        batches=await self.service._fetch([('crossref',Crossref(http),'water',None,2,f)],perf_counter()+1,warnings)
        self.assertEqual(batches[0][0].doi,'10.1234/one')
        self.assertIn('Повторить поиск',warnings[0])

    async def test_versionless_arxiv_identifier_keeps_returned_version_and_skips_other_catalogs(self):
        item=Material('arxiv:2401.12345v2','preprint','Paper',metadata={'arxiv_id':'2401.12345v2'})
        self.service.sources['arxiv'].search=AsyncMock(return_value=[item])
        self.service.sources['crossref'].search=AsyncMock(side_effect=AssertionError('Wrong identifier route'))
        result=await self.service.search(ResearchFilters(topic='arXiv:2401.12345',sources=('arxiv','crossref')))
        self.assertEqual(result.items,[item])


class PairModelTests(unittest.IsolatedAsyncioTestCase):
    async def test_external_e5_batch_has_its_own_bounded_timeout(self):
        import time
        with tempfile.TemporaryDirectory() as directory:
            db=LabDatabase(Path(directory)/'test.db');db.initialize()
            class Engine:
                vectors=np.zeros((1,384),dtype=np.float32)
                device='cpu'
                def encode(self,texts):
                    time.sleep(.03)
                    values=np.zeros((len(texts),384),dtype=np.float32);values[:,0]=1
                    return values
            manager=SemanticSearch(None,LabConfig(semantic_timeout=.01,external_semantic_timeout=.2),db,engine=Engine())
            items,warning=await rerank(manager,db,QUERY,[GOOD,PARTIAL])
            self.assertFalse(warning)
            self.assertFalse(manager.circuit)
            self.assertEqual(items,[GOOD,PARTIAL])
    async def test_token_bounded_windows_include_evidence_at_abstract_end(self):
        seen=[]
        class Tokenizer:
            def encode(self,text,**kwargs):return text.split()
            def decode(self,tokens,**kwargs):return ' '.join(tokens)
        class Model:
            tokenizer=Tokenizer()
            def predict(self,pairs,**kwargs):
                seen.extend(pairs)
                return [3.0 if 'tail_evidence' in doc else -2.0 for _,doc in pairs]
        item=Material('long',None,'Title',abstract_or_snippet='background '*300+'tail_evidence')
        ranker=CrossEncoderRanker(LabConfig(reranker_max_length=64),model_factory=Model)
        _,scores,warning=await ranker.rank('required aspect',[item])
        self.assertFalse(warning)
        self.assertEqual(scores[item.id],3.0)
        self.assertEqual(len(seen),2)
        self.assertTrue(all(len(q.split())+len(d.split())<64 for q,d in seen))
    async def test_loader_uses_exact_multilingual_model_with_network_disabled(self):
        observed = {}
        class Model:
            def __init__(self, source, **kwargs): observed.update(source=source, **kwargs)
            def predict(self, pairs, **kwargs): return [0.1] * len(pairs)
        fake_torch = SimpleNamespace(set_num_threads=lambda n:None,
            cuda=SimpleNamespace(is_available=lambda:False), nn=SimpleNamespace(Identity=lambda:None))
        with tempfile.TemporaryDirectory() as directory, patch.dict('sys.modules', {
                'torch':fake_torch,'sentence_transformers':SimpleNamespace(CrossEncoder=Model)}):
            ranker = CrossEncoderRanker(LabConfig(reranker_model_dir=Path(directory)/'absent'))
            _, _, warning = await ranker.rank(QUERY,[GOOD])
        self.assertFalse(warning)
        self.assertEqual(observed['source'],'cross-encoder/mmarco-mMiniLMv2-L12-H384-v1')
        self.assertTrue(observed['local_files_only'])
        self.assertEqual(observed['device'],'cpu')
        self.assertFalse(observed['trust_remote_code'])

    async def test_pairs_are_original_query_and_title_abstract_not_keywords(self):
        observed = []
        class Model:
            def predict(self, pairs, **kwargs):
                observed.extend(pairs)
                self.assertion = kwargs['batch_size']
                return [-3.5, 9.0]
        config = LabConfig(reranker_candidates=2)
        ranker = CrossEncoderRanker(config, model_factory=Model)
        items = [replace(GOOD, metadata={'keywords':'SHOULD_NOT_REPLACE_CONTENT'}), PARTIAL, BAD]
        ordered, scores, warning = await ranker.rank(QUERY, items)
        self.assertFalse(warning)
        self.assertEqual(ordered[0].id, PARTIAL.id)
        self.assertEqual(scores[GOOD.id], -3.5)
        self.assertEqual(len(observed), 2)
        self.assertEqual(observed[0][0], QUERY)
        self.assertNotIn('SHOULD_NOT_REPLACE_CONTENT', observed[0][1])
        self.assertIn(GOOD.abstract_or_snippet, observed[0][1])
        self.assertIn('elapsed_seconds', ranker.metrics)

    async def test_unavailable_model_falls_back_without_repeated_load_or_download(self):
        calls = []
        def missing():
            calls.append(1)
            raise FileNotFoundError('No local weights')
        ranker = CrossEncoderRanker(LabConfig(), model_factory=missing)
        for _ in range(2):
            items, scores, warning = await ranker.rank(QUERY, [GOOD])
            self.assertEqual(items, [GOOD])
            self.assertFalse(scores)
            self.assertTrue(warning)
        self.assertEqual(len(calls), 1)

    async def test_timeout_does_not_block_event_loop_or_queue_native_workers(self):
        release, calls = threading.Event(), []
        class Blocking:
            def predict(self, pairs, **kwargs):
                calls.append(1)
                release.wait(2)
                return [0.0] * len(pairs)
        ranker = CrossEncoderRanker(LabConfig(reranker_timeout=0.03), model_factory=Blocking)
        try:
            task = asyncio.create_task(ranker.rank(QUERY, [GOOD]))
            await asyncio.sleep(0.01)
            self.assertFalse(task.done())
            result = await task
            self.assertTrue(result[2])
            await ranker.rank(QUERY, [GOOD])
            self.assertEqual(calls, [1])
        finally:
            release.set()

    async def test_external_e5_batches_and_reuses_vectors(self):
        with tempfile.TemporaryDirectory() as directory:
            db = LabDatabase(Path(directory) / 'test.db')
            db.initialize()
            calls = []
            class Engine:
                vectors = np.zeros((1,384),dtype=np.float32)
                device = 'cpu'
                def encode(self, texts):
                    calls.append(texts)
                    vectors = np.zeros((len(texts),384), dtype=np.float32)
                    vectors[:,0] = 1
                    return vectors
            manager = SemanticSearch(None, LabConfig(semantic_enabled=False,external_semantic_enabled=True), db, engine=Engine())
            items, warning = await rerank(manager, db, QUERY, [GOOD, PARTIAL, BAD])
            self.assertEqual(items, [GOOD, PARTIAL, BAD])
            self.assertFalse(warning)
            self.assertEqual([len(batch) for batch in calls], [3, 1])
            await rerank(manager, db, QUERY, [GOOD, PARTIAL, BAD])
            self.assertEqual([len(batch) for batch in calls], [3, 1, 1])


@unittest.skipUnless(os.getenv('RUN_CE_TESTS') == '1', 'Real CE weights require explicit local preparation')
class RealCrossEncoderTests(unittest.IsolatedAsyncioTestCase):
    async def test_real_multilingual_pairs_and_memory(self):
        ranker = CrossEncoderRanker(LabConfig(reranker_timeout=120))
        items, scores, warning = await ranker.rank('Технологии и экономическая эффективность производства хлопка', [BAD, GOOD])
        self.assertFalse(warning, warning)
        self.assertGreater(scores[GOOD.id], scores[BAD.id])
        self.assertEqual(items[0].id, GOOD.id)
        self.assertIn('elapsed_seconds', ranker.metrics)
