"""Exceptions of the AI layer. `AIUnavailable` is also `internest_skills.llm.LLMUnavailable`, so every existing caller
that falls back to local logic on LLMUnavailable handles quota refusals the same way."""
from django.utils.translation import gettext


class AIUnavailable(RuntimeError):
    """The AI could not produce a usable answer; use the local fallback."""


class QuotaExceeded(AIUnavailable):
    """A usage cap was reached. `retry_after` is the number of seconds until the cap resets."""

    def __init__(self, scope: str, retry_after: int, limit: int = 0):
        self.scope, self.retry_after, self.limit = scope, max(1, int(retry_after)), limit
        super().__init__(f"AI quota exceeded ({scope}, limit {limit}); retry after {self.retry_after}s")

    @property
    def user_message(self) -> str:
        if self.scope in ("user_daily", "partner_daily"):
            return gettext("You have reached today's AI limit for this feature. Please try again tomorrow.")
        return gettext("The AI is very busy right now. Please try again in a moment.")


class ShapeError(ValueError):
    """An AI reply does not have the expected JSON shape."""
