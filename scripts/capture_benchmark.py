"""Capture bounded public metadata GETs; no models, Telegram or paid API calls."""
import argparse
import asyncio
from datetime import datetime, timezone
import json
from pathlib import Path
import sys
import tempfile

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from config import ROOT
from external_sources import MetadataHTTP, adapters
from lab_config import LabConfig
from lab_storage import LabDatabase
from query_planner import analyze_query
from search import SearchFilters, SearchService, normalize_doi
from sources import CsvSource


async def capture(output, retry_errors=False):
    queries = json.loads((ROOT / 'benchmarks/queries.json').read_text(encoding='utf-8'))['queries']
    config = LabConfig(semantic_enabled=False, translation_enabled=False, reranker_enabled=False, api_timeout=8.0)
    previous = json.loads(output.read_text(encoding='utf-8')) if retry_errors and output.is_file() else {}
    materials, responses, errors = previous.get('materials', {}), previous.get('responses', {}), previous.get('errors', [])
    failed_keys = {e['source'] + ':' + e['query'].casefold() for e in errors}
    source = CsvSource(config.csv_path)
    local = SearchService(source, config)
    with tempfile.TemporaryDirectory() as directory:
        db = LabDatabase(Path(directory) / 'metadata.db')
        db.initialize()
        http = MetadataHTTP(db, config)
        providers = adapters(http)
        try:
            for case in queries:
                doi = normalize_doi(case['query'])
                intent = analyze_query(case['query'], case['translation'], max_variants=config.query_variants)
                variants = (case['translation'],) if doi else intent.variants
                for provider in case['sources']:
                    for query in variants:
                        key = provider + ':' + query.casefold()
                        if key in responses and not (retry_errors and key in failed_keys):
                            continue
                        try:
                            if provider == 'local':
                                batch = local.search(SearchFilters(topic=query)).items[:10]
                            else:
                                batch = await providers[provider].search(query, doi, 10)
                            ids = []
                            for item in batch:
                                record = item.to_dict()
                                record['abstract_or_snippet'] = (item.abstract_or_snippet or '')[:1200] or None
                                materials[item.id] = record
                                ids.append(item.id)
                            responses[key] = ids
                            errors = [e for e in errors if e['source'] + ':' + e['query'].casefold() != key]
                            print(provider, case['id'], 'records', len(ids), flush=True)
                        except Exception as exc:
                            errors.append({'source': provider, 'query': query, 'error': type(exc).__name__})
                            responses[key] = []
                            print(provider, case['id'], type(exc).__name__, flush=True)
                payload = {'schema':1, 'captured_at':datetime.now(timezone.utc).isoformat(),
                    'description':'Actual public metadata, <=10 hits per captured formulation; truncated abstracts. No weights or paid calls.',
                    'materials':materials, 'responses':responses, 'errors':errors}
                output.parent.mkdir(parents=True, exist_ok=True)
                output.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding='utf-8')
        finally:
            await http.close()
            local.close()
    return 0


if __name__ == '__main__':
    parser = argparse.ArgumentParser()
    parser.add_argument('--output', type=Path, default=ROOT / 'benchmarks/metadata.json')
    parser.add_argument('--retry-errors', action='store_true')
    args = parser.parse_args()
    raise SystemExit(asyncio.run(capture(args.output, args.retry_errors)))
