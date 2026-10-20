"""Fit CE cutoffs on grouped labeled pairs, with a separate evaluation split."""
import argparse
import asyncio
import hashlib
import json
from pathlib import Path
import sys

PROJECT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT))
from evaluation import fit_threshold
from lab_config import load_lab_config
from reranker import CrossEncoderRanker, MODEL, PREPROCESSING
from search import Material, normalize_doi
from query_planner import analyze_query


async def main(args):
    config = load_lab_config(require_token=False)
    metadata_bytes = (PROJECT / 'benchmarks/metadata.json').read_bytes()
    judgments_bytes = (PROJECT / 'benchmarks/judgments.json').read_bytes()
    cases = json.loads((PROJECT / 'benchmarks/queries.json').read_text(encoding='utf-8'))['queries']
    records = json.loads(metadata_bytes)['materials']
    labels = json.loads(judgments_bytes)['judgments']
    by_key = {}
    for mid, record in records.items():
        item = Material.from_dict(record)
        key = 'doi:' + normalize_doi(item.doi) if normalize_doi(item.doi) else mid
        by_key[key] = item
    train_families = {c['family'] for c in cases if c['split'] == 'calibration'}
    evaluation_families = {c['family'] for c in cases if c['split'] == 'evaluation'}
    if train_families & evaluation_families:
        raise ValueError('Query families must not leak between calibration/evaluation.')
    ranker = CrossEncoderRanker(config)
    scores = []
    for case in cases:
        # Calibrate the retrieved shortlist, not the whole fixture catalog.
        pool = []
        intent = analyze_query(case['query'], case['translation'], max_variants=config.query_variants)
        for query in intent.variants:
            for source in case['sources']:
                for mid in json.loads(metadata_bytes)['responses'].get(source + ':' + query.casefold(), []):
                    record = records[mid]
                    key = 'doi:' + normalize_doi(record.get('doi')) if normalize_doi(record.get('doi')) else mid
                    if key not in pool: pool.append(key)
        graded = [(mid, labels[case['id']][mid]) for mid in pool[:config.reranker_candidates]
            if mid in by_key and mid in labels.get(case['id'],{})]
        if not graded or normalize_doi(case['query']):
            continue
        # score bounded batches directly in this offline CLI, outside Telegram.
        pairs = [by_key[mid] for mid, _ in graded]
        batch_scores = {}
        for offset in range(0, len(pairs), config.reranker_candidates):
            batch_scores.update(await asyncio.to_thread(ranker._work,
                case['translation'] if args.query_language=='translated' else case['query'],
                pairs[offset:offset + config.reranker_candidates]))
        for mid, grade in graded:
            scores.append({'query_id':case['id'],'split':case['split'],'material_key':mid,
                'grade':grade,'score':batch_scores[by_key[mid].id]})
    train = [r for r in scores if r['split'] == 'calibration']
    if len(train) < 20 or len({r['query_id'] for r in train}) < 5:
        raise ValueError('Need >=20 labeled pairs across >=5 calibration queries.')
    high = fit_threshold([r['score'] for r in train], [r['grade'] == 2 for r in train])
    low = min(high, fit_threshold([r['score'] for r in train], [r['grade'] >= 1 for r in train]))
    evaluation = [r for r in scores if r['split'] == 'evaluation']
    correct = sum((2 if r['score'] >= high else 1 if r['score'] >= low else 0) == r['grade'] for r in evaluation)
    payload = dict(schema=1, model=MODEL, revision=config.reranker_revision,
        preprocessing=PREPROCESSING,max_length=config.reranker_max_length,
        query_language=args.query_language,
        relevant_min=high, partial_min=low, labeled_pairs=len(train), query_count=len({r['query_id'] for r in train}),
        dataset_sha256=hashlib.sha256(metadata_bytes + judgments_bytes).hexdigest(),
        objective='F0.5, grouped query families, metadata-only labels', evaluation_pairs=len(evaluation),
        evaluation_accuracy=correct / len(evaluation) if evaluation else None, model_metrics=ranker.metrics)
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding='utf-8')
    args.output.with_suffix('.scores.json').write_text(json.dumps(scores, ensure_ascii=False, indent=2), encoding='utf-8')
    print(json.dumps(payload, ensure_ascii=False, indent=2))


if __name__ == '__main__':
    parser = argparse.ArgumentParser()
    parser.add_argument('--output', type=Path, default=PROJECT / 'runtime/relevance-calibration.json')
    parser.add_argument('--query-language',choices=('original','translated'),default='original')
    args = parser.parse_args()
    try:
        asyncio.run(main(args))
    except (ImportError, OSError) as exc:
        print('No local model available; no download was attempted. ' + type(exc).__name__, file=sys.stderr)
        raise SystemExit(2)
