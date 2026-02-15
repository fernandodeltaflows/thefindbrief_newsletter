import asyncio
import logging
import re
from datetime import datetime, timedelta

import httpx

from app.config import settings
from app.database import get_db

logger = logging.getLogger(__name__)

_TIMEOUT = httpx.Timeout(30.0, connect=10.0)

# ---------------------------------------------------------------------------
# Perplexity queries: (user_query, relevance_category)
# ---------------------------------------------------------------------------
_PERPLEXITY_QUERIES = [
    (
        "Recent GCC sovereign wealth fund real estate investments and deals 2026",
        "regional",
    ),
    (
        "LATAM institutional real estate capital flows Mexico Colombia 2026",
        "regional",
    ),
    (
        "US commercial real estate market conditions cap rates multifamily industrial 2026",
        "macro",
    ),
    (
        "Cross-border real estate fund launches LP GP allocations 2026",
        "deals",
    ),
    (
        "CFIUS real estate regulation SEC FINRA compliance updates 2026",
        "regulatory",
    ),
]

_PERPLEXITY_SYSTEM = (
    "You are a financial research assistant. Return a list of recent news "
    "articles, reports, or data points about the topic. For each item, "
    "provide the title, source URL if available, and a brief summary. "
    "Format as a numbered list."
)

# ---------------------------------------------------------------------------
# SerpAPI queries: (query, relevance_category)
# ---------------------------------------------------------------------------
_SERPAPI_QUERIES = [
    ("cross-border real estate investment 2026", "deals"),
    ("GCC sovereign wealth fund real estate", "regional"),
    ("LATAM real estate fund institutional", "regional"),
    ("US commercial real estate market", "macro"),
]

# ---------------------------------------------------------------------------
# FRED series: (series_id, label)
# ---------------------------------------------------------------------------
_FRED_SERIES = [
    ("FEDFUNDS", "Fed Funds Rate"),
    ("DGS10", "10-Year Treasury Yield"),
    ("CPIAUCSL", "CPI"),
]

# ---------------------------------------------------------------------------
# URL extraction regex
# ---------------------------------------------------------------------------
_URL_RE = re.compile(r"https?://[^\s\)\]\>,\"']+")


# ============================= PUBLIC API ==================================


async def run_retrieval(edition_id: int) -> int:
    """Fetch articles from all sources concurrently and store in DB.

    Returns total number of articles stored.
    """
    tasks = [
        _fetch_perplexity(edition_id),
        _fetch_serpapi(edition_id),
        _fetch_edgar(edition_id),
        _fetch_fred(edition_id),
    ]
    results = await asyncio.gather(*tasks, return_exceptions=True)

    all_articles: list[dict] = []
    source_names = ["perplexity", "serpapi", "edgar", "fred"]

    for name, result in zip(source_names, results):
        if isinstance(result, Exception):
            logger.error("Source %s failed: %s", name, result)
            continue
        logger.info("Source %s returned %d articles", name, len(result))
        all_articles.extend(result)

    if all_articles:
        count = await _store_articles(all_articles)
        logger.info(
            "Edition %d: stored %d articles total from retrieval", edition_id, count
        )
        return count

    logger.warning("Edition %d: no articles retrieved from any source", edition_id)
    return 0


# ============================= STORAGE =====================================


async def _store_articles(articles: list[dict]) -> int:
    """Insert article dicts into the articles table. Returns count inserted."""
    db = await get_db()
    try:
        await db.executemany(
            """INSERT INTO articles
               (edition_id, title, url, source, source_tier, relevance_category, raw_snippet)
               VALUES (:edition_id, :title, :url, :source, :source_tier, :relevance_category, :raw_snippet)""",
            articles,
        )
        await db.commit()
        return len(articles)
    finally:
        await db.close()


# ============================= PERPLEXITY ==================================


async def _perplexity_single_query(
    client: httpx.AsyncClient,
    query: str,
    category: str,
    edition_id: int,
) -> list[dict]:
    """Send one query to Perplexity and parse the response forgivingly."""
    resp = await client.post(
        "https://api.perplexity.ai/chat/completions",
        json={
            "model": "sonar",
            "messages": [
                {"role": "system", "content": _PERPLEXITY_SYSTEM},
                {"role": "user", "content": query},
            ],
        },
    )
    resp.raise_for_status()
    data = resp.json()
    content = data.get("choices", [{}])[0].get("message", {}).get("content", "")

    if not content.strip():
        return []

    return _parse_perplexity_response(content, category, edition_id, query)


def _parse_perplexity_response(
    text: str,
    category: str,
    edition_id: int,
    query: str,
) -> list[dict]:
    """Parse Perplexity response forgivingly. Never discard data."""
    articles: list[dict] = []

    # Try numbered lines first: "1.", "2.", etc.
    items = re.split(r"\n\s*\d+[\.\)]\s+", text)
    # First split element is text before "1." — usually empty or preamble
    if len(items) > 1:
        items = items[1:]  # drop preamble
    else:
        # Try bullet points
        items = re.split(r"\n\s*[\-\*\u2022]\s+", text)
        if len(items) > 1:
            items = items[1:]
        else:
            # Try double-newline paragraphs
            items = [p.strip() for p in text.split("\n\n") if p.strip()]

    if not items:
        # Nothing parseable — store the entire response as one article
        articles.append(
            {
                "edition_id": edition_id,
                "title": query[:200],
                "url": None,
                "source": "perplexity",
                "source_tier": 3,
                "relevance_category": category,
                "raw_snippet": text[:2000],
            }
        )
        return articles

    for item in items:
        item = item.strip()
        if not item:
            continue

        # Try to extract a URL
        url_match = _URL_RE.search(item)
        url = url_match.group(0).rstrip(".)") if url_match else None

        # Try to extract a title (first line, or text before URL, or first sentence)
        lines = item.split("\n")
        title_candidate = lines[0].strip()
        # Clean markdown bold/links from title
        title_candidate = re.sub(r"\[([^\]]+)\]\([^\)]+\)", r"\1", title_candidate)
        title_candidate = re.sub(r"\*\*([^*]+)\*\*", r"\1", title_candidate)
        title_candidate = title_candidate.strip("* -#")

        if len(title_candidate) > 200:
            title_candidate = title_candidate[:197] + "..."
        if not title_candidate:
            title_candidate = query[:200]

        articles.append(
            {
                "edition_id": edition_id,
                "title": title_candidate,
                "url": url,
                "source": "perplexity",
                "source_tier": 3,
                "relevance_category": category,
                "raw_snippet": item[:2000],
            }
        )

    # Fallback: if parsing produced zero articles from non-empty items, store whole text
    if not articles:
        articles.append(
            {
                "edition_id": edition_id,
                "title": query[:200],
                "url": None,
                "source": "perplexity",
                "source_tier": 3,
                "relevance_category": category,
                "raw_snippet": text[:2000],
            }
        )

    return articles


async def _fetch_perplexity(edition_id: int) -> list[dict]:
    """Fetch articles from Perplexity API."""
    if not settings.perplexity_api_key:
        logger.warning("Perplexity API key not set — skipping")
        return []

    articles: list[dict] = []
    async with httpx.AsyncClient(
        headers={
            "Authorization": f"Bearer {settings.perplexity_api_key}",
            "Content-Type": "application/json",
        },
        timeout=_TIMEOUT,
    ) as client:
        tasks = [
            _perplexity_single_query(client, query, category, edition_id)
            for query, category in _PERPLEXITY_QUERIES
        ]
        results = await asyncio.gather(*tasks, return_exceptions=True)

        for i, result in enumerate(results):
            if isinstance(result, Exception):
                query_text = _PERPLEXITY_QUERIES[i][0]
                logger.error("Perplexity query failed (%s): %s", query_text, result)
                continue
            articles.extend(result)

    logger.info("Perplexity: %d articles from %d queries", len(articles), len(_PERPLEXITY_QUERIES))
    return articles


# ============================= SERPAPI =====================================


async def _serpapi_single_query(
    client: httpx.AsyncClient,
    query: str,
    category: str,
    edition_id: int,
) -> list[dict]:
    """Run one SerpAPI Google News search."""
    resp = await client.get(
        "https://serpapi.com/search",
        params={
            "engine": "google_news",
            "q": query,
            "api_key": settings.serpapi_api_key,
        },
    )
    resp.raise_for_status()
    data = resp.json()

    articles: list[dict] = []
    for item in data.get("news_results", []):
        title = item.get("title", "").strip()
        if not title:
            continue
        articles.append(
            {
                "edition_id": edition_id,
                "title": title[:200],
                "url": item.get("link"),
                "source": "serpapi",
                "source_tier": 3,
                "relevance_category": category,
                "raw_snippet": item.get("snippet", "")[:2000],
            }
        )

    return articles


async def _fetch_serpapi(edition_id: int) -> list[dict]:
    """Fetch articles from SerpAPI Google News."""
    if not settings.serpapi_api_key:
        logger.warning("SerpAPI key not set — skipping")
        return []

    articles: list[dict] = []
    async with httpx.AsyncClient(timeout=_TIMEOUT) as client:
        tasks = [
            _serpapi_single_query(client, query, category, edition_id)
            for query, category in _SERPAPI_QUERIES
        ]
        results = await asyncio.gather(*tasks, return_exceptions=True)

        for i, result in enumerate(results):
            if isinstance(result, Exception):
                query_text = _SERPAPI_QUERIES[i][0]
                logger.error("SerpAPI query failed (%s): %s", query_text, result)
                continue
            articles.extend(result)

    logger.info("SerpAPI: %d articles from %d queries", len(articles), len(_SERPAPI_QUERIES))
    return articles


# ============================= SEC EDGAR ===================================


async def _fetch_edgar(edition_id: int) -> list[dict]:
    """Fetch recent real-estate-related filings from SEC EDGAR full-text search."""
    today = datetime.now().strftime("%Y-%m-%d")
    start = (datetime.now() - timedelta(days=14)).strftime("%Y-%m-%d")

    url = (
        "https://efts.sec.gov/LATEST/search"
        f'?q=%22real+estate%22&dateRange=custom&startdt={start}&enddt={today}'
        "&forms=D,8-K,S-11"
    )

    async with httpx.AsyncClient(
        headers={
            "User-Agent": "TheFindBrief/1.0 (contact@thefindcapital.com)",
            "Accept": "application/json",
        },
        timeout=_TIMEOUT,
    ) as client:
        try:
            resp = await client.get(url)
            resp.raise_for_status()
            data = resp.json()
        except Exception:
            logger.exception("EDGAR request failed")
            return []

    # Log top-level keys for debugging
    logger.info("EDGAR response keys: %s", list(data.keys()))

    articles: list[dict] = []

    # Try known structures defensively
    filings = None

    if "hits" in data:
        hits = data["hits"]
        if isinstance(hits, dict) and "hits" in hits:
            filings = hits["hits"]
        elif isinstance(hits, list):
            filings = hits
    elif "filings" in data:
        filings = data["filings"]
    elif "results" in data:
        filings = data["results"]
    elif "data" in data:
        filings = data["data"]

    if filings is None:
        logger.warning(
            "EDGAR: unrecognized response structure (keys: %s) — returning empty",
            list(data.keys()),
        )
        return []

    for filing in filings:
        if isinstance(filing, dict):
            # Try _source (Elasticsearch-style) or direct keys
            source_data = filing.get("_source", filing)
            display_names = source_data.get("display_names")
            title = (
                display_names[0]
                if isinstance(display_names, list) and display_names
                else source_data.get("entity_name")
                or source_data.get("display_name")
                or source_data.get("title")
                or source_data.get("file_description")
                or "SEC Filing"
            )
            form_type = source_data.get("form_type", source_data.get("forms", ""))
            file_date = source_data.get("file_date", source_data.get("date_filed", ""))
            file_num = source_data.get("file_num", "")

            # Build URL
            filing_id = filing.get("_id", "")
            if filing_id:
                filing_url = f"https://www.sec.gov/cgi-bin/browse-edgar?action=getcompany&filenum={filing_id}&type=&dateb=&owner=include&count=10"
            else:
                filing_url = "https://www.sec.gov/cgi-bin/browse-edgar"

            display_title = f"{form_type}: {title}" if form_type else str(title)
            if file_date:
                display_title += f" ({file_date})"

            articles.append(
                {
                    "edition_id": edition_id,
                    "title": display_title[:200],
                    "url": filing_url,
                    "source": "edgar",
                    "source_tier": 1,
                    "relevance_category": "regulatory",
                    "raw_snippet": f"Form {form_type} filed {file_date}. File number: {file_num}."[:2000],
                }
            )

    logger.info("EDGAR: %d filings found", len(articles))
    return articles


# ============================= FRED ========================================


async def _fred_single_series(
    client: httpx.AsyncClient,
    series_id: str,
    label: str,
    edition_id: int,
) -> dict | None:
    """Fetch the latest observation for one FRED series."""
    resp = await client.get(
        "https://api.stlouisfed.org/fred/series/observations",
        params={
            "series_id": series_id,
            "api_key": settings.fred_api_key,
            "file_type": "json",
            "sort_order": "desc",
            "limit": 1,
        },
    )
    resp.raise_for_status()
    data = resp.json()

    observations = data.get("observations", [])
    if not observations:
        return None

    obs = observations[0]
    value = obs.get("value", "N/A")
    date = obs.get("date", "unknown")

    return {
        "edition_id": edition_id,
        "title": f"{label}: {value}% ({date})" if series_id != "CPIAUCSL" else f"{label}: {value} ({date})",
        "url": f"https://fred.stlouisfed.org/series/{series_id}",
        "source": "fred",
        "source_tier": 1,
        "relevance_category": "macro",
        "raw_snippet": f"{label} ({series_id}): {value} as of {date}. Source: Federal Reserve Economic Data.",
    }


async def _fetch_fred(edition_id: int) -> list[dict]:
    """Fetch latest macro data points from FRED."""
    if not settings.fred_api_key:
        logger.warning("FRED API key not set — skipping")
        return []

    articles: list[dict] = []
    async with httpx.AsyncClient(timeout=_TIMEOUT) as client:
        tasks = [
            _fred_single_series(client, series_id, label, edition_id)
            for series_id, label in _FRED_SERIES
        ]
        results = await asyncio.gather(*tasks, return_exceptions=True)

        for i, result in enumerate(results):
            if isinstance(result, Exception):
                series_id = _FRED_SERIES[i][0]
                logger.error("FRED series %s failed: %s", series_id, result)
                continue
            if result is not None:
                articles.append(result)

    logger.info("FRED: %d data points retrieved", len(articles))
    return articles
