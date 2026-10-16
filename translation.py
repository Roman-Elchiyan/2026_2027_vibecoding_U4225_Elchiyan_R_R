"""Only installed ru/en Argos packages. No downloads, one native worker."""
import asyncio
from concurrent.futures import ThreadPoolExecutor
import hashlib
import logging
import os
import re
from time import perf_counter

logger = logging.getLogger(__name__)


class Translator:
    def __init__(self, db, enabled=True, timeout=12):
        self.db, self.enabled, self.timeout = db, enabled, timeout
        self.pool = ThreadPoolExecutor(max_workers=1, thread_name_prefix="argos")
        self.future = None
        self.last_status = "ожидает запроса"

    def _translate(self, text, source, target):
        os.environ.setdefault("ARGOS_DEBUG", "0")
        os.environ.setdefault("ARGOS_INTRA_THREADS", "2")
        from argostranslate import translate
        langs = {l.code:l for l in translate.get_installed_languages()}
        if source not in langs or target not in langs:
            raise LookupError("Пара ru/en не установлена")
        translation = langs[source].get_translation(langs[target])
        if translation is None:
            raise LookupError("Установленная пара недоступна")
        # Argos 1.11 Stanza attempts to refresh resources.json, which can fail
        # for read-only installed packages. Use a local sentence splitter on
        # our translation instance: no writes/downloads or package modifications.
        underlying = translation
        while hasattr(underlying, "underlying"):
            underlying = underlying.underlying
        if hasattr(underlying, "sentencizer"):
            underlying.sentencizer = LocalSentencizer()
        return translation.translate(text)

    async def translate(self, text, source="ru", target="en"):
        if source == target or not text or not self.enabled:
            return text, ""
        if (source, target) not in (("ru","en"),("en","ru")):
            raise ValueError("Поддерживаются только русский и английский.")
        key = "translation:"+source+":"+target+":"+hashlib.sha256(text.encode()).hexdigest()
        cached = self.db.cache_get(key)
        if cached:
            logger.info("event=translation_cache_hit direction=%s_%s",source,target)
            return cached, ""
        if self.future and not self.future.done():
            return text, "Переводчик занят; показан оригинал."
        started = perf_counter()
        self.future = self.pool.submit(self._translate, text[:6000], source, target)
        try:
            wrapped = asyncio.wrap_future(self.future)
            wrapped.add_done_callback(lambda completed: completed.exception() if not completed.cancelled() else None)
            result = await asyncio.wait_for(asyncio.shield(wrapped),self.timeout)
            self.db.cache_put(key,result,30*86400)
            self.last_status = "ru↔en готовы"
            logger.info("event=translation_ok direction=%s_%s elapsed_ms=%.1f",source,target,(perf_counter()-started)*1000)
            return result, ""
        except Exception as exc:
            self.last_status = type(exc).__name__
            logger.warning("event=translation_unavailable error=%s",type(exc).__name__)
            return text, "Перевод временно недоступен; используется оригинал."

    async def query(self,text):
        return await self.translate(text) if re.search("[а-яА-ЯёЁ]",text) else (text,"")

    def close(self):
        self.pool.shutdown(wait=False,cancel_futures=True)


class LocalSentencizer:
    def split_sentences(self, text):
        return [piece.strip() for piece in re.split(r"(?<=[.!?])\s+(?=[A-ZА-ЯЁ])|\n+",text) if piece.strip()] or [text]
