import logging
import os

import aiosqlite

from app.config import settings

logger = logging.getLogger(__name__)

_db_path: str = settings.database_path


async def get_db() -> aiosqlite.Connection:
    """Open a connection to the SQLite database."""
    db = await aiosqlite.connect(_db_path)
    db.row_factory = aiosqlite.Row
    await db.execute("PRAGMA journal_mode=WAL")
    await db.execute("PRAGMA foreign_keys=ON")
    return db


async def init_db() -> None:
    """Create all tables if they don't exist. Called on app startup."""
    os.makedirs(os.path.dirname(_db_path) or ".", exist_ok=True)

    db = await get_db()
    try:
        await db.executescript(
            """
            CREATE TABLE IF NOT EXISTS editions (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                status TEXT DEFAULT 'draft',
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                approved_by TEXT,
                approved_at TIMESTAMP,
                pipeline_stage TEXT,
                pipeline_progress INTEGER DEFAULT 0,
                generation_mode TEXT DEFAULT 'auto',
                editorial_brief TEXT
            );

            CREATE TABLE IF NOT EXISTS articles (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                edition_id INTEGER NOT NULL,
                title TEXT NOT NULL,
                url TEXT,
                source TEXT,
                source_tier INTEGER DEFAULT 3,
                quality_score REAL DEFAULT 0.0,
                relevance_category TEXT,
                is_paywalled BOOLEAN DEFAULT 0,
                is_duplicate BOOLEAN DEFAULT 0,
                raw_snippet TEXT,
                retrieved_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                FOREIGN KEY (edition_id) REFERENCES editions(id)
            );

            CREATE TABLE IF NOT EXISTS section_drafts (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                edition_id INTEGER NOT NULL,
                section_name TEXT NOT NULL,
                content TEXT,
                word_count INTEGER,
                model_used TEXT,
                generated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                FOREIGN KEY (edition_id) REFERENCES editions(id)
            );

            CREATE TABLE IF NOT EXISTS compliance_flags (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                section_draft_id INTEGER NOT NULL,
                severity TEXT NOT NULL,
                flag_type TEXT,
                matched_text TEXT,
                rule_reference TEXT,
                explanation TEXT,
                recommended_action TEXT,
                is_resolved BOOLEAN DEFAULT 0,
                resolved_by TEXT,
                resolved_at TIMESTAMP,
                resolution_note TEXT,
                pass_number INTEGER,
                FOREIGN KEY (section_draft_id) REFERENCES section_drafts(id)
            );

            CREATE TABLE IF NOT EXISTS audit_log (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                edition_id INTEGER,
                actor TEXT NOT NULL,
                action TEXT NOT NULL,
                details TEXT,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                FOREIGN KEY (edition_id) REFERENCES editions(id)
            );
            """
        )
        await db.commit()

        # Migrate existing DBs — add columns that may not exist yet
        for col, definition in [
            ("generation_mode", "TEXT DEFAULT 'auto'"),
            ("editorial_brief", "TEXT"),
        ]:
            try:
                await db.execute(
                    f"ALTER TABLE editions ADD COLUMN {col} {definition}"
                )
            except Exception:
                pass  # Column already exists
        await db.commit()

        logger.info("Database initialized at %s", _db_path)
    finally:
        await db.close()
