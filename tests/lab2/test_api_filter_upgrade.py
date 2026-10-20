import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock

from external_sources import OpenAlex, Crossref, DataCite
from research import ResearchFilters


class ProviderFiltersTests(unittest.IsolatedAsyncioTestCase):
    async def test_openalex_pushes_year_language_and_open_filters(self):
        http = SimpleNamespace(config=SimpleNamespace(openalex_key='test-only'), get=AsyncMock(return_value={'results':[]}))
        f = ResearchFilters(topic='water', year_from=2020, year_to=2025, language='en', access_status='open')
        await OpenAlex(http).search(f.topic, filters=f)
        params = http.get.call_args.args[2]
        self.assertIn('from_publication_date:2020-01-01', params['filter'])
        self.assertIn('to_publication_date:2025-12-31', params['filter'])
        self.assertIn('language:en', params['filter'])
        self.assertIn('is_oa:true', params['filter'])

    async def test_crossref_uses_publication_dates_and_preserves_doi_lookup(self):
        http = SimpleNamespace(config=SimpleNamespace(contact_email=''), get=AsyncMock(return_value={'message':{'items':[]}}))
        f = ResearchFilters(topic='water', year_from=2020, year_to=2025)
        await Crossref(http).search(f.topic, filters=f)
        self.assertEqual(http.get.call_args.args[2]['filter'], 'from-pub-date:2020-01-01,until-pub-date:2025-12-31')
        http.get.return_value = {'message':{}}
        await Crossref(http).search('10.1234/a', '10.1234/a', filters=f)
        self.assertNotIn('filter', http.get.call_args.args[2])

    async def test_datacite_explicit_relevance_sort_and_dataset_filter(self):
        http = SimpleNamespace(get=AsyncMock(return_value={'data':[]}))
        f = ResearchFilters(topic='groundwater quality', material_types=('Dataset',), year_from=2020)
        await DataCite(http).search(f.topic, filters=f)
        params = http.get.call_args.args[2]
        self.assertEqual(params['sort'], 'relevance')
        self.assertEqual(params['resource-type-id'], 'dataset')
        self.assertIn('publicationYear:[2020 TO *]', params['query'])
