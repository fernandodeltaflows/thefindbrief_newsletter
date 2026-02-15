import json
import logging

from app.database import get_db
from app.pipeline.retrieval import run_retrieval
from app.pipeline.drafting import run_drafting
from app.pipeline.verification import run_verification
from app.pipeline.compliance import run_compliance

logger = logging.getLogger(__name__)


_ALLOWED_EDITION_COLUMNS = {
    "status", "pipeline_stage", "pipeline_progress", "approved_by", "approved_at",
}


async def _update_edition(edition_id: int, **fields: object) -> None:
    """Update edition fields in the database."""
    invalid = set(fields) - _ALLOWED_EDITION_COLUMNS
    if invalid:
        raise ValueError(f"Invalid column(s): {invalid}")
    set_clause = ", ".join(f"{k} = ?" for k in fields)
    values = list(fields.values())
    values.append(edition_id)
    db = await get_db()
    try:
        await db.execute(
            f"UPDATE editions SET {set_clause} WHERE id = ?", values
        )
        await db.commit()
    finally:
        await db.close()


async def _log_audit(
    edition_id: int, action: str, details: str | None = None
) -> None:
    """Insert a row into the audit_log table."""
    db = await get_db()
    try:
        await db.execute(
            "INSERT INTO audit_log (edition_id, actor, action, details) VALUES (?, 'system', ?, ?)",
            (edition_id, action, details),
        )
        await db.commit()
    finally:
        await db.close()


async def run_pipeline(edition_id: int) -> None:
    """Run the full pipeline for an edition. Called as a background task."""
    try:
        # Layer 1 — Retrieval
        await _update_edition(
            edition_id, pipeline_stage="retrieval", pipeline_progress=10
        )
        await _log_audit(edition_id, "pipeline_started")

        article_count = await run_retrieval(edition_id)
        await _log_audit(
            edition_id,
            "retrieval_completed",
            json.dumps({"article_count": article_count}),
        )

        # Layer 2 — Verification
        await _update_edition(
            edition_id, pipeline_stage="verification", pipeline_progress=30
        )
        await run_verification(edition_id)
        await _log_audit(edition_id, "verification_completed")
        await _update_edition(edition_id, pipeline_progress=50)

        # Layer 3 — Drafting
        await _update_edition(
            edition_id, pipeline_stage="drafting", pipeline_progress=55
        )
        await run_drafting(edition_id)
        await _log_audit(edition_id, "drafting_completed")
        await _update_edition(edition_id, pipeline_progress=70)

        # Layer 4 — Compliance
        await _update_edition(
            edition_id, pipeline_stage="compliance", pipeline_progress=70
        )
        await run_compliance(edition_id)
        await _log_audit(edition_id, "compliance_completed")
        await _update_edition(edition_id, pipeline_progress=90)

        # Layer 5 — Ready for review
        await _update_edition(
            edition_id, pipeline_stage="review", pipeline_progress=90
        )
        await _log_audit(edition_id, "ready_for_review")

        # Complete
        await _update_edition(
            edition_id,
            status="reviewing",
            pipeline_stage="complete",
            pipeline_progress=100,
        )
        await _log_audit(edition_id, "pipeline_completed")
        logger.info("Edition %d: Pipeline completed", edition_id)

    except Exception:
        logger.exception("Edition %d: Pipeline failed", edition_id)
        try:
            await _update_edition(edition_id, status="error", pipeline_stage="error")
            await _log_audit(edition_id, "pipeline_failed")
        except Exception:
            logger.exception(
                "Edition %d: Failed to update edition status after error", edition_id
            )
