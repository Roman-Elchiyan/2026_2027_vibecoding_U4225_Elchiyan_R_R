"""Version-family identity and complete paid metadata requests, without paid calls."""
import itertools
import json
from dataclasses import replace
from pathlib import Path
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock

import httpx

from config import ROOT
from external_sources import DataCite, MetadataHTTP, Zenodo
from lab_config import LabConfig
from lab_storage import LabDatabase
from litellm_admin import BudgetUnavailable
from relevance import merge_candidates
from research import ResearchFilters, ResearchService
from research_ai import FULL_METADATA_INPUT_LIMIT, ResearchAI
from search import Material, SearchService
from sources import CsvSource
from translation import Translator


CONCEPT = '10.5281/zenodo.11442546'
OLD = '10.5281/zenodo.11442547'
NEW = '10.5281/zenodo.11442728'
TITLE = 'EXPLORING THE ENVIRONMENTAL IMPACT OF COTTON PRODUCTION AND THE ROLE OF ETHICAL FINANCE'


def record(doi, relations, created, *, year=2024, abstract='Cotton production.'):
    return DataCite.normalize({'id': doi, 'attributes': {
        'doi': doi, 'titles': [{'title': TITLE}], 'creators': [{'name': 'Author'}],
        'publicationYear': year, 'created': created, 'language': 'en',
        'types': {'resourceTypeGeneral': 'Text'}, 'url': 'https://zenodo.org/doi/' + doi,
        'descriptions': [{'description': abstract}],
        'rightsList': [{'rightsIdentifier': 'cc-by-4.0'}],
        'relatedIdentifiers': [{'relatedIdentifier': target, 'relatedIdentifierType': 'DOI',
                                'relationType': kind} for kind, target in relations]}})


def family():
    # The IDs, timestamps and version relations reproduce the reported DataCite records.
    return [
        record(CONCEPT, [('HasVersion', OLD), ('HasVersion', NEW)], '2024-06-03T09:26:44.000Z'),
        record(OLD, [('IsVersionOf', CONCEPT)], '2024-06-03T09:26:44.000Z', abstract='Older abstract. ' * 100),
        record(NEW, [('IsVersionOf', CONCEPT)], '2024-06-03T09:30:34.000Z', abstract='Current cotton production abstract.'),
    ]


class ZenodoIdentityTests(unittest.TestCase):
    def test_reported_concept_and_versions_form_one_work_in_any_order(self):
        for items in itertools.permutations(family()):
            merged = merge_candidates([items])
            self.assertEqual(len(merged), 1)
            self.assertEqual(merged[0].doi, NEW)
            self.assertEqual(merged[0].abstract_or_snippet, 'Current cotton production abstract.')
            self.assertEqual(set(merged[0].metadata['version_dois']), {CONCEPT, OLD, NEW})
            self.assertEqual({v['doi'] for v in merged[0].metadata['versions']}, {CONCEPT, OLD, NEW})

    def test_versions_share_identity_without_concept_record_and_across_providers(self):
        _, old, new = family()
        zenodo = Zenodo.normalize({'id': 11442728, 'doi': NEW, 'conceptdoi': CONCEPT,
            'created': '2024-06-03T09:30:34.000Z', 'metadata': {
                'title': TITLE, 'description': 'Current cotton production abstract.',
                'publication_date': '2024-06-03', 'access_right': 'open'}})
        merged = merge_candidates([[old], [new], [zenodo]])
        self.assertEqual(len(merged), 1)
        self.assertEqual(merged[0].doi, NEW)
        self.assertEqual({p['source'] for p in merged[0].metadata['provenance']}, {'DataCite', 'Zenodo'})

    def test_identical_title_and_abstract_do_not_join_unrelated_dois(self):
        a = record(OLD, [], '', abstract='Same abstract.')
        b = record(NEW, [('IsSupplementTo', OLD)], '', abstract='Same abstract.')
        self.assertEqual(len(merge_candidates([[a, b]])), 2)

    def test_version_family_cannot_bridge_an_unrelated_doi(self):
        _, old, new = family()
        old = replace(old, metadata={**old.metadata, 'pmid': '123'})
        other = Material('unrelated', 'article', TITLE, doi='10.1234/different', metadata={'pmid': '123'})
        self.assertEqual(len(merge_candidates([[old, new, other]])), 2)

    def test_incremental_merge_keeps_all_version_dois(self):
        concept, old, new = family()
        first = merge_candidates([[concept, old]])
        merged = merge_candidates([first, [new]])
        self.assertEqual(len(merged), 1)
        self.assertEqual(merged[0].doi, NEW)
        self.assertEqual(set(merged[0].metadata['version_dois']), {CONCEPT, OLD, NEW})


class ZenodoPipelineTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.db = LabDatabase(Path(self.temp.name) / 'test.db')
        self.db.initialize()
        self.db.register(100)
        config = LabConfig(semantic_enabled=False, translation_enabled=False,
                           reranker_enabled=False, enrich_dois=0)
        self.local = SearchService(CsvSource(ROOT / 'data/ploswater.csv'), config)
        def unexpected(request):
            raise AssertionError('Unexpected network request: ' + request.url.host)
        http = MetadataHTTP(self.db, config, httpx.MockTransport(unexpected))
        self.service = ResearchService(self.local, self.db, config, Translator(self.db, False), http=http)

    async def asyncTearDown(self):
        await self.service.close()
        self.local.close()
        self.temp.cleanup()

    async def test_distinct_works_fill_page_and_selected_version_survives_favorites(self):
        others = [record(f'10.1234/cotton{i}', [], '') for i in range(4)]
        self.service.sources['datacite'].search = AsyncMock(return_value=family() + others)
        filters = ResearchFilters(topic='cotton', sources=('datacite',), max_results=5)
        result = await self.service.search(filters)
        self.assertEqual(len(result.items), 5)
        selected = next(i for i, m in enumerate(result.items) if m.doi == NEW)
        self.db.save_search(100, filters, result)
        saved = self.db.set_favorite(100, result.search_id, selected, True)
        self.assertEqual(saved.doi, NEW)
        reopened = LabDatabase(self.db.path)
        reopened.initialize()
        favorite_search = reopened.saved_search(100)
        item = reopened.result_item(100, favorite_search, 0)
        self.assertEqual(item.doi, NEW)
        self.assertEqual(set(item.metadata['version_dois']), {CONCEPT, OLD, NEW})

    async def test_exact_doi_returns_requested_older_version(self):
        self.service.sources['datacite'].search = AsyncMock(return_value=family())
        result = await self.service.search(ResearchFilters(topic=OLD, sources=('datacite',)))
        self.assertEqual([m.doi for m in result.items], [OLD])

    async def test_year_filter_is_applied_before_choosing_version(self):
        concept, old, new = family()
        new = replace(new, year=2025)
        self.service.sources['datacite'].search = AsyncMock(return_value=[concept, old, new])
        result = await self.service.search(ResearchFilters(topic='cotton', sources=('datacite',), year_to=2024))
        self.assertEqual([m.doi for m in result.items], [OLD])


class FullAbstractTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.db = LabDatabase(Path(self.temp.name) / 'test.db')
        self.db.initialize()
        self.db.register(100)
        self.management = SimpleNamespace(
            get_limits=AsyncMock(return_value={'remaining': .5}),
            ensure=AsyncMock(return_value=('telegram-100', 'synthetic-virtual-key')))
        self.requests = []
        self.foreign_id = False

        def respond(request):
            body = json.loads(request.content)
            self.requests.append(body)
            prompt = body['input']
            if '\n[' in prompt:
                cards = json.loads(prompt.rsplit('\n', 1)[1])
                rows = [{'id': card['id'], 'tier': 'relevant', 'evidence': card['title'][:50]} for card in cards]
                if self.foreign_id: rows[0]['id'] = '999'
                text = json.dumps(rows)
            else:
                text = 'Explanation.'
            return httpx.Response(200, headers={'x-litellm-response-cost': '0.001'},
                json={'id': 'synthetic-response', 'output': [{'content': [{'type': 'output_text', 'text': text}]}]})

        self.ai = ResearchAI(self.db, LabConfig(ai_enabled=True), self.management, httpx.MockTransport(respond))

    async def asyncTearDown(self):
        await self.ai.close()
        self.temp.cleanup()

    def cards(self):
        return [card for request in self.requests for card in json.loads(request['input'].rsplit('\n', 1)[1])]

    async def test_full_title_and_abstract_beyond_6000_characters_reach_responses(self):
        abstract = 'Полная аннотация с кавычками " и переносом\n' * 200 + 'END_OF_ABSTRACT'
        item = Material('one', 'article', 'Cotton production ' * 30, abstract_or_snippet=abstract)
        result = await self.ai.assess(100, 'cotton', [item], 'full')
        self.assertIn(item.id, result)
        self.assertEqual(self.cards()[0]['abstract'], abstract)
        self.assertEqual(self.cards()[0]['title'], item.title)
        self.assertGreater(len(self.requests[0]['input']), 6000)
        await self.ai.assess(100, 'cotton', [item], 'full')
        self.assertEqual(len(self.requests), 1)

    async def test_batches_keep_whole_abstracts_and_stable_material_ids(self):
        items = [Material(str(i), 'article', f'Cotton {i}', abstract_or_snippet='x' * 24000 + f'END_{i}') for i in range(3)]
        result = await self.ai.assess(100, 'cotton', items, 'batch')
        self.assertEqual(set(result), {m.id for m in items})
        self.assertEqual(len(self.requests), 2)
        self.assertEqual([c['abstract'] for c in self.cards()], [m.abstract_or_snippet for m in items])
        self.assertTrue(all(len(r['input']) <= FULL_METADATA_INPUT_LIMIT for r in self.requests))

    async def test_oversized_single_abstract_is_rejected_before_any_paid_request(self):
        items = [Material('short', 'article', 'Cotton'),
                 Material('large', 'article', 'Cotton', abstract_or_snippet='x' * FULL_METADATA_INPUT_LIMIT)]
        with self.assertRaisesRegex(BudgetUnavailable, 'Аннотация'):
            await self.ai.assess(100, 'cotton', items, 'oversized')
        self.assertEqual(self.requests, [])
        self.management.get_limits.assert_not_awaited()

    async def test_foreign_material_id_is_rejected(self):
        self.foreign_id = True
        with self.assertRaises(BudgetUnavailable):
            await self.ai.assess(100, 'cotton', [Material('one', 'article', 'Cotton')], 'foreign')

    async def test_explanation_receives_full_abstract(self):
        abstract = 'Cotton production. ' * 500 + 'COMPLETE_END'
        item = Material('one', 'article', 'Cotton', abstract_or_snippet=abstract)
        await self.ai.explain(100, 'cotton', item, 'explain')
        self.assertTrue(self.requests[0]['input'].endswith(abstract))

    async def test_generic_input_is_never_silently_truncated(self):
        with self.assertRaisesRegex(BudgetUnavailable, 'не обрезаны'):
            await self.ai.generate(100, 'x' * 6001, 'limit')
        self.assertEqual(self.requests, [])


if __name__ == '__main__':
    unittest.main()
