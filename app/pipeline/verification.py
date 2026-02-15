import asyncio
import logging
from datetime import datetime, timedelta
from difflib import SequenceMatcher
from urllib.parse import urlparse

import httpx

from app.database import get_db

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Tier domain lists (easy to update)
# ---------------------------------------------------------------------------

_TIER_1_DOMAINS = {
    "federalreserve.gov", "sec.gov", "finra.org", "treasury.gov", "bls.gov",
    "cbre.com", "jll.com", "cushmanwakefield.com",
    "bloomberg.com", "wsj.com", "ft.com", "reuters.com",
}

_TIER_2_DOMAINS = {
    "pere.com", "globest.com", "bisnow.com", "commercialobserver.com",
    "zawya.com", "preqin.com", "pitchbook.com", "nareit.com",
}

_PAYWALL_DOMAINS = {
    "wsj.com", "ft.com", "bloomberg.com", "barrons.com",
    "economist.com", "nytimes.com",
}

_TIER_WEIGHTS = {1: 1.0, 2: 0.7, 3: 0.3}

_LINK_TIMEOUT = httpx.Timeout(5.0, connect=3.0)
_LINK_SEMAPHORE_LIMIT = 10


# ============================= PUBLIC API ==================================


async def run_verification(edition_id: int) -> None:
    """Run all verification checks on articles for an edition.

    Checks run in order: tier classification → link validation →
    paywall detection → deduplication → quality scoring.
    """
    db = await get_db()
    try:
        cursor = await db.execute(
            "SELECT id, title, url, source, source_tier, is_paywalled, "
            "is_duplicate, quality_score, retrieved_at "
            "FROM articles WHERE edition_id = ?",
            (edition_id,),
        )
        rows = await cursor.fetchall()
    finally:
        await db.close()

    if not rows:
        logger.info("Edition %d: no articles to verify", edition_id)
        return

    # Convert Row objects to mutable dicts
    articles = [dict(row) for row in rows]
    logger.info("Edition %d: verifying %d articles", edition_id, len(articles))

    # Check A — Tier classification
    _classify_tiers(articles)

    # Check B — Link validation
    await _validate_links(articles)

    # Check C — Paywall detection
    _detect_paywalls(articles)

    # Check D — Deduplication
    _deduplicate(articles)

    # Check E — Quality scoring
    _compute_scores(articles)

    # Save results
    await _save_verification_results(articles)

    # Log summary
    tier_counts = {1: 0, 2: 0, 3: 0}
    paywalled = 0
    duplicates = 0
    for a in articles:
        tier_counts[a["source_tier"]] = tier_counts.get(a["source_tier"], 0) + 1
        if a["is_paywalled"]:
            paywalled += 1
        if a["is_duplicate"]:
            duplicates += 1

    logger.info(
        "Edition %d verification complete: tiers=%s, paywalled=%d, duplicates=%d",
        edition_id, tier_counts, paywalled, duplicates,
    )


# ============================= HELPERS =====================================


def _extract_domain(url: str) -> str | None:
    """Extract the domain from a URL, stripping www. prefix."""
    try:
        netloc = urlparse(url).netloc.lower()
        if netloc.startswith("www."):
            netloc = netloc[4:]
        return netloc if netloc else None
    except Exception:
        return None


def _domain_matches(domain: str, domain_set: set[str]) -> bool:
    """Check if domain matches any entry in the set (including subdomains)."""
    if domain in domain_set:
        return True
    # Check if domain is a subdomain of any entry
    for entry in domain_set:
        if domain.endswith("." + entry):
            return True
    return False


# ============================= CHECK A: TIER CLASSIFICATION ================


def _classify_tiers(articles: list[dict]) -> None:
    """Classify each article into Tier 1, 2, or 3 based on URL domain."""
    for article in articles:
        # FRED and EDGAR are always Tier 1
        if article["source"] in ("fred", "edgar"):
            article["source_tier"] = 1
            continue

        url = article.get("url")
        if not url:
            # No URL — keep existing tier (set at retrieval)
            continue

        domain = _extract_domain(url)
        if not domain:
            continue

        if _domain_matches(domain, _TIER_1_DOMAINS):
            article["source_tier"] = 1
        elif _domain_matches(domain, _TIER_2_DOMAINS):
            article["source_tier"] = 2
        else:
            article["source_tier"] = 3

    logger.info("Tier classification complete")


# ============================= CHECK B: LINK VALIDATION ====================


async def _validate_links(articles: list[dict]) -> None:
    """Check if article URLs are reachable via HEAD request.

    Skips FRED (constructed URLs), EDGAR (government source), and
    known paywall domains (they block automated requests but are live).
    Skips known Tier 1/2 domains (many block automated requests with
    401/403) and paywall domains. Falls back to GET if HEAD returns
    a 4xx/5xx status for unknown domains.
    """
    # Combine all trusted domains — these block automated requests
    _TRUSTED_DOMAINS = _TIER_1_DOMAINS | _TIER_2_DOMAINS | _PAYWALL_DOMAINS
    semaphore = asyncio.Semaphore(_LINK_SEMAPHORE_LIMIT)

    async def check_one(article: dict, client: httpx.AsyncClient) -> None:
        url = article.get("url")

        # Skip: no URL, known-good sources
        if not url or article["source"] in ("fred", "edgar"):
            article["link_valid"] = True
            return

        # Skip: known Tier 1/2 and paywall domains (block automated requests)
        domain = _extract_domain(url)
        if domain and _domain_matches(domain, _TRUSTED_DOMAINS):
            article["link_valid"] = True
            return

        async with semaphore:
            try:
                resp = await client.head(url)
                if resp.status_code >= 400:
                    # HEAD blocked or failed — try GET as fallback
                    resp = await client.get(url, follow_redirects=True)
                article["link_valid"] = resp.status_code < 400
            except Exception:
                article["link_valid"] = False

    async with httpx.AsyncClient(
        timeout=_LINK_TIMEOUT,
        follow_redirects=True,
    ) as client:
        tasks = [check_one(a, client) for a in articles]
        await asyncio.gather(*tasks, return_exceptions=True)

    valid = sum(1 for a in articles if a.get("link_valid", True))
    logger.info(
        "Link validation complete: %d/%d valid", valid, len(articles),
    )


# ============================= CHECK C: PAYWALL DETECTION ==================


def _detect_paywalls(articles: list[dict]) -> None:
    """Mark articles from known paywall domains."""
    count = 0
    for article in articles:
        url = article.get("url")
        if not url:
            continue

        domain = _extract_domain(url)
        if domain and _domain_matches(domain, _PAYWALL_DOMAINS):
            article["is_paywalled"] = 1
            count += 1

    logger.info("Paywall detection complete: %d paywalled", count)


# ============================= CHECK D: DEDUPLICATION ======================


def _deduplicate(articles: list[dict]) -> None:
    """Mark duplicate articles based on title similarity."""
    count = 0
    for i, a in enumerate(articles):
        if a.get("is_duplicate"):
            continue
        for j in range(i + 1, len(articles)):
            b = articles[j]
            if b.get("is_duplicate"):
                continue

            ratio = SequenceMatcher(
                None, a["title"].lower(), b["title"].lower()
            ).ratio()

            if ratio > 0.75:
                # Mark the lower-tier article as duplicate
                # Higher tier number = lower quality tier
                if a["source_tier"] > b["source_tier"]:
                    a["is_duplicate"] = 1
                    count += 1
                    break  # a is marked, stop comparing it
                else:
                    b["is_duplicate"] = 1
                    count += 1

    logger.info("Deduplication complete: %d duplicates found", count)


# ============================= CHECK E: QUALITY SCORING ====================


def _compute_scores(articles: list[dict]) -> None:
    """Compute quality_score for each article."""
    now = datetime.now()

    for article in articles:
        if article.get("is_duplicate"):
            article["quality_score"] = 0.0
            continue

        # Tier weight
        tier = article.get("source_tier", 3)
        tier_weight = _TIER_WEIGHTS.get(tier, 0.3)

        # Recency score
        retrieved_at = article.get("retrieved_at")
        if retrieved_at:
            try:
                if isinstance(retrieved_at, str):
                    retrieved_dt = datetime.fromisoformat(retrieved_at)
                else:
                    retrieved_dt = retrieved_at
                age = now - retrieved_dt
            except (ValueError, TypeError):
                age = timedelta(days=0)
        else:
            age = timedelta(days=0)

        if age < timedelta(days=3):
            recency_score = 1.0
        elif age < timedelta(days=7):
            recency_score = 0.8
        elif age < timedelta(days=14):
            recency_score = 0.5
        else:
            recency_score = 0.2

        # Relevance score (placeholder)
        relevance_score = 1.0

        # Accessibility
        link_valid = article.get("link_valid", True)
        if not link_valid:
            accessibility = 0.0
        elif article.get("is_paywalled"):
            accessibility = 0.5
        else:
            accessibility = 1.0

        article["quality_score"] = round(
            tier_weight * recency_score * relevance_score * accessibility, 2
        )

    logger.info("Quality scoring complete")


# ============================= DB UPDATE ===================================


async def _save_verification_results(articles: list[dict]) -> None:
    """Write verification results back to the articles table."""
    db = await get_db()
    try:
        await db.executemany(
            "UPDATE articles SET source_tier=?, quality_score=?, is_paywalled=?, is_duplicate=? "
            "WHERE id=?",
            [
                (
                    a["source_tier"],
                    a["quality_score"],
                    a.get("is_paywalled", 0),
                    a.get("is_duplicate", 0),
                    a["id"],
                )
                for a in articles
            ],
        )
        await db.commit()
    finally:
        await db.close()

    logger.info("Verification results saved for %d articles", len(articles))
