import asyncio
import html as html_lib
import json
import logging
import re
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import Depends, FastAPI, Form, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates

from app.auth import AuthRequired, authenticate, create_session, get_current_user
from app.database import get_db, init_db
from app.pipeline.orchestrator import run_pipeline
from app.pipeline.prompts import DISCLAIMER_TEXTS, SECTION_DISPLAY_NAMES

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

_BASE_DIR = Path(__file__).resolve().parent


@asynccontextmanager
async def lifespan(app: FastAPI):
    await init_db()
    logger.info("The Find Brief started")
    yield


app = FastAPI(title="The Find Brief", lifespan=lifespan)

app.mount("/static", StaticFiles(directory=_BASE_DIR / "static"), name="static")

templates = Jinja2Templates(directory=_BASE_DIR / "templates")


@app.exception_handler(AuthRequired)
async def auth_required_handler(request: Request, exc: AuthRequired):
    return RedirectResponse("/login", status_code=303)


@app.get("/login")
async def login_page(request: Request):
    return templates.TemplateResponse(request, "login.html", {"user": None})


@app.post("/login")
async def login_submit(request: Request, username: str = Form(), password: str = Form()):
    user = authenticate(username, password)
    if not user:
        return templates.TemplateResponse(
            request,
            "login.html",
            {"user": None, "error": "Invalid username or password"},
            status_code=401,
        )
    response = RedirectResponse("/", status_code=303)
    is_secure = request.url.scheme == "https"
    response.set_cookie(
        "session",
        create_session(user["username"], user["display_name"]),
        httponly=True,
        secure=is_secure,
        samesite="lax",
        max_age=86400,
    )
    return response


@app.get("/logout")
async def logout():
    response = RedirectResponse("/login", status_code=303)
    response.delete_cookie("session")
    return response


@app.get("/")
async def dashboard(request: Request, user: dict = Depends(get_current_user)):
    db = await get_db()
    try:
        cursor = await db.execute(
            "SELECT id, status, created_at, approved_by, approved_at, generation_mode, editorial_brief FROM editions ORDER BY created_at DESC"
        )
        editions = await cursor.fetchall()
    finally:
        await db.close()
    return templates.TemplateResponse(
        request, "dashboard.html", {"user": user, "editions": editions}
    )


# ---- Pipeline API routes ----


@app.post("/api/pipeline/run")
async def pipeline_run(
    request: Request,
    user: dict = Depends(get_current_user),
    mode: str = Form(default="auto"),
    editorial_brief: str = Form(default=""),
):
    # Sanitize: only keep brief in guided mode, strip whitespace
    editorial_brief = editorial_brief.strip() if mode == "guided" else None
    if not editorial_brief:
        editorial_brief = None
        mode = "auto"

    db = await get_db()
    try:
        # Concurrent pipeline guard
        cursor = await db.execute(
            "SELECT id FROM editions WHERE status = 'generating' LIMIT 1"
        )
        running = await cursor.fetchone()
        if running:
            return templates.TemplateResponse(
                request,
                "partials/pipeline_status.html",
                {"error_message": "A pipeline is already running"},
            )

        # Create new edition
        cursor = await db.execute(
            "INSERT INTO editions (status, pipeline_stage, pipeline_progress, generation_mode, editorial_brief) "
            "VALUES ('generating', 'starting', 0, ?, ?)",
            (mode, editorial_brief),
        )
        await db.commit()
        edition_id = cursor.lastrowid

        # Log to audit
        await db.execute(
            "INSERT INTO audit_log (edition_id, actor, action) VALUES (?, ?, 'pipeline_started')",
            (edition_id, user["username"]),
        )
        await db.commit()
    finally:
        await db.close()

    # Fire off pipeline as background task
    asyncio.create_task(run_pipeline(edition_id, editorial_brief=editorial_brief))

    return templates.TemplateResponse(
        request,
        "partials/pipeline_status.html",
        {
            "edition_id": edition_id,
            "pipeline_stage": "starting",
            "pipeline_progress": 0,
            "status": "generating",
            "article_count": 0,
        },
    )


@app.get("/api/pipeline/status/{edition_id}")
async def pipeline_status(
    request: Request, edition_id: int, user: dict = Depends(get_current_user)
):
    db = await get_db()
    try:
        cursor = await db.execute(
            "SELECT status, pipeline_stage, pipeline_progress FROM editions WHERE id = ?",
            (edition_id,),
        )
        edition = await cursor.fetchone()

        cursor = await db.execute(
            "SELECT COUNT(*) as cnt FROM articles WHERE edition_id = ?",
            (edition_id,),
        )
        row = await cursor.fetchone()
        article_count = row["cnt"] if row else 0
    finally:
        await db.close()

    if not edition:
        return templates.TemplateResponse(
            request,
            "partials/pipeline_status.html",
            {"error_message": "Edition not found"},
        )

    return templates.TemplateResponse(
        request,
        "partials/pipeline_status.html",
        {
            "edition_id": edition_id,
            "pipeline_stage": edition["pipeline_stage"],
            "pipeline_progress": edition["pipeline_progress"],
            "status": edition["status"],
            "article_count": article_count,
        },
    )


@app.get("/sources/{edition_id}")
async def sources_page(
    request: Request, edition_id: int, user: dict = Depends(get_current_user)
):
    db = await get_db()
    try:
        cursor = await db.execute(
            "SELECT id, status, created_at, approved_by FROM editions WHERE id = ?",
            (edition_id,),
        )
        edition = await cursor.fetchone()

        cursor = await db.execute(
            "SELECT * FROM articles WHERE edition_id = ? ORDER BY source, quality_score DESC",
            (edition_id,),
        )
        articles = await cursor.fetchall()
    finally:
        await db.close()

    if not edition:
        return RedirectResponse("/", status_code=303)

    # Group articles by source
    sources: dict[str, list] = {}
    for article in articles:
        src = article["source"]
        if src not in sources:
            sources[src] = []
        sources[src].append(article)

    # Compute summary stats for the stats bar
    stats = {
        "total": len(articles),
        "tier_1": sum(1 for a in articles if a["source_tier"] == 1),
        "tier_2": sum(1 for a in articles if a["source_tier"] == 2),
        "tier_3": sum(1 for a in articles if a["source_tier"] == 3),
        "paywalled": sum(1 for a in articles if a["is_paywalled"]),
        "duplicates": sum(1 for a in articles if a["is_duplicate"]),
        "avg_score": round(
            sum(a["quality_score"] for a in articles) / len(articles), 2
        ) if articles else 0.0,
    }

    return templates.TemplateResponse(
        request,
        "sources.html",
        {
            "user": user,
            "edition": edition,
            "sources": sources,
            "total_count": len(articles),
            "stats": stats,
        },
    )


def _annotate_content(content: str, flags: list[dict]) -> str:
    """HTML-escape content and insert compliance highlight spans.

    Escapes the full content first, then inserts trusted <span> tags
    for each flag. Tracks annotated ranges to prevent overlapping highlights.
    """
    escaped = html_lib.escape(content)

    # Sort flags by matched_text length descending (longest first)
    sorted_flags = sorted(flags, key=lambda f: len(f.get("matched_text", "")), reverse=True)

    annotated_ranges: list[list[int]] = []

    for flag in sorted_flags:
        matched_text = flag.get("matched_text", "")
        if not matched_text:
            continue

        # HTML-escape the matched text so it matches the escaped content
        escaped_match = html_lib.escape(matched_text)
        severity_class = flag["severity"].lower().replace("_", "-")
        flag_id = flag["id"]

        match = re.search(re.escape(escaped_match), escaped)
        if not match:
            continue

        start, end = match.start(), match.end()

        # Check for overlap with existing annotations
        overlaps = False
        for r in annotated_ranges:
            if start < r[1] and end > r[0]:
                overlaps = True
                break

        if overlaps:
            logger.debug(
                "Flag %d skipped inline highlight (overlaps existing annotation)",
                flag_id,
            )
            continue

        replacement = (
            f'<span class="compliance-highlight compliance-highlight--{severity_class}" '
            f'data-flag-id="{flag_id}">'
            f'{escaped_match}'
            f'<span class="compliance-indicator compliance-indicator--{severity_class}" '
            f'data-flag-id="{flag_id}"></span>'
            f'</span>'
        )

        # Replace this occurrence
        escaped = escaped[:start] + replacement + escaped[end:]

        # Track the annotated range (adjusted for inserted HTML)
        new_end = start + len(replacement)
        annotated_ranges.append([start, new_end])

        # Shift all existing ranges that come after this insertion point
        shift = len(replacement) - (end - start)
        for r in annotated_ranges[:-1]:
            if r[0] >= end:
                r[0] += shift
                r[1] += shift

    return escaped


def _compute_disclaimers(
    flag_types: set[str], article_categories: set[str]
) -> list[dict]:
    """Determine which disclaimers to include based on flags and article categories."""
    disclaimers: list[dict] = [
        {"name": "GENERAL", "text": DISCLAIMER_TEXTS["GENERAL"]}
    ]

    if "forward_looking" in flag_types:
        disclaimers.append(
            {"name": "FORWARD_LOOKING", "text": DISCLAIMER_TEXTS["FORWARD_LOOKING"]}
        )

    if "performance_claim" in flag_types:
        disclaimers.append(
            {"name": "PERFORMANCE", "text": DISCLAIMER_TEXTS["PERFORMANCE"]}
        )

    if "regional" in article_categories:
        disclaimers.append(
            {"name": "CROSS_BORDER", "text": DISCLAIMER_TEXTS["CROSS_BORDER"]}
        )

    if "deals" in article_categories:
        disclaimers.append(
            {"name": "PRIVATE_PLACEMENT", "text": DISCLAIMER_TEXTS["PRIVATE_PLACEMENT"]}
        )

    return disclaimers


@app.get("/draft/{edition_id}")
async def draft_page(
    request: Request, edition_id: int, user: dict = Depends(get_current_user)
):
    db = await get_db()
    try:
        cursor = await db.execute(
            "SELECT id, status, created_at, approved_by FROM editions WHERE id = ?",
            (edition_id,),
        )
        edition = await cursor.fetchone()

        cursor = await db.execute(
            "SELECT id, section_name, content, word_count, model_used, generated_at "
            "FROM section_drafts WHERE edition_id = ? ORDER BY id",
            (edition_id,),
        )
        sections = await cursor.fetchall()

        # Fetch compliance flags for all sections in this edition
        section_ids = [s["id"] for s in sections]
        flags_by_section: dict[int, list[dict]] = {sid: [] for sid in section_ids}

        if section_ids:
            placeholders = ",".join("?" * len(section_ids))
            cursor = await db.execute(
                "SELECT id, section_draft_id, severity, flag_type, matched_text, "
                "rule_reference, explanation, recommended_action, is_resolved, "
                "pass_number FROM compliance_flags "
                f"WHERE section_draft_id IN ({placeholders})",
                section_ids,
            )
            all_flags = [dict(row) for row in await cursor.fetchall()]
            for f in all_flags:
                flags_by_section[f["section_draft_id"]].append(f)

        # Fetch article categories for disclaimer computation
        cursor = await db.execute(
            "SELECT DISTINCT relevance_category FROM articles "
            "WHERE edition_id = ? AND is_duplicate = 0 AND relevance_category IS NOT NULL",
            (edition_id,),
        )
        article_categories = {row["relevance_category"] for row in await cursor.fetchall()}
    finally:
        await db.close()

    if not edition:
        return RedirectResponse("/", status_code=303)

    # Convert Row objects to dicts and attach compliance data
    sections_list = [dict(s) for s in sections]
    all_flag_types: set[str] = set()
    flag_counts: dict[str, int] = {
        "BLOCK": 0, "MANDATORY_REVIEW": 0, "WARNING": 0, "ADD_DISCLAIMER": 0
    }
    total_flags = 0

    for section in sections_list:
        section_flags = flags_by_section.get(section["id"], [])
        section["flags"] = section_flags

        if section_flags and section["content"]:
            section["annotated_content"] = _annotate_content(
                section["content"], section_flags
            )
        else:
            section["annotated_content"] = None

        for f in section_flags:
            if not f.get("is_resolved"):
                flag_counts[f["severity"]] = flag_counts.get(f["severity"], 0) + 1
                total_flags += 1
                all_flag_types.add(f.get("flag_type", ""))

    has_unresolved_blocks = flag_counts.get("BLOCK", 0) > 0
    disclaimers = _compute_disclaimers(all_flag_types, article_categories)

    return templates.TemplateResponse(
        request,
        "draft.html",
        {
            "user": user,
            "edition": edition,
            "sections": sections_list,
            "display_names": SECTION_DISPLAY_NAMES,
            "flag_counts": flag_counts,
            "total_flags": total_flags,
            "has_unresolved_blocks": has_unresolved_blocks,
            "disclaimers": disclaimers,
        },
    )


# ---- Review & Approval routes ----


@app.get("/review/{edition_id}")
async def review_page(
    request: Request, edition_id: int, user: dict = Depends(get_current_user)
):
    db = await get_db()
    try:
        cursor = await db.execute(
            "SELECT id, status, created_at, approved_by, approved_at FROM editions WHERE id = ?",
            (edition_id,),
        )
        edition = await cursor.fetchone()

        cursor = await db.execute(
            "SELECT id, section_name, content, word_count, model_used, generated_at "
            "FROM section_drafts WHERE edition_id = ? ORDER BY id",
            (edition_id,),
        )
        sections = await cursor.fetchall()

        # Fetch compliance flags for all sections
        section_ids = [s["id"] for s in sections]
        flags_by_section: dict[int, list[dict]] = {sid: [] for sid in section_ids}

        if section_ids:
            placeholders = ",".join("?" * len(section_ids))
            cursor = await db.execute(
                "SELECT id, section_draft_id, severity, flag_type, matched_text, "
                "rule_reference, explanation, recommended_action, is_resolved, "
                "resolved_by, resolved_at, resolution_note, pass_number "
                "FROM compliance_flags "
                f"WHERE section_draft_id IN ({placeholders})",
                section_ids,
            )
            all_flags = [dict(row) for row in await cursor.fetchall()]
            for f in all_flags:
                flags_by_section[f["section_draft_id"]].append(f)

        # Fetch article categories for disclaimer computation
        cursor = await db.execute(
            "SELECT DISTINCT relevance_category FROM articles "
            "WHERE edition_id = ? AND is_duplicate = 0 AND relevance_category IS NOT NULL",
            (edition_id,),
        )
        article_categories = {row["relevance_category"] for row in await cursor.fetchall()}
    finally:
        await db.close()

    if not edition:
        return RedirectResponse("/", status_code=303)

    if edition["status"] == "generating":
        return RedirectResponse(f"/draft/{edition_id}", status_code=303)

    # Convert Row objects to dicts and attach compliance data
    sections_list = [dict(s) for s in sections]
    all_flag_types: set[str] = set()
    flag_counts: dict[str, int] = {
        "BLOCK": 0, "MANDATORY_REVIEW": 0, "WARNING": 0, "ADD_DISCLAIMER": 0
    }
    total_flags = 0
    blocking_count = 0

    for section in sections_list:
        section_flags = flags_by_section.get(section["id"], [])
        section["flags"] = section_flags

        if section_flags and section["content"]:
            section["annotated_content"] = _annotate_content(
                section["content"], section_flags
            )
        else:
            section["annotated_content"] = None

        for f in section_flags:
            if not f.get("is_resolved"):
                flag_counts[f["severity"]] = flag_counts.get(f["severity"], 0) + 1
                total_flags += 1
                all_flag_types.add(f.get("flag_type", ""))
                if f["severity"] in ("BLOCK", "MANDATORY_REVIEW"):
                    blocking_count += 1

    can_approve = (
        blocking_count == 0
        and edition["status"] == "reviewing"
    )
    disclaimers = _compute_disclaimers(all_flag_types, article_categories)

    return templates.TemplateResponse(
        request,
        "review.html",
        {
            "user": user,
            "edition": edition,
            "sections": sections_list,
            "display_names": SECTION_DISPLAY_NAMES,
            "flag_counts": flag_counts,
            "total_flags": total_flags,
            "disclaimers": disclaimers,
            "can_approve": can_approve,
            "blocking_count": blocking_count,
            "edition_id": edition_id,
            "edition_status": edition["status"],
            "approved_by": edition["approved_by"],
        },
    )


@app.post("/api/flags/{flag_id}/resolve")
async def resolve_flag(
    request: Request,
    flag_id: int,
    user: dict = Depends(get_current_user),
    resolution_note: str = Form(default=""),
):
    db = await get_db()
    try:
        # Update the flag
        await db.execute(
            "UPDATE compliance_flags SET is_resolved = 1, resolved_by = ?, "
            "resolved_at = CURRENT_TIMESTAMP, resolution_note = ? WHERE id = ?",
            (user["username"], resolution_note, flag_id),
        )
        await db.commit()

        # Fetch the updated flag
        cursor = await db.execute(
            "SELECT id, section_draft_id, severity, flag_type, matched_text, "
            "rule_reference, explanation, recommended_action, is_resolved, "
            "resolved_by, resolved_at, resolution_note, pass_number "
            "FROM compliance_flags WHERE id = ?",
            (flag_id,),
        )
        flag_row = await cursor.fetchone()

        if not flag_row:
            return HTMLResponse("<p>Flag not found</p>", status_code=404)

        flag_dict = dict(flag_row)

        # Find the edition_id via section_drafts
        cursor = await db.execute(
            "SELECT sd.edition_id FROM section_drafts sd "
            "JOIN compliance_flags cf ON cf.section_draft_id = sd.id "
            "WHERE cf.id = ?",
            (flag_id,),
        )
        edition_row = await cursor.fetchone()
        if not edition_row:
            return HTMLResponse("<p>Edition not found</p>", status_code=404)
        edition_id = edition_row["edition_id"]

        # Audit log
        await db.execute(
            "INSERT INTO audit_log (edition_id, actor, action, details) "
            "VALUES (?, ?, 'flag_resolved', ?)",
            (
                edition_id,
                user["username"],
                json.dumps({
                    "flag_id": flag_id,
                    "severity": flag_dict["severity"],
                    "flag_type": flag_dict["flag_type"],
                }),
            ),
        )
        await db.commit()

        # Recompute blocking count for approve button
        cursor = await db.execute(
            "SELECT COUNT(*) as cnt FROM compliance_flags cf "
            "JOIN section_drafts sd ON cf.section_draft_id = sd.id "
            "WHERE sd.edition_id = ? AND cf.is_resolved = 0 "
            "AND cf.severity IN ('BLOCK', 'MANDATORY_REVIEW')",
            (edition_id,),
        )
        row = await cursor.fetchone()
        blocking_count = row["cnt"] if row else 0

        # Fetch edition status
        cursor = await db.execute(
            "SELECT status, approved_by FROM editions WHERE id = ?",
            (edition_id,),
        )
        edition = await cursor.fetchone()
    finally:
        await db.close()

    can_approve = blocking_count == 0 and edition["status"] == "reviewing"

    # Render flag card + OOB approve button
    flag_html = templates.get_template("partials/flag_card.html").render(
        flag=flag_dict
    )
    approve_html = templates.get_template("partials/approve_button.html").render(
        can_approve=can_approve,
        edition_id=edition_id,
        blocking_count=blocking_count,
        edition_status=edition["status"],
        approved_by=edition["approved_by"],
    )
    combined = (
        flag_html
        + f'\n<div id="approve-button" hx-swap-oob="innerHTML">{approve_html}</div>'
    )
    return HTMLResponse(combined)


@app.post("/api/edition/{edition_id}/approve")
async def approve_edition(
    request: Request,
    edition_id: int,
    user: dict = Depends(get_current_user),
):
    db = await get_db()
    try:
        # Server-side guard: recheck blocking flags
        cursor = await db.execute(
            "SELECT COUNT(*) as cnt FROM compliance_flags cf "
            "JOIN section_drafts sd ON cf.section_draft_id = sd.id "
            "WHERE sd.edition_id = ? AND cf.is_resolved = 0 "
            "AND cf.severity IN ('BLOCK', 'MANDATORY_REVIEW')",
            (edition_id,),
        )
        row = await cursor.fetchone()
        blocking_count = row["cnt"] if row else 0

        if blocking_count > 0:
            approve_html = templates.get_template(
                "partials/approve_button.html"
            ).render(
                can_approve=False,
                edition_id=edition_id,
                blocking_count=blocking_count,
                edition_status="reviewing",
                approved_by=None,
            )
            return HTMLResponse(approve_html)

        # Approve the edition
        await db.execute(
            "UPDATE editions SET status = 'approved', approved_by = ?, "
            "approved_at = CURRENT_TIMESTAMP WHERE id = ?",
            (user["username"], edition_id),
        )
        await db.commit()

        # Audit log
        await db.execute(
            "INSERT INTO audit_log (edition_id, actor, action) "
            "VALUES (?, ?, 'edition_approved')",
            (edition_id, user["username"]),
        )
        await db.commit()
    finally:
        await db.close()

    response = HTMLResponse(
        templates.get_template("partials/approve_button.html").render(
            can_approve=False,
            edition_id=edition_id,
            blocking_count=0,
            edition_status="approved",
            approved_by=user["display_name"],
        )
    )
    response.headers["HX-Redirect"] = "/"
    return response
