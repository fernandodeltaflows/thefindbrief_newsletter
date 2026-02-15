# CLAUDE.md — The Find Brief

## Project Summary

The Find Brief is an AI-powered newsletter generation agent for The Find Capital LLC, a cross-border real estate capital advisory firm. The two partners — Francisco Covarrubias and Juliana Soto — are FINRA-registered representatives through Finalis Securities LLC (Member FINRA/SIPC), which makes regulatory compliance non-negotiable for every piece of content the system produces.

This is a **working demo** — not a prototype with mock data. Real news flows through a five-layer pipeline (retrieval → verification → drafting → compliance scan → review), and the partners log in, review flagged content, and approve editions through an in-app interface.

The newsletter covers cross-border real estate capital flows between GCC (Gulf Cooperation Council), LATAM, and US markets. The audience is institutional — sovereign wealth funds, family offices, fund managers, and operators who will immediately spot generic content.

---

## Tech Stack (Strict — Do Not Deviate)

| Layer | Technology | Notes |
|-------|-----------|-------|
| **Backend** | Python 3.11+ / FastAPI | Async throughout. Single application serves both API and frontend. |
| **Frontend** | Jinja2 templates + HTMX | Server-rendered. HTMX handles all reactivity (pipeline progress, live updates). |
| **Database** | SQLite | Single file at `data/thefindbrief.db`. No ORM — use raw SQL or lightweight wrapper. |
| **LLM** | Google Gemini 1.5 Flash | Via `google-generativeai` Python SDK. Used for drafting and compliance Pass 2. |
| **Styling** | Custom CSS | Hand-written. No Tailwind, no Bootstrap, no CSS framework. |
| **Auth** | Session cookies (signed, `itsdangerous`) | Two hardcoded accounts from `.env`. No user table. |
| **Deployment** | Docker → Easypanel on VPS | Single container. SQLite persisted via Docker volume. |

---

## Project Structure

```
the-find-brief/
├── app/
│   ├── main.py                  # FastAPI app entry + route definitions
│   ├── auth.py                  # Login/logout, session management, route protection
│   ├── config.py                # API keys, partner credentials, settings (from .env)
│   ├── database.py              # SQLite setup + table creation + query helpers
│   │
│   ├── pipeline/
│   │   ├── orchestrator.py      # Runs the full pipeline step by step
│   │   ├── retrieval.py         # Layer 1: Perplexity, SerpAPI, EDGAR, FRED
│   │   ├── verification.py      # Layer 2: Tier scoring, dedup, link check
│   │   ├── drafting.py          # Layer 3: Gemini drafting with voice profile
│   │   ├── compliance.py        # Layer 4: Regex + Gemini compliance scan
│   │   └── prompts.py           # All LLM prompts (voice profile, section templates, compliance)
│   │
│   ├── compliance/
│   │   └── compliance_framework.md  # Regulatory reference — FINRA 2210 full text + supporting rules
│   │
│   ├── templates/
│   │   ├── base.html            # Layout shell (dark theme, nav, branding)
│   │   ├── login.html           # Login page
│   │   ├── dashboard.html       # Main dashboard — trigger pipeline + edition history
│   │   ├── sources.html         # Retrieved articles viewer with scoring
│   │   ├── review.html          # Newsletter review + compliance annotations + approve
│   │   └── partials/            # HTMX partial templates
│   │       ├── pipeline_status.html
│   │       ├── article_card.html
│   │       ├── section_draft.html
│   │       └── compliance_flag.html
│   │
│   └── static/
│       ├── css/style.css        # All custom styles
│       ├── js/app.js            # Minimal JS (HTMX does the heavy lifting)
│       └── img/logo.svg         # The Find Capital logo
│
├── data/
│   └── thefindbrief.db          # SQLite (created at runtime)
│
├── requirements.txt
├── .env.example
├── Dockerfile
├── docker-compose.yml
├── CLAUDE.md                    # This file
└── README.md
```

---

## Code Conventions

### Python
- **Async everywhere.** All route handlers and pipeline steps are `async def`. Use `httpx.AsyncClient` for external API calls, not `requests`.
- **Type hints** on all function signatures.
- **No ORM.** Use `aiosqlite` for async SQLite access with raw SQL. Keep queries in the functions that need them or in `database.py`.
- **Config via pydantic `BaseSettings`** loading from `.env`. Never hardcode API keys or credentials.
- **Logging** via Python's `logging` module. No `print()` statements.
- **Error handling:** Every external API call (Perplexity, SerpAPI, Gemini, EDGAR, FRED) must have try/except with graceful degradation. If one source fails, the pipeline continues with the others.

### Routes
- Page routes return `TemplateResponse` (Jinja2).
- API routes under `/api/` return JSON.
- Auth: every route except `/login` and `/static` requires a valid session. Use a dependency or middleware.
- The `/api/edition/{id}/approve` route must capture the logged-in partner's identity for the audit log.

### Templates (Jinja2 + HTMX)
- `base.html` is the layout shell. All pages extend it.
- HTMX attributes go directly on HTML elements (`hx-get`, `hx-post`, `hx-target`, `hx-swap`, `hx-trigger`).
- Partials in `partials/` are fragments returned by API routes for HTMX to swap in.
- No inline `<script>` blocks. Minimal JS in `app.js` only where HTMX can't handle it.
- Load HTMX from CDN: `<script src="https://unpkg.com/htmx.org@1.9.10"></script>`

### CSS
- Single file: `static/css/style.css`.
- Use CSS custom properties (variables) for the color palette — makes theme changes easy.
- No utility classes, no framework. Semantic class names.
- Mobile-responsive is nice but not required for demo — desktop-first is fine.

---

## Design System

The UI must match **thefindcapital.com** — institutional, dark, understated.

### Color Palette (CSS Variables)
```css
:root {
  --bg-primary: #1a1a2e;        /* Deep navy — main background */
  --bg-secondary: #16213e;      /* Slightly lighter — cards, panels */
  --bg-surface: #0f3460;        /* Surface elements — active states */
  --text-primary: #e8e8e8;      /* Main text — off-white, not pure white */
  --text-secondary: #a0a0b0;    /* Secondary text — muted */
  --accent-gold: #c4a35a;       /* Muted gold — buttons, links, highlights */
  --accent-gold-hover: #d4b86a; /* Gold hover state */
  --border: #2a2a4a;            /* Subtle borders */
  --success: #4a9e6d;           /* Approved / resolved states */
  --flag-block: #c0392b;        /* Red — BLOCK flags */
  --flag-mandatory: #e67e22;    /* Orange — MANDATORY_REVIEW flags */
  --flag-warning: #f1c40f;      /* Yellow — WARNING flags */
  --flag-disclaimer: #3498db;   /* Blue — ADD_DISCLAIMER flags */
}
```

### Typography
- **Headings:** Use a serif or refined sans-serif. Google Fonts: `Playfair Display` for headings or `Inter` for a clean sans-serif throughout. NOT Roboto, NOT system fonts.
- **Body text:** 16px base, 1.6 line-height. Readable, not cramped.
- **Code/data:** Monospace for scores, IDs, timestamps.

### Layout Principles
- Generous whitespace. Let content breathe.
- Max content width ~1200px, centered.
- Cards for articles and editions — subtle borders, no heavy shadows.
- Compliance flags are **subtle color chips** with text labels, not screaming badges or icons.
- No emojis anywhere in the UI. No icon overload. Text-first.

### Compliance Flag Display
- `BLOCK` → small red dot + red text label + red left border on the flagged text
- `MANDATORY_REVIEW` → orange dot + orange text
- `WARNING` → yellow dot + yellow text
- `ADD_DISCLAIMER` → blue dot + blue text
- Each flag has a hover/click popover showing: rule reference, explanation, recommended action
- "Resolve" button on each flag

---

## Authentication

Two accounts, hardcoded in `.env`:

```
PARTNER_1_USERNAME=francisco
PARTNER_1_PASSWORD=<set_a_real_password>
PARTNER_1_DISPLAY_NAME=Francisco Covarrubias
PARTNER_2_USERNAME=juliana
PARTNER_2_PASSWORD=<set_a_real_password>
PARTNER_2_DISPLAY_NAME=Juliana Soto
SECRET_KEY=<random_secret_for_signing_cookies>
```

- On login: validate credentials → set signed session cookie containing username + display name.
- On every request: middleware or dependency checks cookie, extracts partner identity.
- On logout: clear cookie, redirect to `/login`.
- Login page: dark themed, The Find logo, centered card with username/password fields. No registration link, no "forgot password."
- Failed login: show error message inline. No redirect.

---

## Database Schema

```sql
CREATE TABLE articles (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    edition_id INTEGER NOT NULL,
    title TEXT NOT NULL,
    url TEXT,
    source TEXT,
    source_tier INTEGER DEFAULT 3,
    quality_score REAL DEFAULT 0.0,
    relevance_category TEXT,        -- 'macro', 'regional', 'deals', 'regulatory'
    is_paywalled BOOLEAN DEFAULT 0,
    is_duplicate BOOLEAN DEFAULT 0,
    raw_snippet TEXT,
    retrieved_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    FOREIGN KEY (edition_id) REFERENCES editions(id)
);

CREATE TABLE editions (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    status TEXT DEFAULT 'draft',    -- 'draft', 'reviewing', 'approved'
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    approved_by TEXT,               -- partner username who approved
    approved_at TIMESTAMP,
    pipeline_stage TEXT,            -- current stage during generation
    pipeline_progress INTEGER DEFAULT 0
);

CREATE TABLE section_drafts (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    edition_id INTEGER NOT NULL,
    section_name TEXT NOT NULL,     -- 'market_pulse', 'regional_spotlight', 'capital_flows', 'regulatory_watch', 'perspective'
    content TEXT,
    word_count INTEGER,
    model_used TEXT,
    generated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    FOREIGN KEY (edition_id) REFERENCES editions(id)
);

CREATE TABLE compliance_flags (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    section_draft_id INTEGER NOT NULL,
    severity TEXT NOT NULL,         -- 'BLOCK', 'MANDATORY_REVIEW', 'WARNING', 'ADD_DISCLAIMER'
    flag_type TEXT,                 -- 'performance_claim', 'guarantee_language', 'solicitation', etc.
    matched_text TEXT,              -- the flagged text from the draft
    rule_reference TEXT,            -- e.g. '2210(d)(1)(B)'
    explanation TEXT,
    recommended_action TEXT,
    is_resolved BOOLEAN DEFAULT 0,
    resolved_by TEXT,               -- partner username
    resolved_at TIMESTAMP,
    resolution_note TEXT,
    pass_number INTEGER,            -- 1 = regex, 2 = LLM
    FOREIGN KEY (section_draft_id) REFERENCES section_drafts(id)
);

CREATE TABLE audit_log (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    edition_id INTEGER,
    actor TEXT NOT NULL,             -- 'francisco', 'juliana', or 'system'
    action TEXT NOT NULL,            -- 'pipeline_started', 'draft_generated', 'flag_resolved', 'edition_approved', etc.
    details TEXT,                    -- JSON string with additional context
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    FOREIGN KEY (edition_id) REFERENCES editions(id)
);
```

---

## Compliance Engine

### Critical: `compliance_framework.md`

The file at `/Users/akira/Documents/0.- AAA AI Agency/1.- AI Agents/0.- Agents/0.- Customers/TheFindCapital/Demo/compliance_framework.md` is the regulatory reference. It contains:
1. **FINRA Rule 2210 full text** — the binding rule. Newsletter = "retail communication" (25+ recipients).
2. **SEC Regulation D summary** — general solicitation boundaries.
3. **CFIUS summary** — cross-border investment review.
4. **SEC Marketing Rule 206(4)-1** — seven general prohibitions.
5. **FINRA Rule 2010** — ethical conduct catch-all.

**This file MUST be loaded into the Gemini compliance prompt context for Pass 2.** Read it at startup or at compliance scan time and inject it into the system prompt.

### Pass 1 — Regex/Pattern Scan
Mechanical checks. Run before the LLM. Each pattern maps to a severity level:

| Pattern Category | Example Triggers | Severity | Rule Reference |
|-----------------|-----------------|----------|----------------|
| Performance claims | "% return", "IRR", "yield of", "outperformed" | MANDATORY_REVIEW | 2210(d)(1)(F) |
| Guarantee language | "guaranteed", "risk-free", "no risk", "certain to" | BLOCK | 2210(d)(1)(B) |
| Solicitation | "contact us", "invest with", "schedule a call" | WARNING | 2210(d)(1)(A), Reg D |
| Forward-looking | "we expect", "will likely", "projected to", "forecast" | ADD_DISCLAIMER | 2210(d)(1)(F) |
| MNPI risk | "insider", "confidential", "non-public", "before announcement" | BLOCK | 2210(d)(1)(B) |
| Superlatives | "best fund", "top manager", "leading performer" | BLOCK | 2210(d)(1)(B) |
| Tax claims | "tax-free", "no tax implications" | WARNING | 2210(d)(1)(B) |

### Pass 2 — Gemini Holistic Review
Send the full draft + `compliance_framework.md` to Gemini with a compliance review prompt. The model evaluates:
- Fair and balanced treatment
- Implicit promises or misleading framing
- Missing risk disclosures
- Whether content could be general solicitation
- Cross-border regulatory awareness
- Tone appropriateness for a registered rep

The model returns structured JSON with any additional flags not caught by regex.

### Required Disclaimers (auto-appended)
- **GENERAL** (every edition): "This newsletter is for informational purposes only and does not constitute investment advice. Securities offered through Finalis Securities LLC, Member FINRA/SIPC."
- **FORWARD-LOOKING** (when flagged): "Contains forward-looking statements based on current expectations. Past performance is not indicative of future results."
- **PERFORMANCE** (when data cited): "Performance data sourced from third-party reports and has not been independently verified by The Find Capital."
- **CROSS-BORDER** (foreign investment flows): "Cross-border investments may be subject to CFIUS review, FATCA/FBAR reporting requirements, and other regulatory obligations."
- **PRIVATE PLACEMENT** (fund activity): "Information based on publicly available sources and does not constitute an endorsement or solicitation."

---

## Voice Profile (for LLM Prompts)

The Find's editorial voice. All drafting prompts must enforce this:

- **Authority without arrogance.** They've been in the rooms — GP seats, LP meetings, sovereign wealth conversations — and don't need to prove it. State facts, don't flex.
- **Institutional vocabulary used naturally.** GP/LP, IRR, cap rates, NAV, basis points, waterfall structures — never define these. The audience knows.
- **Cross-cultural fluency.** GCC, LATAM, and US dynamics appear in the same paragraph naturally. Reference Sharia-compliant structures, AMEFIBRA (Mexican REITs), 1031 exchanges — without explaining what they are.
- **Understated confidence.** No exclamation marks. No hype words ("exciting", "amazing", "incredible"). No clickbait.
- **Dual-perspective framing.** Always consider both the capital seeker and the allocator viewpoint. "This creates an opportunity for operators seeking patient capital, though allocators should note the extended J-curve."
- **Concise.** Every sentence earns its place. No filler, no throat-clearing introductions.
- **Balanced risk/opportunity.** FINRA requires it, but beyond compliance — the audience respects it. Never present an opportunity without the risk, or a risk without the context.

---

## Environment Variables (.env)

```
# Partner Auth
PARTNER_1_USERNAME=francisco
PARTNER_1_PASSWORD=
PARTNER_1_DISPLAY_NAME=Francisco Covarrubias
PARTNER_2_USERNAME=juliana
PARTNER_2_PASSWORD=
PARTNER_2_DISPLAY_NAME=Juliana Soto
SECRET_KEY=

# LLM
GEMINI_API_KEY=

# Data Sources
PERPLEXITY_API_KEY=
SERPAPI_API_KEY=
FRED_API_KEY=

# App
DATABASE_PATH=data/thefindbrief.db
HOST=0.0.0.0
PORT=8000
```

---

## Source Tier Classification

Used in `verification.py` for scoring articles:

**Tier 1 (weight 1.0):** Government + major institutional sources
- federalreserve.gov, sec.gov, finra.org, treasury.gov, bls.gov
- CBRE, JLL, Cushman & Wakefield (research divisions)
- Bloomberg, WSJ, Financial Times, Reuters

**Tier 2 (weight 0.7):** Industry publications + specialized data
- PERE, GlobeSt, Bisnow, Commercial Observer
- Zawya, AMEFIBRA, Gulf News Business
- Preqin, PitchBook, NAREIT, Real Capital Analytics

**Tier 3 (weight 0.3):** Everything else — background context only, never cite directly.

**Quality Score Formula:**
`quality = tier_weight × recency_score × relevance_score × accessibility`
- `recency_score`: 1.0 (< 3 days), 0.8 (< 7 days), 0.5 (< 14 days), 0.2 (older)
- `relevance_score`: keyword match density against section clusters
- `accessibility`: 1.0 (open), 0.5 (paywalled), 0.0 (dead link)

---

## Newsletter Sections

| Section | Target Words | Content Focus |
|---------|-------------|---------------|
| **Market Pulse** | 250-350 | Macro analysis — rates, CPI, Fed policy, market conditions |
| **Regional Spotlight** | 400-500 | Deep-dive rotating: GCC → LATAM → US each edition |
| **Capital Flows** | 200-300 | Deal activity, fund launches, LP/GP movements |
| **Regulatory Watch** | 200-300 | CFIUS, SEC, FINRA updates relevant to cross-border RE |
| **The Find's Perspective** | 100-200 | Partner-written (placeholder text in demo) |

Total: ~1,200-1,600 words per edition.

---

## DO NOT

- **Do not use React, Vue, Svelte, or any JS framework.** Jinja2 + HTMX only.
- **Do not use Tailwind, Bootstrap, or any CSS framework.** Custom CSS only.
- **Do not use PostgreSQL, Supabase, or any external database.** SQLite only.
- **Do not use an ORM** (SQLAlchemy, Tortoise, etc.). Raw SQL with `aiosqlite`.
- **Do not add user registration, password reset, or OAuth.** Two hardcoded accounts.
- **Do not use `requests` library.** Use `httpx` (async).
- **Do not mock API responses.** Always call real APIs. If an API fails, handle gracefully.
- **Do not invent compliance rules.** Every flag must trace back to `compliance_framework.md`.
- **Do not use emojis in the UI.** Anywhere. Ever.
- **Do not add features not in the architecture doc.** This is a demo — scope is fixed.
- **Do not skip the compliance scan.** Every draft runs through both passes before reaching review.
- **Do not hardcode API keys.** Everything from `.env` via `config.py`.
