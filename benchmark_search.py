"""Offline hardware/relevance checks. Writes measurements only under ignored runtime/."""
from __future__ import annotations

import argparse
from dataclasses import replace
import json
from pathlib import Path
import sys
from time import perf_counter

from config import load_config
from search import SearchFilters, SearchService, search_text
from semantic_search import SemanticIndex, memory_usage
from sources import CsvSource

QUERIES = (
    "Водоснабжение и качество питьевой воды",
    "drinking water quality",
    "качество питьевой воды",
    "climate change",
    "drnking watre qualty",
    "zzzz_no_matching_material_zzzz",
    "quantum entanglement in superconducting qubits",
    "квантовая запутанность сверхпроводящих кубитов",
    "qualité de l’eau potable",
)


def main():
    sys.stdout.reconfigure(encoding="utf-8")
    parser = argparse.ArgumentParser()
    parser.add_argument("--fresh-index", action="store_true", help="Use a new runtime directory without deleting an existing index")
    args = parser.parse_args()
    config = load_config(False)
    if args.fresh_index:
        from uuid import uuid4
        config = replace(config, semantic_index_dir=config.semantic_index_dir / ("benchmark-"+uuid4().hex[:8]))
    source = CsvSource(config.csv_path)
    items = source.load()
    engine = SemanticIndex(source, config)
    engine.prepare()
    report = {"initial": dict(engine.metrics), "queries": []}
    # Reload vectors into another index object while reusing the already loaded model.
    reused = SemanticIndex(source, config)
    reused.model, reused.device, reused.revision, reused.torch = engine.model, engine.device, engine.revision, engine.torch
    reused.prepare()
    report["cached_index"] = dict(reused.metrics)
    service = SearchService(source, config)
    import numpy as np
    for query in QUERIES:
        started = perf_counter()
        baseline = [m for m in items if all(w in search_text(m).casefold() for w in query.casefold().split())]
        baseline_time = perf_counter()-started
        started = perf_counter()
        plain = service.search(SearchFilters(topic=query))
        lexical_time = perf_counter()-started
        started = perf_counter()
        ranked = engine.rank(query, set(engine.metadata["ids"]))
        hybrid = service.search(SearchFilters(topic=query), ranked)
        total_time = perf_counter()-started
        # Extra diagnostic encoding is excluded from measured hybrid time.
        scores = engine.vectors @ engine.encode(["query: "+query])[0]
        top = np.argsort(-scores)[:5]
        row = {"query": query, "old_count": len(baseline), "old_seconds": baseline_time,
               "lexical_count": plain.total_count, "lexical_seconds": lexical_time,
               "semantic_seconds": engine.metrics["query_seconds"], "hybrid_seconds": total_time,
               "hybrid_count": hybrid.total_count,
               "score_distribution": {"median": float(np.median(scores)), "mean": float(np.mean(scores)),
                    "std": float(np.std(scores)), "max_minus_median": float(np.max(scores)-np.median(scores))},
               "results": [{"id": m.id, "title": m.title, "doi": m.doi} for m in hybrid.items[:20]],
               "raw_semantic_top": [{"title": items[int(i)].title, "cosine": float(scores[i])} for i in top]}
        report["queries"].append(row)
        print(json.dumps(row, ensure_ascii=False, indent=2))
    report["final_memory"] = memory_usage(engine.torch)
    service.close()
    output = Path("runtime/benchmark/results.json")
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps({"initial": report["initial"], "cached_index": report["cached_index"], "memory": report["final_memory"]}, indent=2))
    print("Measurements:", output)


if __name__ == "__main__":
    main()
