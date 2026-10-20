"""Batched E5 for the unified candidate pool, using its existing single worker."""
import asyncio
import hashlib
import logging
from time import perf_counter
from relevance import material_text

logger = logging.getLogger(__name__)


async def _wait(manager, work, external=False):
    if manager.circuit or not (manager.external_enabled if external else manager.enabled):
        return None, 'E5 отключена или сработала защита; используется текстовый отбор.'
    if manager.engine.vectors is None:
        manager.start()
        return None, 'E5 ещё готовится; используется текстовый отбор.'
    generation=manager._generation
    operation='external' if external else 'query'
    timeout=getattr(manager.config,'external_semantic_timeout',manager.config.semantic_timeout) if external else manager.config.semantic_timeout
    future = manager._submit(operation, work)
    if future is None:
        return None, 'E5 занята; используется текстовый отбор.'
    started = perf_counter()
    try:
        while not future.done():
            if generation!=manager._generation or not (manager.external_enabled if external else manager.enabled):
                return None,'Настройка E5 изменилась; сохранён предварительный отбор.'
            if perf_counter()-started >= timeout:
                return None, 'E5 превысила время обработки; используется текстовый отбор.'
            await asyncio.sleep(0.02)
        if generation!=manager._generation or not (manager.external_enabled if external else manager.enabled):
            return None,'Настройка E5 изменилась; сохранён предварительный отбор.'
        return future.result(), ''
    except Exception:
        return None, 'E5 временно недоступна; используется текстовый отбор.'


async def local_candidates(manager, topic, allowed, limit):
    """Retrieval is broader than old CSV thresholds; relevance is checked later."""
    def work():
        import numpy as np
        query = manager.engine.encode(['query: ' + topic])[0]
        similarities = manager.engine.vectors @ query
        return [manager.engine.metadata['ids'][int(i)] for i in np.argsort(-similarities)
            if manager.engine.metadata['ids'][int(i)] in allowed][:limit]
    return await _wait(manager, work)


async def rerank(manager, db, topic, items):
    if not manager.external_enabled or not items:
        return items, ''
    def work():
        import numpy as np
        selected = items[:getattr(manager.config, 'search_candidates', 180)]
        vectors, missing, texts, keys = {}, [], [], {}
        for item in selected:
            text = material_text(item)[:5000]
            key = 'external-e5:v2:' + hashlib.sha256((manager.config.semantic_model +
                manager.config.semantic_revision + text).encode()).hexdigest()
            keys[item.id] = key
            cached = db.cache_get(key)
            if (isinstance(cached, list) and len(cached) == 384
                    and np.isfinite(np.asarray(cached, dtype=np.float32)).all()):
                vectors[item.id] = cached
            else:
                missing.append(item.id)
                texts.append('passage: ' + text)
        if texts:
            encoded = manager.engine.encode(texts)
            for mid, vector in zip(missing, encoded):
                vectors[mid] = vector.tolist()
                db.cache_put(keys[mid], vectors[mid], 30 * 86400)
        query = manager.engine.encode(['query: ' + topic])[0]
        matrix = np.asarray([vectors[item.id] for item in selected], dtype=np.float32)
        scores = matrix @ query
        return [selected[int(i)] for i in np.argsort(-scores)] + items[len(selected):]
    result, warning = await _wait(manager, work, external=True)
    return (result if result is not None else items), warning
