import asyncio
import logging
import time

import google.generativeai as genai

from app.config import settings
from app.database import get_db
from app.pipeline.gemini_utils import call_with_retry
from app.pipeline.prompts import (
    NO_ARTICLES_ADDENDUM,
    PERSPECTIVE_PLACEHOLDER,
    SECTION_ARTICLE_LIMITS,
    SECTION_CATEGORIES,
    SECTION_ORDER,
    SECTION_PROMPTS,
    VOICE_SYSTEM_PROMPT,
)

logger = logging.getLogger(__name__)


# ============================= PUBLIC API ==================================


async def run_drafting(edition_id: int, *, editorial_brief: str | None = None) -> None:
    """Generate all newsletter sections for an edition using Gemini.

    Sections are generated sequentially to respect rate limits.
    If the Gemini API key is not set, logs a warning and returns.
    """
    if not settings.gemini_api_key:
        logger.warning("Gemini API key not set — skipping drafting")
        return

    genai.configure(api_key=settings.gemini_api_key)

    model = genai.GenerativeModel(
        model_name="gemini-2.5-flash",
        system_instruction=VOICE_SYSTEM_PROMPT,
        generation_config=genai.GenerationConfig(
            temperature=0.7,
            max_output_tokens=4096,
        ),
    )

    # Fetch all usable articles for this edition (one query, filter in memory)
    db = await get_db()
    try:
        cursor = await db.execute(
            "SELECT id, title, url, source, source_tier, relevance_category, raw_snippet "
            "FROM articles WHERE edition_id = ? AND is_duplicate = 0 AND quality_score > 0 "
            "ORDER BY quality_score DESC",
            (edition_id,),
        )
        all_articles = [dict(row) for row in await cursor.fetchall()]
    finally:
        await db.close()

    logger.info(
        "Edition %d: drafting %d sections with %d usable articles (model=%s)",
        edition_id, len(SECTION_ORDER), len(all_articles), "gemini-2.5-flash",
    )

    start_time = time.monotonic()

    gemini_call_count = 0
    for section_name in SECTION_ORDER:
        if section_name != "perspective" and gemini_call_count > 0:
            await asyncio.sleep(2)
        await _generate_section(edition_id, section_name, model, all_articles, editorial_brief)
        if section_name != "perspective":
            gemini_call_count += 1

    elapsed = round(time.monotonic() - start_time, 1)
    logger.info("Edition %d: drafting complete in %.1fs", edition_id, elapsed)


# ============================= HELPERS =====================================


async def _generate_section(
    edition_id: int,
    section_name: str,
    model: genai.GenerativeModel,
    all_articles: list[dict],
    editorial_brief: str | None = None,
) -> None:
    """Generate and store a single newsletter section."""

    # Perspective — static placeholder, no LLM call
    if section_name == "perspective":
        await _store_section(
            edition_id, section_name, PERSPECTIVE_PLACEHOLDER, model_used="static"
        )
        word_count = len(PERSPECTIVE_PLACEHOLDER.split())
        logger.info(
            "Edition %d [%s]: stored placeholder (%d words)",
            edition_id, section_name, word_count,
        )
        return

    # Filter articles by relevance category for this section
    categories = SECTION_CATEGORIES.get(section_name, [])
    limit = SECTION_ARTICLE_LIMITS.get(section_name, 5)
    section_articles = [
        a for a in all_articles
        if a.get("relevance_category") in categories
    ][:limit]

    # Format articles for prompt context
    articles_context = _format_articles(section_articles) if section_articles else ""

    # Build prompt
    prompt_template = SECTION_PROMPTS[section_name]
    prompt = prompt_template.format(articles_context=articles_context)
    if not section_articles:
        prompt += NO_ARTICLES_ADDENDUM

    # Prepend editorial direction for guided mode
    if editorial_brief:
        prompt = (
            f"EDITORIAL DIRECTION: {editorial_brief}\n"
            "Prioritize this theme in your analysis while maintaining balanced coverage.\n\n"
            + prompt
        )

    logger.info(
        "Edition %d [%s]: generating (%d articles in context)",
        edition_id, section_name, len(section_articles),
    )

    # Call Gemini (with rate-limit retry)
    try:
        response = await call_with_retry(
            lambda: model.generate_content_async(prompt),
            label=f"Edition {edition_id} [{section_name}]",
        )
        content = response.text if response.text else "[No content generated]"
    except Exception:
        logger.exception(
            "Edition %d [%s]: Gemini call failed", edition_id, section_name
        )
        content = "[Draft generation failed for this section. Error logged.]"

    # Store result
    await _store_section(edition_id, section_name, content, model_used="gemini-2.5-flash")
    word_count = len(content.split())
    logger.info(
        "Edition %d [%s]: %d words generated", edition_id, section_name, word_count
    )


def _format_articles(articles: list[dict]) -> str:
    """Format a list of article dicts for injection into an LLM prompt."""
    parts: list[str] = []
    for i, a in enumerate(articles, 1):
        lines = [f"[{i}] {a['title']}"]
        lines.append(f"Source: {a.get('source', 'unknown')} (Tier {a.get('source_tier', 3)})")
        if a.get("url"):
            lines.append(f"URL: {a['url']}")
        snippet = a.get("raw_snippet", "")
        if snippet:
            lines.append(f"Summary: {snippet[:500]}")
        parts.append("\n".join(lines))
    return "\n\n".join(parts)


async def _store_section(
    edition_id: int, section_name: str, content: str, model_used: str
) -> None:
    """Insert a section draft into the database."""
    word_count = len(content.split())
    db = await get_db()
    try:
        await db.execute(
            "INSERT INTO section_drafts (edition_id, section_name, content, word_count, model_used) "
            "VALUES (?, ?, ?, ?, ?)",
            (edition_id, section_name, content, word_count, model_used),
        )
        await db.commit()
    finally:
        await db.close()
