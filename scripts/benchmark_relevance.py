"""Replay actual captured metadata against main or the new pipeline, without fees."""
import argparse
import asyncio
from dataclasses import replace
from datetime import datetime, timezone
import json
from pathlib import Path
import sys
import tempfile
from time import perf_counter

PROJECT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT))
from evaluation import ranking_metrics


async def run(args):
    # Import project modules only after selecting the actual implementation.
    sys.path.insert(0, str(args.project_root.resolve()))
    from lab_config import LabConfig
    from lab_storage import LabDatabase
    from research import ResearchFilters, ResearchService
    from search import Material, SearchService, normalize_doi
    from semantic_search import SemanticSearch, memory_usage
    from translation import Translator

    fixtures = json.loads((PROJECT / 'benchmarks/metadata.json').read_text(encoding='utf-8'))
    cases = json.loads((PROJECT / 'benchmarks/queries.json').read_text(encoding='utf-8'))['queries']
    judgments = json.loads((PROJECT / 'benchmarks/judgments.json').read_text(encoding='utf-8'))['judgments']
    catalog = {mid: Material.from_dict(v) for mid, v in fixtures['materials'].items()}
    class Snapshot:
        supports_types = False
        path = args.project_root.resolve() / 'data/ploswater.csv'
        def load(self):
            return [m for m in catalog.values() if m.metadata.get('paperid')]
    def key(item):
        return 'doi:' + normalize_doi(item.doi) if normalize_doi(item.doi) else item.id
    config_args = dict(semantic_enabled=args.models, translation_enabled=False, external_semantic_enabled=args.models)
    if 'reranker_enabled' in LabConfig.__dataclass_fields__:
        config_args.update(reranker_enabled=args.models, enrich_dois=0)
        config_args['reranker_query_language']=args.rank_language
        if args.calibration:
            config_args['relevance_calibration']=args.calibration.resolve()
    config = LabConfig(**config_args)
    results = []
    with tempfile.TemporaryDirectory() as directory:
        # The replay corpus is a subset of the CSV: its index must never replace
        # the owner's production index, even though the CSV path is the same.
        config=replace(config,semantic_index_dir=Path(directory)/'semantic')
        db = LabDatabase(Path(directory) / 'benchmark.db')
        db.initialize()
        local = SearchService(Snapshot(), config)
        if args.models:
            local.semantic = SemanticSearch(local.source, config, db)
            await asyncio.to_thread(local.semantic.engine.prepare)
        translator = Translator(db, False)
        service = ResearchService(local, db, config, translator)
        calls = []
        class Replay:
            def __init__(self, name): self.name = name
            async def search(self, query, doi=None, limit=20, **kwargs):
                calls.append((self.name, query, doi))
                if doi:
                    return [m for m in catalog.values() if normalize_doi(m.doi) == doi][:limit]
                ids = fixtures['responses'].get(self.name + ':' + query.casefold(), [])
                return [catalog[mid] for mid in ids[:limit]]
        service.sources = {name: Replay(name) for name in service.sources}
        try:
            for case in cases:
                async def translated(text, target=case['translation']): return target, ''
                translator.query = translated
                count_before = len(calls)
                f = ResearchFilters(topic=case['query'], sources=tuple(case['sources']), max_results=10,
                    material_types=tuple(case.get('material_types', ())))
                started = perf_counter()
                result = await service.search(f)
                elapsed = perf_counter() - started
                grades = judgments.get(case['id'], {})
                ranking = [key(item) for item in result.items]
                partial = [key(item) for item in getattr(result, 'partial_items', [])]
                preliminary=[key(Material.from_dict(m)) for m in getattr(result,'diagnostics',{}).get('preliminary_items',[])]
                results.append(dict(id=case['id'], query=case['query'], split=case['split'],
                    metrics=ranking_metrics(ranking, grades), combined_metrics=ranking_metrics(ranking + partial, grades),
                    preliminary_metrics=ranking_metrics(preliminary,grades),
                    ranking=ranking, partial=partial, elapsed_seconds=elapsed,
                    adapter_calls=len(calls) - count_before, warnings=result.warnings,
                    diagnostics=getattr(result,'diagnostics',{})))
        finally:
            await service.close()
            local.close()
    payload = dict(schema=1, generated_at=datetime.now(timezone.utc).isoformat(),
        project_root=str(args.project_root.resolve()), models_requested=args.models,
        scope='Offline replay: <=10 captured hits/formulation, curated subset of PLOS Water, fixed translations, no live HTTP/paid calls.',
        memory=memory_usage(), results=results)
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding='utf-8')
    print(json.dumps({'cases':len(results),'output':str(args.output),'models':args.models,'memory':payload['memory']}, ensure_ascii=False))


if __name__ == '__main__':
    parser = argparse.ArgumentParser()
    parser.add_argument('--project-root', type=Path, default=PROJECT)
    parser.add_argument('--output', type=Path, default=PROJECT / 'runtime/relevance-benchmark.json')
    parser.add_argument('--models', action='store_true', help='Use existing local weights; never download')
    parser.add_argument('--calibration',type=Path,help='Evaluate a candidate calibration without installing it')
    parser.add_argument('--rank-language',choices=('original','translated'),default='original')
    raise SystemExit(asyncio.run(run(parser.parse_args())))
