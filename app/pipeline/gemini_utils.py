import asyncio
import logging
from collections.abc import Awaitable, Callable
from typing import TypeVar

try:
    from google.api_core.exceptions import ResourceExhausted
except ImportError:
    ResourceExhausted = None

logger = logging.getLogger(__name__)

T = TypeVar("T")

_RETRY_DELAYS = [15, 30]


async def call_with_retry(
    fn: Callable[[], Awaitable[T]],
    *,
    label: str = "Gemini call",
) -> T:
    """Call an async Gemini function with retry on rate-limit (429) errors.

    Attempts the call up to 3 times:
      1. Initial attempt
      2. On 429: wait 15s, retry
      3. On 429: wait 30s, retry
    If all 3 attempts fail with 429, re-raises the last exception.
    Non-429 errors are raised immediately.
    """
    last_exc: Exception | None = None
    for attempt in range(3):
        try:
            return await fn()
        except Exception as exc:
            if not _is_rate_limit(exc):
                raise
            last_exc = exc
            if attempt < len(_RETRY_DELAYS):
                delay = _RETRY_DELAYS[attempt]
                logger.warning(
                    "%s: rate limited (429), waiting %ds before retry %d/2",
                    label, delay, attempt + 1,
                )
                await asyncio.sleep(delay)
            else:
                logger.error(
                    "%s: rate limited (429) after 3 attempts, giving up", label
                )
                raise
    raise last_exc  # type: ignore[misc]


def _is_rate_limit(exc: Exception) -> bool:
    """Check if an exception is a Gemini rate-limit (429) error."""
    if ResourceExhausted is not None and isinstance(exc, ResourceExhausted):
        return True
    exc_str = str(exc).lower()
    return "429" in exc_str or "resource exhausted" in exc_str
