"""Local E5 index and one bounded, daemon inference worker. No model downloads."""

from __future__ import annotations

import asyncio
from concurrent.futures import Future
import hashlib
import json
import logging
import os
import re
from pathlib import Path
import tempfile
import threading
from zipfile import BadZipFile
from time import perf_counter

from search import deduplicate

logger = logging.getLogger(__name__)
INDEX_SCHEMA = 2
DIMENSION = 384
FALLBACK = "Межъязыковой поиск ограничен; используются FTS5 и RapidFuzz."


def document_text(item) -> str:
    # PLOS keywords are taxonomy paths. Repeating each parent category dilutes the title's meaning.
    # Keep every distinct terminal topic; the original paths remain untouched in SQLite/CSV and FTS.
    keywords = str(item.metadata.get("keywords") or "")
    if keywords.strip().startswith("/"):
        keywords = "; ".join(dict.fromkeys(part.strip().rstrip("/").rsplit("/", 1)[-1]
                                         for part in keywords.split(";") if part.strip()))
    return item.title + (". Keywords: " + keywords if keywords else "")


def memory_usage(torch_module=None) -> dict:
    """Current process RSS on Windows; peak RSS on Unix, plus PyTorch VRAM if available."""
    result = {}
    try:
        if os.name == "nt":
            import ctypes
            from ctypes import wintypes
            class Counters(ctypes.Structure):
                _fields_ = [("cb", wintypes.DWORD), ("faults", wintypes.DWORD)] + [
                    (name, ctypes.c_size_t) for name in
                    ("peak_rss", "rss", "peak_paged", "paged", "peak_nonpaged", "nonpaged", "pagefile", "peak_pagefile", "private")]
            counters = Counters()
            counters.cb = ctypes.sizeof(counters)
            kernel = ctypes.WinDLL("kernel32")
            kernel.GetCurrentProcess.restype = wintypes.HANDLE
            psapi = ctypes.WinDLL("psapi")
            psapi.GetProcessMemoryInfo.argtypes = [wintypes.HANDLE, ctypes.POINTER(Counters), wintypes.DWORD]
            if psapi.GetProcessMemoryInfo(kernel.GetCurrentProcess(), ctypes.byref(counters), counters.cb):
                result.update(rss_mb=round(counters.rss / 2**20, 2), peak_rss_mb=round(counters.peak_rss / 2**20, 2))
        else:
            import resource
            result["peak_rss_mb"] = round(resource.getrusage(resource.RUSAGE_SELF).ru_maxrss / 1024, 2)
        if torch_module is not None and torch_module.cuda.is_available():
            result.update(vram_allocated_mb=round(torch_module.cuda.memory_allocated()/2**20, 2),
                          vram_peak_mb=round(torch_module.cuda.max_memory_allocated()/2**20, 2),
                          vram_reserved_mb=round(torch_module.cuda.memory_reserved()/2**20, 2))
    except Exception as exc:
        logger.warning("event=memory_probe_failed error=%s", type(exc).__name__)
    return result


class SemanticIndex:
    def __init__(self, source, config, model_factory=None):
        self.source = source
        self.config = config
        self.model_factory = model_factory  # Small deterministic encoders in unit tests only.
        self.model = None
        self.device = "cpu"
        self.revision = config.semantic_revision
        self.vectors = None
        self.metadata = None
        self.metrics = {}
        self.torch = None

    def _load_model(self):
        if self.model is not None:
            return
        started = perf_counter()
        if self.model_factory:
            self.model = self.model_factory()
        else:
            import torch
            from huggingface_hub import snapshot_download
            from sentence_transformers import SentenceTransformer
            self.torch = torch
            torch.set_num_threads(self.config.semantic_cpu_threads)
            # Resolve only the already downloaded snapshot; no network or token is needed.
            snapshot = snapshot_download(self.config.semantic_model, revision=self.config.semantic_revision or None,
                                         local_files_only=True)
            self.revision = Path(snapshot).name
            self.model = SentenceTransformer(snapshot, device="cpu", local_files_only=True,
                                             model_kwargs={"attn_implementation": "eager"})
            self.model.max_seq_length = 512
            self.metrics["cuda_available"] = torch.cuda.is_available()
            if torch.cuda.is_available():
                try:
                    self.metrics["gpu"] = torch.cuda.get_device_name(0)
                    self.metrics["cuda_capability"] = list(torch.cuda.get_device_capability(0))
                    # is_available alone does not verify kernels for this old GPU.
                    with torch.inference_mode():
                        probe = torch.ones((16, 16), device="cuda")
                        assert float((probe @ probe).sum().item()) == 4096.0
                        torch.cuda.synchronize()
                    self.model.to("cuda")
                    self.device = "cuda"
                    self._encode_raw(["query: drinking water quality"])
                    torch.cuda.synchronize()
                    self.metrics["cuda_probe"] = "passed"
                except Exception as exc:
                    self.metrics["cuda_probe"] = type(exc).__name__
                    self._cpu_fallback(exc)
            else:
                self.metrics["cuda_probe"] = "unavailable"
        self.metrics["model_load_seconds"] = perf_counter() - started
        self.metrics.update(device=self.device, revision=self.revision, **memory_usage(self.torch))
        logger.info("event=e5_loaded device=%s elapsed_ms=%.1f", self.device, self.metrics["model_load_seconds"]*1000)

    def _cpu_fallback(self, error):
        logger.warning("event=e5_cuda_fallback device=cpu error=%s", type(error).__name__)
        self.model.to("cpu")
        self.device = "cpu"
        if self.torch:
            try:
                self.torch.cuda.empty_cache()
            except Exception:
                pass

    def _encode_raw(self, texts):
        return self.model.encode(texts, batch_size=self.config.semantic_batch_size,
                                 normalize_embeddings=True, convert_to_numpy=True,
                                 show_progress_bar=False, device=self.device)

    def encode(self, texts):
        import numpy as np
        try:
            values = self._encode_raw(texts)
        except Exception as exc:
            if self.device != "cuda":
                raise
            self._cpu_fallback(exc)
            values = self._encode_raw(texts)
        values = np.asarray(values, dtype=np.float32)
        if values.shape != (len(texts), DIMENSION) or not np.isfinite(values).all():
            raise ValueError("Invalid E5 embeddings")
        norms = np.linalg.norm(values, axis=1, keepdims=True)
        if (norms < 1e-9).any():
            raise ValueError("Zero E5 embedding")
        return values / norms

    def prepare(self):
        import numpy as np
        preparation_started = perf_counter()
        self._load_model()
        started = perf_counter()
        # Hash the same CSV snapshot the adapter decoded, and retry if it changed during loading.
        digest_before = hashlib.sha256(self.source.path.read_bytes()).hexdigest()
        items = deduplicate(self.source.load())
        digest_after = hashlib.sha256(self.source.path.read_bytes()).hexdigest()
        if digest_before != digest_after:
            raise RuntimeError("Catalog changed during indexing; retry preparation")
        metadata = {
            "schema": INDEX_SCHEMA, "model": self.config.semantic_model, "revision": self.revision,
            "requested_revision": self.config.semantic_revision, "dimension": DIMENSION,
            "csv_sha256": digest_after, "ids": [m.id for m in items],
            "text_sha256": hashlib.sha256(json.dumps([document_text(m) for m in items], ensure_ascii=False).encode()).hexdigest(),
            "max_seq_length": 512, "prefix": "passage:", "normalized": True,
        }
        if metadata == self.metadata and self.vectors is not None:
            return
        path = self.config.semantic_index_dir / "index.npz"
        try:
            with np.load(path, allow_pickle=False) as cached:
                cached_metadata = json.loads(str(cached["metadata"].item()))
                vectors = cached["vectors"]
                if (cached_metadata == metadata and vectors.shape == (len(items), DIMENSION)
                        and vectors.dtype == np.float32 and np.isfinite(vectors).all()
                        and np.allclose(np.linalg.norm(vectors, axis=1), 1, atol=1e-4)):
                    self.vectors, self.metadata = vectors, metadata
                    self.metrics["index_load_seconds"] = perf_counter() - started
                    self.metrics["index_reused"] = True
                    self.metrics.update(prepare_seconds=perf_counter()-preparation_started,
                                        count=len(items), device=self.device, **memory_usage(self.torch))
                    logger.info("event=e5_index_loaded count=%d elapsed_ms=%.1f", len(items), self.metrics["index_load_seconds"]*1000)
                    return
        except (OSError, ValueError, KeyError, EOFError, BadZipFile):
            pass
        build_started = perf_counter()
        vectors = self.encode(["passage: " + document_text(m) for m in items])
        path.parent.mkdir(parents=True, exist_ok=True)
        # Metadata and vectors are in one atomic file; an interrupted rebuild leaves the old index intact.
        with tempfile.NamedTemporaryFile(dir=path.parent, suffix=".tmp", delete=False) as stream:
            temporary = Path(stream.name)
            try:
                np.savez_compressed(stream, vectors=vectors, metadata=json.dumps(metadata))
                stream.flush()
                os.fsync(stream.fileno())
            except BaseException:
                stream.close()
                temporary.unlink(missing_ok=True)
                raise
        try:
            os.replace(temporary, path)
        finally:
            temporary.unlink(missing_ok=True)
        self.vectors, self.metadata = vectors, metadata
        self.metrics.update(index_build_seconds=perf_counter()-build_started, index_reused=False,
                            prepare_seconds=perf_counter()-preparation_started,
                            count=len(items), device=self.device, **memory_usage(self.torch))
        logger.info("event=e5_index_built count=%d device=%s elapsed_ms=%.1f", len(items), self.device,
                    self.metrics["index_build_seconds"]*1000)

    def rank(self, topic: str, allowed_ids: set[str]) -> list[str]:
        import numpy as np
        started = perf_counter()
        self.prepare()
        # Repeated-letter noise is not a meaningful natural-language retrieval request.
        result = []
        if any(c.isalpha() for c in topic) and not re.search(r"([^\W\d_])\1{3,}", topic, re.I):
            vector = self.encode(["query: " + topic])[0]
            similarities = self.vectors @ vector
            indices = [i for i in np.argsort(-similarities) if self.metadata["ids"][i] in allowed_ids]
            # E5 scores are not probabilities. Both absolute and top-relative gates discard weak tails.
            floor = max(self.config.semantic_min_score,
                        float(similarities[indices[0]])-self.config.semantic_score_margin) if indices else 1.0
            result = [self.metadata["ids"][i] for i in indices if float(similarities[i]) >= floor][
                :self.config.semantic_candidates]
        self.metrics.update(query_seconds=perf_counter()-started, device=self.device, **memory_usage(self.torch))
        logger.info("event=e5_query device=%s count=%d elapsed_ms=%.1f", self.device, len(result), self.metrics["query_seconds"]*1000)
        return result


class SemanticSearch:
    """At most one unfinished operation; timed-out native code cannot accumulate workers."""
    def __init__(self, source, config, database, engine=None):
        self.config = config
        self.db = database
        self.engine = engine or SemanticIndex(source, config)
        self.enabled = database.get_app_setting("local_semantic_enabled", config.semantic_enabled)
        self.external_enabled = database.get_app_setting("external_semantic_enabled", config.external_semantic_enabled)
        self.circuit = ""
        self._lock = threading.Lock()
        self._future = None
        self._started = 0.0
        self._operation = ""
        self._generation = 0

    def _submit(self, operation, function, *args):
        with self._lock:
            if self._future is not None and not self._future.done():
                return None
            future = Future()
            self._future = future
            self._started = perf_counter()
            self._operation = operation
            generation = self._generation
        def run():
            try:
                result = function(*args)
                limit = self._time_limit(operation)
                with self._lock:
                    if generation == self._generation and perf_counter()-self._started > limit:
                        self.circuit = "превышено время обработки"
                        logger.warning("event=e5_circuit_open operation=%s reason=timeout", operation)
                future.set_result(result)
            except BaseException as exc:
                logger.warning("event=e5_failed operation=%s error=%s", operation, type(exc).__name__)
                with self._lock:
                    if generation == self._generation:
                        self.circuit = "ошибка модели"
                future.set_exception(exc)
        threading.Thread(target=run, name="e5-worker", daemon=True).start()
        return future

    def _time_limit(self, operation):
        if operation=='prepare':return self.config.semantic_build_timeout
        if operation=='external':return getattr(self.config,'external_semantic_timeout',self.config.semantic_timeout)
        return self.config.semantic_timeout

    def start(self):
        if (self.enabled or self.external_enabled) and not self.circuit:
            self._submit("prepare", self.engine.prepare)

    def state(self) -> str:
        with self._lock:
            busy = self._future is not None and not self._future.done()
            if busy:
                limit = self._time_limit(self._operation)
                if perf_counter()-self._started > limit:
                    if not self.circuit:
                        logger.warning("event=e5_circuit_open operation=%s reason=timeout", self._operation)
                    self.circuit = "превышено время обработки"
            if not self.enabled:
                return "отключена администратором / конфигурацией"
            if self.circuit:
                return f"защита: {self.circuit}; включите E5 повторно через /admin"
            if busy:
                return "индекс строится / модель загружается" if self._operation == "prepare" else "занята другим запросом"
            return "готова" if self.engine.vectors is not None else "ожидает подготовки"

    def set_enabled(self, enabled: bool, external: bool = False):
        key = "external_semantic_enabled" if external else "local_semantic_enabled"
        self.db.set_app_setting(key, enabled)
        if external:
            self.external_enabled = enabled
            if enabled:
                with self._lock:
                    self._generation += 1
                    self.circuit = ''
                if self.engine.vectors is None:
                    self.start()
            return
        with self._lock:
            self._generation += 1
            self.enabled = enabled
            self.circuit = ""
        if enabled and self.engine.vectors is None:
            self.start()

    async def rank(self, topic, allowed_ids):
        state = self.state()
        if not self.enabled or self.circuit:
            return [], f"E5: {state}. {FALLBACK}"
        if self.engine.vectors is None:
            self.start()
            return [], f"E5: {self.state()}. {FALLBACK}"
        generation = self._generation
        future = self._submit("query", self.engine.rank, topic, allowed_ids)
        if future is None:
            return [], f"E5 занята, семантический этап пропущен. {FALLBACK}"
        started = perf_counter()
        # Poll a concurrent Future without cancelling native work or attaching unobserved asyncio exceptions.
        try:
            while not future.done():
                if generation != self._generation or not self.enabled:
                    return [], f"Настройка E5 изменена. {FALLBACK}"
                if perf_counter()-started >= self.config.semantic_timeout:
                    with self._lock:
                        self.circuit = "превышено время обработки"
                    logger.warning("event=e5_timeout device=%s elapsed_ms=%.1f", self.engine.device, (perf_counter()-started)*1000)
                    return [], f"E5 превысила время обработки; включена защита. {FALLBACK}"
                await asyncio.sleep(min(0.02, self.config.semantic_timeout/5))
            if generation != self._generation or not self.enabled:
                return [], f"Настройка E5 изменена. {FALLBACK}"
            if self.circuit:
                return [], f"E5: {self.state()}. {FALLBACK}"
            return future.result(), ""
        except Exception as exc:
            logger.warning("event=e5_query_fallback error=%s", type(exc).__name__)
            return [], f"E5 недоступна. {FALLBACK}"
