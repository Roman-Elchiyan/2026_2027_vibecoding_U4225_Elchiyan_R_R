"""Optional multilingual Cross-Encoder; offline loading, one bounded worker."""
import asyncio
from concurrent.futures import Future
import logging
import math
from pathlib import Path
import threading
from time import perf_counter

from relevance import material_text
from semantic_search import memory_usage

logger = logging.getLogger(__name__)
MODEL = 'cross-encoder/mmarco-mMiniLMv2-L12-H384-v1'
PREPROCESSING = 'title-abstract-head-tail-v1'


class CrossEncoderRanker:
    def __init__(self, config, semantic=None, model_factory=None):
        self.config, self.semantic, self.model_factory = config, semantic, model_factory
        self.model = None
        self.torch = None
        self.device = 'cpu'
        self.metrics = {}
        self.circuit = ''
        self._lock = threading.Lock()
        self._future = None

    def state(self):
        if not self.config.reranker_enabled:
            return 'отключён'
        if self.circuit:
            return 'резервный режим: ' + self.circuit
        if self._future is not None and not self._future.done():
            return 'занят'
        return 'готов (' + self.device + ')' if self.model else 'ожидает локальных весов / первого запроса'

    def _load(self):
        if self.model is not None:
            return
        if self.model_factory:
            self.model = self.model_factory()
            return
        import torch
        from sentence_transformers import CrossEncoder
        self.torch = torch
        torch.set_num_threads(self.config.semantic_cpu_threads)
        path = Path(self.config.reranker_model_dir)
        source = str(path) if path.is_dir() and (path / 'config.json').is_file() else MODEL
        self.model = CrossEncoder(source, device='cpu', max_length=self.config.reranker_max_length,
            local_files_only=True, revision=self.config.reranker_revision or None,
            trust_remote_code=False, model_kwargs={'attn_implementation': 'eager'})
        # CPU is the default. Never share the GTX 960 with an E5 already on
        # CUDA, or one which could still migrate there during its first load.
        engine = getattr(self.semantic, 'engine', None)
        e5_may_use_gpu = bool(self.semantic and (
            getattr(engine,'model',None) is not None and getattr(engine,'device','cpu')!='cpu' or
            (self.semantic.enabled or self.semantic.external_enabled) and getattr(engine,'model',None) is None))
        if self.config.reranker_device in ('auto', 'cuda') and not e5_may_use_gpu and torch.cuda.is_available():
            try:
                free, _ = torch.cuda.mem_get_info()
                weight_bytes = sum(p.numel() * p.element_size() for p in self.model.model.parameters())
                if free < weight_bytes * 2 + 512 * 2**20:
                    raise RuntimeError('insufficient_vram')
                # The actual kernel probe, not just CUDA availability, matters
                # for Maxwell / a potentially incompatible PyTorch build.
                probe = torch.ones((16, 16), device='cuda')
                assert float((probe @ probe).sum().item()) == 4096
                self.model.model.to('cuda')
                self.device = 'cuda'
                self._predict([('test', 'test')])
                torch.cuda.synchronize()
            except Exception as exc:
                self.model.model.to('cpu')
                self.device = 'cpu'
                torch.cuda.empty_cache()
                logger.warning('event=ce_cuda_fallback error=%s', type(exc).__name__)

    def _predict(self, pairs):
        options = dict(batch_size=self.config.reranker_batch_size, show_progress_bar=False, convert_to_numpy=True)
        if self.torch is not None:
            options['activation_fn'] = self.torch.nn.Identity()  # raw logits, not probabilities
        return self.model.predict(pairs, **options)

    def _work(self, topic, items):
        started = perf_counter()
        self._load()
        pairs, owners = [], []
        tokenizer=getattr(self.model,'tokenizer',None)
        for item in items:
            title=item.title[:1000]
            abstract=item.abstract_or_snippet or ''
            documents=[title+'\n'+abstract]
            if tokenizer is not None:
                query_tokens=tokenizer.encode(topic[:500],add_special_tokens=False,verbose=False)
                title_tokens=tokenizer.encode(title,add_special_tokens=False,verbose=False)
                capacity=max(16,self.config.reranker_max_length-len(query_tokens)-min(len(title_tokens),80)-6)
                abstract_tokens=tokenizer.encode(abstract,add_special_tokens=False,verbose=False)
                if len(abstract_tokens)>capacity:
                    short_title=tokenizer.decode(title_tokens[:80],skip_special_tokens=True)
                    documents=[short_title+'\n'+tokenizer.decode(window,skip_special_tokens=True)
                        for window in (abstract_tokens[:capacity],abstract_tokens[-capacity:])]
            for document in documents:
                pairs.append((topic[:500],document));owners.append(item.id)
        try:
            values = self._predict(pairs)
        except Exception:
            if self.device != 'cuda':
                raise
            self.model.model.to('cpu')
            self.device = 'cpu'
            self.torch.cuda.empty_cache()
            values = self._predict(pairs)
        values = [float(v) for v in values]
        if len(values) != len(pairs) or not all(math.isfinite(v) for v in values):
            raise ValueError('invalid_cross_encoder_scores')
        self.metrics = dict(elapsed_seconds=perf_counter() - started, count=len(items), pairs=len(pairs), device=self.device,
            **memory_usage(self.torch))
        logger.info('event=cross_encoder count=%d device=%s elapsed_ms=%.1f memory=%s',
            len(items), self.device, self.metrics['elapsed_seconds'] * 1000, self.metrics)
        scores={}
        for mid,value in zip(owners,values):scores[mid]=max(scores.get(mid,-math.inf),value)
        return scores

    async def rank(self, topic, items):
        if not self.config.reranker_enabled or not items:
            return items, {}, ''
        if self.circuit:
            return items, {}, 'MiniLM недоступна; работает E5/текстовый отбор. ' + self.circuit
        selected = items[:self.config.reranker_candidates]
        with self._lock:
            if self._future is not None and not self._future.done():
                return items, {}, 'MiniLM занята; сохранён предварительный отбор.'
            future = self._future = Future()
        def run():
            try:
                future.set_result(self._work(topic, selected))
            except BaseException as exc:
                logger.warning('event=ce_unavailable error=%s', type(exc).__name__)
                self.circuit = 'Нет локальной модели или ошибка вычисления; подробности в /status.'
                future.set_exception(exc)
        threading.Thread(target=run, name='cross-encoder-worker', daemon=True).start()
        started = perf_counter()
        timeout=max(30.0,self.config.reranker_timeout) if self.model is None and self.model_factory is None else self.config.reranker_timeout
        while not future.done():
            if perf_counter() - started > timeout:
                self.circuit = 'Превышено время MiniLM; для сброса защиты перезапустите бота.'
                return items, {}, self.circuit
            await asyncio.sleep(0.02)
        try:
            scores = future.result()
            ordered = sorted(selected, key=lambda item: -scores[item.id])
            return ordered + items[len(selected):], scores, ''
        except Exception:
            return items, {}, 'MiniLM недоступна; работает E5/текстовый отбор. Веса автоматически не скачиваются.'
