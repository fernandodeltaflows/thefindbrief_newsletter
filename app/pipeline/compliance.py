import asyncio
import json
import logging
import re
import time
from pathlib import Path

import google.generativeai as genai

from app.config import settings
from app.database import get_db
from app.pipeline.gemini_utils import call_with_retry
from app.pipeline.prompts import (
    COMPLIANCE_SYSTEM_PROMPT,
    COMPLIANCE_USER_TEMPLATE,
)

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Regex patterns for Pass 1
# Each entry: name (flag_type), compiled pattern, severity, rule, explanation,
# recommended_action.
# ---------------------------------------------------------------------------

_VALID_SEVERITIES = {"BLOCK", "MANDATORY_REVIEW", "WARNING", "ADD_DISCLAIMER"}

REGEX_PATTERNS: list[dict] = [
    {
        "name": "guarantee_language",
        "pattern": re.compile(
            r"\b(guaranteed|risk[- ]free(?!\s+(?:rate|returns?|yield|benchmark))|no\s+risk|certain\s+to|cannot\s+lose)\b",
            re.IGNORECASE,
        ),
        "severity": "BLOCK",
        "rule_reference": "2210(d)(1)(B)",
        "explanation": "Guarantee or risk-elimination language is prohibited in broker-dealer communications.",
        "recommended_action": "Remove guarantee language entirely. Reframe with appropriate risk disclosure.",
    },
    {
        "name": "mnpi_risk",
        "pattern": re.compile(
            r"\b(insider\s+information|confidential\s+information|non[- ]public\s+information|before\s+announcement)\b",
            re.IGNORECASE,
        ),
        "severity": "BLOCK",
        "rule_reference": "2210(d)(1)(B)",
        "explanation": "Content that references or implies use of material non-public information.",
        "recommended_action": "Remove any reference to non-public or insider information. Ensure all data is from public sources.",
    },
    {
        "name": "superlative_claim",
        "pattern": re.compile(
            r"\b(best\s+fund|top\s+manager|leading\s+performer|#1\s+fund|number\s+one\s+fund)\b",
            re.IGNORECASE,
        ),
        "severity": "BLOCK",
        "rule_reference": "2210(d)(1)(B)",
        "explanation": "Superlative claims about fund performance or manager rankings are misleading without substantiation.",
        "recommended_action": "Remove superlative. If ranking is sourced, cite the methodology and time period.",
    },
    {
        "name": "performance_claim",
        "pattern": re.compile(
            r"\b(\d+\s*%\s*(return|yield|IRR|annualized|net|gross)|(IRR|yield|return)\s+of\s+\d+|outperform(ed|s|ing)?|beat(s|ing)?\s+(the\s+)?benchmark)\b",
            re.IGNORECASE,
        ),
        "severity": "MANDATORY_REVIEW",
        "rule_reference": "2210(d)(1)(F)",
        "explanation": "Specific performance figures or claims of outperformance require careful review for fair presentation.",
        "recommended_action": "Verify source attribution. Add context about time period, methodology, and that past performance does not guarantee future results.",
    },
    {
        "name": "solicitation",
        "pattern": re.compile(
            r"\b(contact\s+us\s+to\s+invest|invest\s+with\s+us|schedule\s+a\s+call|get\s+in\s+touch\s+to\s+(invest|learn|discuss))\b",
            re.IGNORECASE,
        ),
        "severity": "WARNING",
        "rule_reference": "2210(d)(1)(A), Reg D 506(b)",
        "explanation": "Direct solicitation language may violate general solicitation restrictions for private placements.",
        "recommended_action": "Remove solicitation language. Newsletter should inform, not solicit.",
    },
    {
        "name": "tax_claim",
        "pattern": re.compile(
            r"\b(tax[- ]free\s+investment|no\s+tax\s+implications|tax\s+exempt\s+investment|avoid(s|ing)?\s+(all\s+)?tax(es|ation)?)\b",
            re.IGNORECASE,
        ),
        "severity": "WARNING",
        "rule_reference": "2210(d)(4)",
        "explanation": "Tax benefit claims must be qualified and cannot overstate the tax advantages of an investment.",
        "recommended_action": "Qualify tax references. Add disclaimer that tax treatment depends on individual circumstances.",
    },
    {
        "name": "forward_looking",
        "pattern": re.compile(
            r"\b(we\s+expect|we\s+forecast|we\s+anticipate|will\s+likely|projected\s+to|poised\s+to)\b",
            re.IGNORECASE,
        ),
        "severity": "ADD_DISCLAIMER",
        "rule_reference": "2210(d)(1)(F)",
        "explanation": "Forward-looking statements should be identified as such and accompanied by appropriate disclaimers.",
        "recommended_action": "Add forward-looking statement disclaimer. Consider qualifying with 'based on current expectations' or similar.",
    },
]


# ============================= PUBLIC API ==================================


async def run_compliance(edition_id: int) -> None:
    """Run two-pass compliance scan on all section drafts for an edition.

    Pass 1: Regex pattern matching (mechanical checks).
    Pass 2: Gemini holistic review against the full regulatory framework.
    """
    db = await get_db()
    try:
        cursor = await db.execute(
            "SELECT id, section_name, content FROM section_drafts WHERE edition_id = ?",
            (edition_id,),
        )
        drafts = [dict(row) for row in await cursor.fetchall()]
    finally:
        await db.close()

    if not drafts:
        logger.warning("Edition %d: no section drafts to scan", edition_id)
        return

    logger.info(
        "Edition %d: running compliance scan on %d sections (model=%s)",
        edition_id, len(drafts), "gemini-2.5-flash",
    )

    start_time = time.monotonic()
    pass_1_total = 0
    pass_2_total = 0

    # --- Pass 1: Regex ---
    for draft in drafts:
        if draft["section_name"] == "perspective":
            continue
        try:
            flags = _run_pass_1(draft["id"], draft["content"])
            if flags:
                await _store_flags(flags)
                pass_1_total += len(flags)
        except Exception:
            logger.exception(
                "Edition %d [%s]: Pass 1 failed",
                edition_id, draft["section_name"],
            )

    logger.info(
        "Edition %d: Pass 1 complete — %d regex flags", edition_id, pass_1_total
    )

    # --- Pass 2: Gemini ---
    if not settings.gemini_api_key:
        logger.warning("Gemini API key not set — skipping compliance Pass 2")
    else:
        genai.configure(api_key=settings.gemini_api_key)
        framework_text = _load_compliance_framework()
        filled_system_prompt = COMPLIANCE_SYSTEM_PROMPT.format(
            compliance_framework=framework_text
        )

        model = genai.GenerativeModel(
            model_name="gemini-2.5-flash",
            system_instruction=filled_system_prompt,
            generation_config=genai.GenerationConfig(
                temperature=0.3,
                max_output_tokens=8192,
            ),
        )

        gemini_call_count = 0
        for draft in drafts:
            if draft["section_name"] == "perspective":
                continue
            if gemini_call_count > 0:
                await asyncio.sleep(2)
            try:
                flags = await _run_pass_2(
                    draft["id"], draft["section_name"], draft["content"], model
                )
                if flags:
                    await _store_flags(flags)
                    pass_2_total += len(flags)
            except Exception:
                logger.exception(
                    "Edition %d [%s]: Pass 2 failed",
                    edition_id, draft["section_name"],
                )
            gemini_call_count += 1

        logger.info(
            "Edition %d: Pass 2 complete — %d Gemini flags",
            edition_id, pass_2_total,
        )

    elapsed = round(time.monotonic() - start_time, 1)
    logger.info(
        "Edition %d: compliance scan complete in %.1fs (pass_1=%d, pass_2=%d)",
        edition_id, elapsed, pass_1_total, pass_2_total,
    )


# ============================= HELPERS =====================================


def _run_pass_1(section_draft_id: int, content: str) -> list[dict]:
    """Run regex patterns against section content. Returns flag dicts."""
    flags: list[dict] = []
    for pattern_def in REGEX_PATTERNS:
        for match in pattern_def["pattern"].finditer(content):
            flags.append({
                "section_draft_id": section_draft_id,
                "severity": pattern_def["severity"],
                "flag_type": pattern_def["name"],
                "matched_text": match.group(0),
                "rule_reference": pattern_def["rule_reference"],
                "explanation": pattern_def["explanation"],
                "recommended_action": pattern_def["recommended_action"],
                "pass_number": 1,
            })
    return flags


async def _run_pass_2(
    section_draft_id: int,
    section_name: str,
    content: str,
    model: genai.GenerativeModel,
) -> list[dict]:
    """Run Gemini holistic review on a section. Returns flag dicts."""
    prompt = COMPLIANCE_USER_TEMPLATE.format(
        section_name=section_name, content=content
    )

    try:
        response = await call_with_retry(
            lambda: model.generate_content_async(prompt),
            label=f"Compliance [{section_name}]",
        )
        raw = response.text if response.text else ""
    except Exception:
        logger.exception("Gemini compliance call failed for section %s", section_name)
        return []

    if not raw.strip():
        return []

    # Strip markdown code fences if present
    cleaned = raw.strip()
    if cleaned.startswith("```"):
        # Remove opening fence (with optional language tag)
        cleaned = re.sub(r"^```\w*\n?", "", cleaned)
    if cleaned.endswith("```"):
        cleaned = cleaned[:-3]
    cleaned = cleaned.strip()

    try:
        parsed = json.loads(cleaned)
    except json.JSONDecodeError:
        logger.warning(
            "Section %s: failed to parse Gemini compliance JSON: %.200s",
            section_name, cleaned,
        )
        return []

    raw_flags = parsed.get("flags", [])
    if not isinstance(raw_flags, list):
        logger.warning("Section %s: 'flags' is not a list", section_name)
        return []

    flags: list[dict] = []
    for f in raw_flags:
        severity = f.get("severity", "")
        if severity not in _VALID_SEVERITIES:
            logger.warning(
                "Section %s: invalid severity '%s', skipping flag", section_name, severity
            )
            continue
        flags.append({
            "section_draft_id": section_draft_id,
            "severity": severity,
            "flag_type": f.get("flag_type", "general"),
            "matched_text": f.get("matched_text", ""),
            "rule_reference": f.get("rule_reference", ""),
            "explanation": f.get("explanation", ""),
            "recommended_action": f.get("recommended_action", ""),
            "pass_number": 2,
        })

    return flags


async def _store_flags(flags: list[dict]) -> None:
    """Insert compliance flags into the database."""
    db = await get_db()
    try:
        await db.executemany(
            "INSERT INTO compliance_flags "
            "(section_draft_id, severity, flag_type, matched_text, "
            "rule_reference, explanation, recommended_action, pass_number) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            [
                (
                    f["section_draft_id"],
                    f["severity"],
                    f["flag_type"],
                    f["matched_text"],
                    f["rule_reference"],
                    f["explanation"],
                    f["recommended_action"],
                    f["pass_number"],
                )
                for f in flags
            ],
        )
        await db.commit()
    finally:
        await db.close()


def _load_compliance_framework() -> str:
    """Load the compliance framework markdown file."""
    path = (
        Path(__file__).resolve().parent.parent
        / "compliance"
        / "compliance_framework.md"
    )
    try:
        return path.read_text(encoding="utf-8")
    except FileNotFoundError:
        logger.warning("Compliance framework file not found at %s", path)
        return ""
