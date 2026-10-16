"""Lab 2 contract for personal LiteLLM virtual keys, usage and limit requests."""

from typing import Protocol

LIMITS_UNAVAILABLE = "Управление лимитами будет доступно после подключения LiteLLM в Lab 2."


class BudgetAdapter(Protocol):
    async def get_limits(self, user_id: int) -> dict: ...
    async def request_limit(self, user_id: int, reason: str) -> None: ...


class DisabledBudget:
    async def get_limits(self, user_id: int) -> dict:
        raise NotImplementedError(LIMITS_UNAVAILABLE)

    async def request_limit(self, user_id: int, reason: str) -> None:
        raise NotImplementedError(LIMITS_UNAVAILABLE)
