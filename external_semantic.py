"""Optional E5 reranking using the existing single GPU worker and SQLite cache."""
import asyncio
import hashlib
import logging
from time import perf_counter

logger = logging.getLogger(__name__)


async def rerank(manager,db,topic,items):
    if not manager.external_enabled or not items:
        return items,""
    if manager.engine.vectors is None or manager.circuit:
        return items,"E5 внешних источников ещё не готова; сохранён порядок API."

    def work():
        import numpy as np
        selected = items[:100]
        vectors = []
        for item in selected:
            text = item.title+" "+(item.abstract_or_snippet or "")[:1200]
            key = "external-e5:"+hashlib.sha256((manager.config.semantic_model+manager.config.semantic_revision+text).encode()).hexdigest()
            cached = db.cache_get(key)
            if cached is None:
                cached = manager.engine.encode(["passage: "+text])[0].tolist()
                db.cache_put(key,cached,30*86400)
            vectors.append(cached)
        query = manager.engine.encode(["query: "+topic])[0]
        scores = np.asarray(vectors,dtype=np.float32) @ query
        return [selected[int(i)] for i in np.argsort(-scores)] + items[100:]

    future = manager._submit("query",work)
    if future is None:
        return items,"E5 занята; сохранён порядок внешних API."
    started = perf_counter()
    try:
        while not future.done():
            if perf_counter()-started >= manager.config.semantic_timeout:
                return items,"E5 внешних API превысила время обработки; сохранён порядок API."
            await asyncio.sleep(0.02)
        result = future.result()
        logger.info("event=external_e5_ranked count=%d elapsed_ms=%.1f",len(result),(perf_counter()-started)*1000)
        return result,""
    except Exception:
        return items,"E5 внешних API временно недоступна; сохранён порядок API."
