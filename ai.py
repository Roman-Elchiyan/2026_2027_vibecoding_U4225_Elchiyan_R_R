"""Lab 2 contract: Responses API through LiteLLM Proxy. No client in Lab 1."""

from typing import Protocol

from search import SearchFilters, SearchResult

AI_UNAVAILABLE = "ИИ-поиск будет подключён в Lab 2. Сейчас доступен локальный поиск: /search."


class AiSearchAdapter(Protocol):
    async def search(self, user_id: int, filters: SearchFilters) -> SearchResult: ...


class DisabledAi:
    async def search(self, user_id: int, filters: SearchFilters) -> SearchResult:
        raise NotImplementedError(AI_UNAVAILABLE)
